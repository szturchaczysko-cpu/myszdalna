"""
pamiec_dopasowan.py — trwała pamięć decyzji użytkownika o pozycjach faktur.

Dwie rzeczy, oba w Firestore (przeżywają restart apki i działają między userami):

  1) CZARNA LISTA — indeksy dostawcy odrzucone ("usuń z listy"). Pozycja raz
     odrzucona jest ZAWSZE pomijana w przyszłych odczytach (to towar innej
     kategorii, nie wpisywany do maggo tą drogą).
     Kolekcja: faktury_czarna_lista, dokument = klucz(dostawca, indeks).

  2) WYUCZONE DOPASOWANIA — ręczne mapowania (dostawca+indeks dostawcy ->
     indeks wewnętrzny), które user zatwierdził klikając "użyj tego". Następnym
     razem ten sam indeks od razu wskakuje na zatwierdzony indeks wewnętrzny.
     Kolekcja: faktury_dopasowania, dokument = klucz(dostawca, indeks).

Plus SZUKANIE PODOBNYCH (gdy lookup dał BRAK):
  A1 — przeszukuje WSZYSTKICH dostawców (nie tylko tego z faktury),
  A2 — dopasowanie rozmyte: fragmenty, wspólny prefiks, literówki (odległość
       edycyjna), po normalizacji (bez spacji/kropek/znaków, lowercase).

Firestore inicjalizowany tym samym wzorcem co dashboard (FIREBASE_CREDS).
"""

from __future__ import annotations

import re
import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────
#  FIRESTORE — inicjalizacja (wzorzec z dashboardu: firebase_admin)
# ──────────────────────────────────────────────────────────────────────────

_COL_CZARNA = "faktury_czarna_lista"
_COL_DOPAS = "faktury_dopasowania"


def init_firestore(secrets):
    """
    Zwraca klienta Firestore albo (None, błąd). `secrets` to st.secrets.
    Używa firebase_admin + FIREBASE_CREDS — jak reszta apek Autos.
    Bezpieczne przy wielokrotnym wywołaniu (nie inicjalizuje appki dwa razy).
    """
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        return None, "Brak biblioteki firebase-admin (dodaj do requirements.txt)"

    try:
        if not firebase_admin._apps:
            creds_dict = json.loads(secrets["FIREBASE_CREDS"])
            cred = credentials.Certificate(creds_dict)
            firebase_admin.initialize_app(cred)
        return firestore.client(), None
    except Exception as e:
        return None, f"Błąd inicjalizacji Firestore: {e}"


# ──────────────────────────────────────────────────────────────────────────
#  KLUCZE — stabilny identyfikator dokumentu z (dostawca, indeks)
# ──────────────────────────────────────────────────────────────────────────

def _klucz(dostawca: str, indeks: str) -> str:
    """
    Buduje bezpieczny identyfikator dokumentu Firestore z dostawcy i indeksu.
    Firestore nie lubi '/', spacji itp. w ID — używamy hasha, ale zachowujemy
    czytelny prefiks dla podglądu w konsoli Firebase.
    """
    surowy = f"{(dostawca or '').strip().lower()}|{(indeks or '').strip().lower()}"
    h = hashlib.sha1(surowy.encode("utf-8")).hexdigest()[:16]
    czytelny = re.sub(r"[^a-z0-9]+", "_", (indeks or "").strip().lower())[:40].strip("_")
    return f"{czytelny}__{h}" if czytelny else h


# ──────────────────────────────────────────────────────────────────────────
#  CZARNA LISTA
# ──────────────────────────────────────────────────────────────────────────

def wczytaj_czarna_liste(db) -> set[str]:
    """Zwraca zbiór kluczy odrzuconych pozycji. Pusty zbiór gdy błąd/pusto."""
    try:
        out = set()
        for d in db.collection(_COL_CZARNA).stream():
            out.add(d.id)
        return out
    except Exception:
        return set()


def dodaj_do_czarnej_listy(db, dostawca: str, indeks: str) -> bool:
    """Trwale odrzuca pozycję (dostawca+indeks). True gdy zapisano."""
    try:
        k = _klucz(dostawca, indeks)
        db.collection(_COL_CZARNA).document(k).set({
            "dostawca": dostawca,
            "indeks_dostawcy": indeks,
            "dodano": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception:
        return False


def usun_z_czarnej_listy(db, dostawca: str, indeks: str) -> bool:
    """Cofa odrzucenie (gdyby user się rozmyślił — przyda się w panelu zarządzania)."""
    try:
        db.collection(_COL_CZARNA).document(_klucz(dostawca, indeks)).delete()
        return True
    except Exception:
        return False


def na_czarnej_liscie(czarna: set[str], dostawca: str, indeks: str) -> bool:
    return _klucz(dostawca, indeks) in czarna


# ──────────────────────────────────────────────────────────────────────────
#  WYUCZONE DOPASOWANIA
# ──────────────────────────────────────────────────────────────────────────

def wczytaj_dopasowania(db) -> dict[str, dict]:
    """
    Zwraca słownik {klucz: {indeks_wewnetrzny, dostawca, indeks_dostawcy, ...}}.
    Pusty gdy błąd/pusto.
    """
    try:
        out = {}
        for d in db.collection(_COL_DOPAS).stream():
            out[d.id] = d.to_dict()
        return out
    except Exception:
        return {}


def zapisz_dopasowanie(db, dostawca: str, indeks: str, indeks_wewnetrzny: str,
                       nazwa: str = "") -> bool:
    """Trwale zapamiętuje: (dostawca+indeks dostawcy) -> indeks wewnętrzny."""
    try:
        k = _klucz(dostawca, indeks)
        db.collection(_COL_DOPAS).document(k).set({
            "dostawca": dostawca,
            "indeks_dostawcy": indeks,
            "indeks_wewnetrzny": indeks_wewnetrzny,
            "nazwa": nazwa,
            "zapisano": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception:
        return False


def usun_dopasowanie(db, dostawca: str, indeks: str) -> bool:
    try:
        db.collection(_COL_DOPAS).document(_klucz(dostawca, indeks)).delete()
        return True
    except Exception:
        return False


def pobierz_wyuczone(dopasowania: dict, dostawca: str, indeks: str) -> Optional[str]:
    """Jeśli para (dostawca, indeks) ma wyuczony indeks wewnętrzny — zwraca go."""
    wpis = dopasowania.get(_klucz(dostawca, indeks))
    if wpis:
        return wpis.get("indeks_wewnetrzny") or None
    return None


# ──────────────────────────────────────────────────────────────────────────
#  SZUKANIE PODOBNYCH (A1 + A2)
# ──────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normalizacja do porównań: małe litery, bez wszystkiego poza [a-z0-9]."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _lev(a: str, b: str, limit: int = 3) -> int:
    """Odległość Levenshteina z wczesnym cięciem (zwraca limit+1 gdy przekroczy)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            best = min(best, cur[-1])
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


@dataclass
class Kandydat:
    indeks_wewnetrzny: str
    nazwa: str
    dostawca_zrodlowy: str        # u którego dostawcy znaleziono ten zapis
    indeks_dostawcy: str          # jak był zapisany u tamtego dostawcy
    powod: str                    # "fragment" / "prefiks" / "literówka (odl. N)"
    score: float                  # im wyżej tym lepiej (do sortowania)


def szukaj_podobnych(lookup, indeks_szukany: str, maks: int = 8) -> list[Kandydat]:
    """
    A1+A2: przeszukuje CAŁĄ bazę (wszystkich dostawców) szukając indeksów
    podobnych do `indeks_szukany`. Zwraca listę kandydatów (unikalne indeksy
    wewnętrzne), posortowaną od najlepszego.

    `lookup` to LookupDostawcow — sięgamy do jego ._cache["po_dostawcy_dokladnie"].
    """
    cel = _norm(indeks_szukany)
    if not cel:
        return []

    cache = lookup._cache.get("po_dostawcy_dokladnie", {})

    # zbieramy najlepszy trafiony wariant per indeks_wewnetrzny (dedup)
    najlepszy: dict[str, Kandydat] = {}

    for dostawca, indeksy in cache.items():
        for ind_dost, wpis in indeksy.items():
            n = _norm(ind_dost)
            if not n:
                continue

            powod = None
            score = 0.0

            # 1) FRAGMENT: jeden zawiera drugi (np. "62422" ⊂ "62422masiero")
            if cel == n:
                powod, score = "dokładne (po normalizacji)", 100.0
            elif cel in n or n in cel:
                # im bliższa długość, tym lepiej
                krotszy, dluzszy = sorted((len(cel), len(n)))
                powod = "fragment"
                score = 80.0 + 15.0 * (krotszy / dluzszy)
            else:
                # 2) WSPÓLNY PREFIKS (co najmniej 4 znaki albo 60% krótszego)
                wsp = _wspolny_prefiks(cel, n)
                prog = max(4, int(0.6 * min(len(cel), len(n))))
                if wsp >= prog:
                    powod = "wspólny początek"
                    score = 50.0 + wsp
                else:
                    # 3) LITERÓWKA: odległość edycyjna <= 2
                    d = _lev(cel, n, limit=2)
                    if d <= 2:
                        powod = f"literówka (odl. {d})"
                        score = 70.0 - 10.0 * d

            if powod is None:
                continue

            iw = wpis.get("indeks_glowny", "")
            if not iw:
                continue

            kand = Kandydat(
                indeks_wewnetrzny=iw,
                nazwa=wpis.get("nazwa", ""),
                dostawca_zrodlowy=dostawca,
                indeks_dostawcy=ind_dost,
                powod=powod,
                score=score,
            )
            # dedup po indeksie wewnętrznym — trzymamy najwyższy score
            if iw not in najlepszy or kand.score > najlepszy[iw].score:
                najlepszy[iw] = kand

    wynik = sorted(najlepszy.values(), key=lambda k: k.score, reverse=True)
    return wynik[:maks]


def _wspolny_prefiks(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n
