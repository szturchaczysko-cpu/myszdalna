"""
Lookup indeksów dostawców — wersja czytająca z Firestore.

Zachowuje logikę z indeks_lookup.py (znajdz, normalizacja, aliasy dostawców),
ale źródłem danych jest Firestore (kolekcja 'dostawcy_lookup'), nie lokalny JSON.

Struktura w Firestore (ustalona z użytkownikiem):
  - dostawcy_lookup        : 1 dokument per wiersz wmsArtykulyKontrahenci
  - dostawcy_lookup_meta   : dokument 'snapshot' z metadanymi
  - dostawcy_lookup_braki  : log nieznanych pozycji (zapisywany przez apkę)

Pola znormalizowane (_indeks_u_dostawcy_norm, _dostawca_norm) są już wypełnione
przez agenta AI przy zapisie do Firestore — apka ich nie liczy, tylko używa.

Użycie:
    from lookup_firestore import LookupDostawcow
    lookup = LookupDostawcow()           # pobiera całość z Firestore raz przy starcie
    w = lookup.znajdz("INTER CARS S.A.", "N.40000.S05.H100")
    if w:
        print(w.indeks_glowny, w.cena_netto, w.dopasowanie)
    else:
        lookup.loguj_brak(nazwa_dostawcy="...", indeks="...", ...)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Nazwy kolekcji — zgodnie ze specyfikacją
COLL_LOOKUP = "dostawcy_lookup"
COLL_META = "dostawcy_lookup_meta"
DOC_META = "snapshot"
COLL_BRAKI = "dostawcy_lookup_braki"


@dataclass
class WynikLookup:
    """Wynik znalezienia indeksu w cache (identyczny kontrakt jak w indeks_lookup.py)."""
    indeks_glowny: str
    nazwa: str
    cena_netto: Optional[float]
    cena_brutto: Optional[float]
    waluta_id: Optional[int]
    czy_glowny: bool
    data_aktualizacji: Optional[str]
    dopasowanie: str  # 'dokladne' albo 'znormalizowane'


def _normalizuj(tekst: str) -> str:
    """Normalizacja indeksu: usuń wszystko poza [a-z0-9], lowercase.
    Musi być identyczna z tym, co robi agent przy polu _indeks_u_dostawcy_norm.
    """
    if not tekst:
        return ""
    return re.sub(r"[^a-z0-9]", "", tekst.lower())


def _normalizuj_dostawce(tekst: str) -> str:
    """Normalizacja nazwy dostawcy: lowercase + interpunkcja na spacje + zwiń spacje.
    Identyczna z polem _dostawca_norm po stronie agenta. "INTER CARS S.A." -> "inter cars s a"
    """
    if not tekst:
        return ""
    t = tekst.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


class LookupDostawcow:
    """Lookup indeksów dostawców z Firestore (dane ładowane raz przy starcie do RAM)."""

    def __init__(self, db=None, autoload: bool = True):
        """
        db: opcjonalnie gotowy klient firestore. Jeśli None — bierzemy z get_db().
            (wstrzykiwanie ułatwia testy na atrapie)
        """
        if db is None:
            from firebase_client import get_db
            db = get_db()
        self.db = db

        self._po_dostawcy: dict = {}        # dostawca -> {indeks_dokladny: dane}
        self._po_dostawcy_norm: dict = {}   # dostawca -> {indeks_norm: dane}
        self._aliasy_dostawcow: dict = {}   # _dostawca_norm -> dostawca (kanoniczny)
        self._meta: dict = {}

        if autoload:
            self.refresh()

    # ---------- ładowanie z Firestore ----------
    def refresh(self):
        """Pobiera wszystkie dokumenty z 'dostawcy_lookup' i przebudowuje słowniki w RAM.
        Używane przy starcie i przez endpoint /reload-cache.
        """
        po_dostawcy: dict = {}
        po_dostawcy_norm: dict = {}
        aliasy: dict = {}
        liczba = 0

        for doc in self.db.collection(COLL_LOOKUP).stream():
            d = doc.to_dict() or {}
            dostawca = (d.get("dostawca") or "").strip()
            indeks_dost = (d.get("indeks_u_dostawcy") or "").strip()
            if not dostawca or not indeks_dost:
                continue

            dane = {
                "indeks_glowny": d.get("indeks_glowny"),
                "nazwa": d.get("nazwa"),
                "cena_netto": _as_float(d.get("cena_netto")),
                "cena_brutto": _as_float(d.get("cena_brutto")),
                "waluta_id": d.get("waluta_id"),
                "czy_glowny": bool(d.get("czy_glowny")),
                "data_aktualizacji": d.get("data_aktualizacji"),
            }

            # poziom 1: dokładny klucz (literalnie jak w bazie)
            po_dostawcy.setdefault(dostawca, {})[indeks_dost] = dane

            # poziom 2: znormalizowany klucz — użyj pola z agenta, a gdyby go nie było, policz sam
            klucz_norm = d.get("_indeks_u_dostawcy_norm") or _normalizuj(indeks_dost)
            if klucz_norm:
                slo = po_dostawcy_norm.setdefault(dostawca, {})
                istn = slo.get(klucz_norm)
                if istn and istn.get("indeks_glowny") != dane["indeks_glowny"]:
                    # konflikt normalizacji: ten sam znormalizowany ciąg -> 2 różne indeksy główne.
                    # Zostawiamy pierwszy, logujemy. Fallback dla tego klucza nie zadziała (bezpieczniej).
                    logger.warning(
                        "Konflikt normalizacji u '%s' klucz '%s': %s vs %s",
                        dostawca, klucz_norm, istn.get("indeks_glowny"), dane["indeks_glowny"],
                    )
                else:
                    slo[klucz_norm] = dane

            # alias dostawcy — użyj pola z agenta, a gdyby go nie było, policz sam
            d_norm = d.get("_dostawca_norm") or _normalizuj_dostawce(dostawca)
            if d_norm:
                aliasy.setdefault(d_norm, dostawca)

            liczba += 1

        self._po_dostawcy = po_dostawcy
        self._po_dostawcy_norm = po_dostawcy_norm
        self._aliasy_dostawcow = aliasy
        logger.info("Załadowano %d wpisów, %d dostawców z Firestore.", liczba, len(po_dostawcy))

        # metadane (osobny dokument)
        try:
            meta_doc = self.db.collection(COLL_META).document(DOC_META).get()
            self._meta = meta_doc.to_dict() if meta_doc.exists else {}
        except Exception as e:
            logger.warning("Nie udało się pobrać metadanych: %s", e)
            self._meta = {}

        return liczba

    # ---------- dopasowanie dostawcy ----------
    def _dopasuj_dostawce(self, nazwa_dostawcy: str) -> Optional[str]:
        if not nazwa_dostawcy:
            return None
        if nazwa_dostawcy in self._po_dostawcy:
            return nazwa_dostawcy
        norm = _normalizuj_dostawce(nazwa_dostawcy)
        if norm in self._aliasy_dostawcow:
            return self._aliasy_dostawcow[norm]
        # prefiks ze spacjami: "INTER CARS" pasuje do "inter cars s a"
        for n_baza, oryginal in self._aliasy_dostawcow.items():
            if norm and (norm.startswith(n_baza) or n_baza.startswith(norm)):
                return oryginal
        # fallback bez spacji: "intercars" -> "inter cars s a"
        norm_ns = norm.replace(" ", "")
        for n_baza, oryginal in self._aliasy_dostawcow.items():
            baza_ns = n_baza.replace(" ", "")
            if norm_ns and (norm_ns.startswith(baza_ns) or baza_ns.startswith(norm_ns)):
                return oryginal
        return None

    # ---------- główny lookup ----------
    def znajdz(self, nazwa_dostawcy: str, indeks_zewnetrzny: str) -> Optional[WynikLookup]:
        """dostawca + indeks_u_dostawcy -> WynikLookup albo None."""
        kanon = self._dopasuj_dostawce(nazwa_dostawcy)
        if not kanon:
            return None

        slo_dokl = self._po_dostawcy.get(kanon, {})
        if indeks_zewnetrzny in slo_dokl:
            return WynikLookup(dopasowanie="dokladne", **slo_dokl[indeks_zewnetrzny])

        slo_norm = self._po_dostawcy_norm.get(kanon, {})
        norm = _normalizuj(indeks_zewnetrzny)
        if norm and norm in slo_norm:
            return WynikLookup(dopasowanie="znormalizowane", **slo_norm[norm])

        return None

    # ---------- logowanie braków ----------
    def loguj_brak(
        self,
        nazwa_dostawcy: str,
        indeks: str,
        nazwa_pozycji: str = "",
        cena: Optional[float] = None,
        ilosc: Optional[float] = None,
        nr_faktury: str = "",
    ) -> str:
        """Zapisuje nieznaną pozycję do 'dostawcy_lookup_braki'. Zwraca ID dokumentu."""
        dane = {
            "data": datetime.now(timezone.utc),
            "nazwa_dostawcy_z_faktury": nazwa_dostawcy or "",
            "indeks_z_faktury": indeks or "",
            "nazwa_pozycji_z_faktury": nazwa_pozycji or "",
            "cena_z_faktury": _as_float(cena),
            "ilosc_z_faktury": _as_float(ilosc),
            "nr_faktury": nr_faktury or "",
            "status": "nowy",
        }
        ref = self.db.collection(COLL_BRAKI).document()
        ref.set(dane)
        return ref.id

    # ---------- pomocnicze / UI ----------
    def lista_dostawcow(self) -> list[str]:
        return sorted(self._po_dostawcy.keys())

    def meta(self) -> dict:
        return dict(self._meta)

    def wiek_cache_godzin(self) -> Optional[float]:
        """Ile godzin temu zbudowano snapshot (z pola 'zbudowano' w metadanych)."""
        z = self._meta.get("zbudowano")
        if not z:
            return None
        dt = _as_datetime(z)
        if not dt:
            return None
        teraz = datetime.now(dt.tzinfo or timezone.utc)
        return (teraz - dt).total_seconds() / 3600

    def statystyki(self) -> dict:
        """Szybkie liczby do nagłówka UI."""
        return {
            "wpisow_w_ram": sum(len(v) for v in self._po_dostawcy.values()),
            "dostawcow_w_ram": len(self._po_dostawcy),
            "meta": self.meta(),
            "wiek_godzin": self.wiek_cache_godzin(),
        }


def _as_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_datetime(v):
    """Akceptuje firestore Timestamp / datetime / string ISO."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    # firestore zwykle zwraca obiekt z .ToDatetime() albo już datetime;
    # obsłużmy też string ISO
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    to_dt = getattr(v, "ToDatetime", None)
    if callable(to_dt):
        try:
            return to_dt()
        except Exception:
            return None
    return None
