"""
Test LookupDostawcow (wersja Firestore) na ATRAPIE klienta firestore.
Nie łączy się z niczym — symuluje db.collection(...).stream() i .document().get().

Dane w kształcie Firestore wg specyfikacji (płaskie dokumenty z polami _norm).
Scenariusze identyczne jak w oryginalnym test_lookup.py — sprawdzamy, że logika
znajdz() zachowała się 1:1 mimo zmiany źródła danych.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lookup_firestore import LookupDostawcow, _normalizuj, _normalizuj_dostawce


# ---- Atrapa Firestore ----
class FakeDoc:
    def __init__(self, data, exists=True):
        self._data = data
        self.exists = exists
    def to_dict(self):
        return self._data

class FakeDocRef:
    def __init__(self, store, coll, doc_id):
        self.store, self.coll, self.doc_id = store, coll, doc_id
        self.id = doc_id
    def get(self):
        data = self.store.get(self.coll, {}).get(self.doc_id)
        return FakeDoc(data, exists=data is not None)
    def set(self, data):
        self.store.setdefault(self.coll, {})[self.doc_id] = data

class FakeCollection:
    def __init__(self, store, coll):
        self.store, self.coll = store, coll
        self._auto = 0
    def stream(self):
        for doc_id, data in self.store.get(self.coll, {}).items():
            yield FakeDoc({**data, "_id": doc_id})
    def document(self, doc_id=None):
        if doc_id is None:
            self._auto += 1
            doc_id = f"auto_{self._auto}"
        return FakeDocRef(self.store, self.coll, doc_id)

class FakeDB:
    def __init__(self, store):
        self.store = store
    def collection(self, coll):
        return FakeCollection(self.store, coll)


def _wiersz(art_id, indeks_glowny, nazwa, kon_id, dostawca, indeks_dost,
            cena_netto, cena_brutto, czy_glowny, data):
    """Buduje dokument w kształcie Firestore, z polami _norm jak zrobiłby agent."""
    return {
        "art_id": art_id,
        "indeks_glowny": indeks_glowny,
        "nazwa": nazwa,
        "kon_id": kon_id,
        "dostawca": dostawca,
        "indeks_u_dostawcy": indeks_dost,
        "cena_netto": cena_netto,
        "cena_brutto": cena_brutto,
        "waluta_id": 26,
        "czy_glowny": czy_glowny,
        "data_aktualizacji": data,
        "_indeks_u_dostawcy_norm": _normalizuj(indeks_dost),
        "_dostawca_norm": _normalizuj_dostawce(dostawca),
    }


STORE = {
    "dostawcy_lookup": {
        "1_1": _wiersz(100, "N40000S05H100", "Łożysko kolumny", 1, "INTER CARS S.A.",
                       "N.40000.S05.H100", 61.29, 75.39, False, "2024-05-14"),
        "2_1": _wiersz(100, "N40000S05H100", "Łożysko kolumny", 2, "ALBECO",
                       "N40000S05H100 SNR", 67.10, 82.53, False, "2023-07-03"),
        "3_1": _wiersz(100, "N40000S05H100", "Łożysko kolumny", 3, "Rombor Sp. z o.o.",
                       "N 40000 H100 SNR", 166.50, 204.80, False, "2022-10-13"),
        "4_1": _wiersz(100, "N40000S05H100", "Łożysko kolumny", 4, "IV",
                       "SNR N.40000.S05.H100", 55.28, 67.99, True, "2024-05-14"),
    },
    "dostawcy_lookup_meta": {
        "snapshot": {
            "zbudowano": "2026-06-02T08:00:00+00:00",
            "wierszy_total": 4,
            "unikalnych_artykulow": 1,
            "unikalnych_dostawcow": 4,
            "agent_version": "test-1.0",
        }
    },
}


def main():
    db = FakeDB(STORE)
    lookup = LookupDostawcow(db=db)

    testy = [
        ("Dokładny match: INTER CARS dokładny", "INTER CARS S.A.", "N.40000.S05.H100", "dokladne", "N40000S05H100"),
        ("OCR pomylił kropki na spacje", "INTER CARS S.A.", "N 40000 S05 H100", "znormalizowane", "N40000S05H100"),
        ("Alias dostawcy: 'INTER CARS' bez S.A.", "INTER CARS", "N.40000.S05.H100", "dokladne", "N40000S05H100"),
        ("Alias + literówka", "intercars", "N 40000 S05 H100", "znormalizowane", "N40000S05H100"),
        ("Inny dostawca: ALBECO", "ALBECO", "N40000S05H100 SNR", "dokladne", "N40000S05H100"),
        ("Rombor - skrócony zapis", "Rombor", "N 40000 H100 SNR", "dokladne", "N40000S05H100"),
        ("Wiodący dostawca: IV", "IV", "SNR N.40000.S05.H100", "dokladne", "N40000S05H100"),
        ("Brak: nieznany dostawca", "DZICZIZNA SP. Z O.O.", "N.40000.S05.H100", None, None),
        ("Brak: indeks którego nie znamy", "INTER CARS S.A.", "JAKIES_NIEISTNIEJACE", None, None),
    ]

    print(f"{'OK?':>4}  {'opis':<48}  {'wynik':<22}")
    print("-" * 90)
    sukcesy = 0
    for opis, dost, indeks, oczek_dop, oczek_glow in testy:
        w = lookup.znajdz(dost, indeks)
        if oczek_dop is None:
            ok = w is None
            wynik = "None (poprawnie)" if ok else f"{w.indeks_glowny}"
        else:
            ok = w is not None and w.dopasowanie == oczek_dop and w.indeks_glowny == oczek_glow
            wynik = f"{w.indeks_glowny}/{w.dopasowanie}" if w else "None (źle)"
        marker = " ✓" if ok else " ✗"
        if ok:
            sukcesy += 1
        print(f"{marker:>4}  {opis:<48}  {wynik:<22}")

    print("-" * 90)
    print(f"Wynik: {sukcesy}/{len(testy)} testów przeszło")

    # test loguj_brak — zapis do atrapy
    bid = lookup.loguj_brak("NOWY DOSTAWCA", "XYZ123", "Jakaś część", 99.0, 3, "FV/2026/001")
    zapisane = STORE.get("dostawcy_lookup_braki", {})
    print(f"\nloguj_brak: zapisano dokument id={bid}, w kolekcji braki jest {len(zapisane)} wpis(ów)")

    # test metadanych
    st = lookup.statystyki()
    print(f"Statystyki: {st['wpisow_w_ram']} wpisów, {st['dostawcow_w_ram']} dostawców, "
          f"meta.agent_version={st['meta'].get('agent_version')}")

    return 0 if sukcesy == len(testy) else 1


if __name__ == "__main__":
    sys.exit(main())
