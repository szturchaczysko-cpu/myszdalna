"""Lookup indeksów: dostawca + indeks_u_dostawcy -> indeks_wewnętrzny.

Używany przez apkę OCR-fakturową. Ładuje cache JSON raz przy starcie,
lookup to operacja słownikowa (< 1 mikrosekundy).

Użycie:
    from indeks_lookup import LookupDostawcow

    lookup = LookupDostawcow("indeksy_dostawcow.json")

    # podstawowy lookup: dokładny match
    wynik = lookup.znajdz("INTER CARS S.A.", "N.40000.S05.H100")
    if wynik:
        print(wynik.indeks_glowny)  # "N40000S05H100"
        print(wynik.cena_netto)     # 61.29
        print(wynik.dopasowanie)    # "dokladne"

    # gdyby OCR zniekształcił indeks ("N 40000 S05 H100"):
    wynik = lookup.znajdz("INTER CARS S.A.", "N 40000 S05 H100")
    # dalej znajdzie, bo fuzzy match po znormalizowanej formie
    print(wynik.dopasowanie)  # "znormalizowane"
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass
class WynikLookup:
    """Wynik znalezienia indeksu w cache."""
    indeks_glowny: str
    nazwa: str
    cena_netto: Optional[float]
    cena_brutto: Optional[float]
    waluta_id: Optional[int]
    czy_glowny: bool
    data_aktualizacji: Optional[str]
    dopasowanie: str  # 'dokladne' albo 'znormalizowane' — apka może to zalogować
    # kod producenta tego dostawcy (z nowego cache). Domyślne dla zgodności
    # ze starym cache (gdyby jeszcze nie przebudowany) — wtedy puste.
    kod_producenta: str = ""
    nazwa_producenta: str = ""


def _normalizuj(tekst: str) -> str:
    """Ten sam algorytm normalizacji co build_cache.py.

    Jeśli zmieniasz tu, ZMIEŃ TEŻ TAM. Inaczej cache i runtime się rozjadą.
    """
    if not tekst:
        return ""
    return re.sub(r"[^a-z0-9]", "", tekst.lower())


def _normalizuj_dostawce(tekst: str) -> str:
    """Normalizacja nazwy dostawcy (na potrzeby alias-matchingu).

    OCR czasem czyta "INTER CARS S.A." jako "INTER CARS SA" lub "intercars".
    Robię łagodną normalizację: lowercase + usuwam interpunkcję + zostawiam spacje.
    """
    if not tekst:
        return ""
    t = tekst.lower()
    t = re.sub(r"[^\w\s]", " ", t)  # usuń kropki/przecinki
    t = re.sub(r"\s+", " ", t).strip()
    return t


class LookupDostawcow:
    """Lookup indeksów dostawców z cache JSON (ładowany raz przy starcie)."""

    def __init__(self, cache_path: str = "indeksy_dostawcow.json"):
        self.cache_path = Path(cache_path)
        self._cache = None
        self._aliasy_dostawcow = None
        self._zbudowano_kiedy = None
        self._przeladuj()

    def _przeladuj(self):
        """Wczytuje cache z dysku do RAM."""
        if not self.cache_path.exists():
            raise FileNotFoundError(
                f"Cache {self.cache_path} nie istnieje. "
                "Odpal najpierw build_cache.py żeby go wygenerować."
            )

        with self.cache_path.open(encoding="utf-8") as f:
            self._cache = json.load(f)

        # mapa znormalizowanych nazw dostawców -> oryginalna nazwa (alias-match)
        self._aliasy_dostawcow = {
            _normalizuj_dostawce(d): d
            for d in self._cache["po_dostawcy_dokladnie"].keys()
        }

        meta = self._cache.get("_meta", {})
        self._zbudowano_kiedy = meta.get("zbudowano")
        logger.info(
            "Cache załadowany: %d artykułów, %d dostawców, zbudowany %s",
            meta.get("unikalnych_artykulow", 0),
            meta.get("unikalnych_dostawcow", 0),
            self._zbudowano_kiedy,
        )

    def przeladuj(self):
        """Publiczne przeładowanie (np. po nightly cron)."""
        self._przeladuj()

    def wiek_cache_godzin(self) -> Optional[float]:
        """Ile godzin temu cache został zbudowany (do monitoringu)."""
        if not self._zbudowano_kiedy:
            return None
        zbudowano = datetime.fromisoformat(self._zbudowano_kiedy.replace("Z", "+00:00"))
        teraz = datetime.now(zbudowano.tzinfo)
        return (teraz - zbudowano).total_seconds() / 3600

    def _dopasuj_dostawce(self, nazwa_dostawcy: str) -> Optional[str]:
        """Znajdź kanoniczną nazwę dostawcy (z bazy) na podstawie OCR-owego stringa."""
        if not nazwa_dostawcy:
            return None
        # 1) próbuj dokładny match (z bazy)
        if nazwa_dostawcy in self._cache["po_dostawcy_dokladnie"]:
            return nazwa_dostawcy
        # 2) alias-match po znormalizowanej nazwie
        norm = _normalizuj_dostawce(nazwa_dostawcy)
        if norm in self._aliasy_dostawcow:
            return self._aliasy_dostawcow[norm]
        # 3) szukaj po prefiksie ZE spacjami (np. "INTER CARS" pasuje do "INTER CARS S.A.")
        for n_baza, oryginal in self._aliasy_dostawcow.items():
            if norm and (norm.startswith(n_baza) or n_baza.startswith(norm)):
                return oryginal
        # 4) ostatni fallback: porównaj BEZ spacji (np. "intercars" -> "inter cars s a")
        # Niektóre OCR-y zlepiają nazwę w jedno słowo lub odwrotnie.
        norm_nospaces = norm.replace(" ", "")
        for n_baza, oryginal in self._aliasy_dostawcow.items():
            baza_nospaces = n_baza.replace(" ", "")
            if norm_nospaces and (
                norm_nospaces.startswith(baza_nospaces)
                or baza_nospaces.startswith(norm_nospaces)
            ):
                return oryginal
        return None

    def znajdz(
        self, nazwa_dostawcy: str, indeks_zewnetrzny: str,
    ) -> Optional[WynikLookup]:
        """Najważniejsza funkcja: znajdź indeks wewnętrzny po dostawcy+indeksie zewn.

        Zwraca WynikLookup albo None jeśli nic nie pasuje.

        Strategia:
          1. Dopasuj dostawcę (dokładnie / przez aliasy)
          2. Dla tego dostawcy: szukaj indeksu dokładnie
          3. Jeśli nie ma — szukaj po znormalizowanej formie (literówki OCR)
        """
        dostawca_kanoniczny = self._dopasuj_dostawce(nazwa_dostawcy)
        if not dostawca_kanoniczny:
            return None

        # 1) dokładne dopasowanie
        slownik_dokl = self._cache["po_dostawcy_dokladnie"].get(dostawca_kanoniczny, {})
        if indeks_zewnetrzny in slownik_dokl:
            return WynikLookup(dopasowanie="dokladne", **slownik_dokl[indeks_zewnetrzny])

        # 2) fallback: znormalizowane dopasowanie
        slownik_norm = self._cache["po_dostawcy_znormalizowane"].get(dostawca_kanoniczny, {})
        norm = _normalizuj(indeks_zewnetrzny)
        if norm and norm in slownik_norm:
            return WynikLookup(dopasowanie="znormalizowane", **slownik_norm[norm])

        return None

    def lista_dostawcow(self) -> list[str]:
        """Lista wszystkich dostawców w cache (do walidacji w UI)."""
        return sorted(self._cache["po_dostawcy_dokladnie"].keys())

    def statystyki_dostawcy(self, nazwa_dostawcy: str) -> Optional[dict]:
        """Ile indeksów ma dany dostawca, kiedy ostatnio aktualizowany."""
        kanon = self._dopasuj_dostawce(nazwa_dostawcy)
        if not kanon:
            return None
        slownik = self._cache["po_dostawcy_dokladnie"][kanon]
        daty = [v["data_aktualizacji"] for v in slownik.values()
                if v["data_aktualizacji"]]
        return {
            "dostawca": kanon,
            "liczba_indeksow": len(slownik),
            "najstarsza_cena": min(daty) if daty else None,
            "najnowsza_cena": max(daty) if daty else None,
        }


# ============================================================
# Demo: jak używać z apki
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    lookup = LookupDostawcow("indeksy_dostawcow.json")

    print(f"Wiek cache: {lookup.wiek_cache_godzin():.1f} h")
    print(f"Liczba dostawców: {len(lookup.lista_dostawcow())}")

    # Test 1: dokładny match
    w = lookup.znajdz("INTER CARS S.A.", "N.40000.S05.H100")
    print(f"\nTest 1 (dokładny): {w}")

    # Test 2: literówka OCR (spacje zamiast kropek)
    w = lookup.znajdz("INTER CARS S.A.", "N 40000 S05 H100")
    print(f"\nTest 2 (znormalizowane): {w}")

    # Test 3: alias dostawcy ("INTER CARS" zamiast "INTER CARS S.A.")
    w = lookup.znajdz("INTER CARS", "N.40000.S05.H100")
    print(f"\nTest 3 (alias dostawcy): {w}")

    # Test 4: nie ma w cache
    w = lookup.znajdz("INTER CARS S.A.", "NIEISTNIEJACY123")
    print(f"\nTest 4 (brak): {w}")
