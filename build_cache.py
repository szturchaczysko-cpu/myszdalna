"""Budowa cache lookup-u indeksów dostawców z PostgreSQL (wmsArtykulyKontrahenci).

Co robi:
1. Łączy się z bazą maggo (PostgreSQL) i pobiera trójkąt
   indeks_glowny -> (dostawca, indeks_u_dostawcy) z cenami.
2. Zapisuje 2 warianty słownika dla różnej tolerancji literówek/OCR:
   - klucz_dokladny: literalnie jak w bazie ("N.40000.S05.H100")
   - klucz_znormalizowany: bez spacji/kropek/wielkich liter ("n40000s05h100")
3. Zapisuje też metadane: kiedy ostatnio zbudowany, ile wierszy.

Wynik: indeksy_dostawcow.json (~150 kB).

Odpalanie z crona np. raz dziennie:
  0 5 * * * cd /opt/twojaApka && python build_cache.py >> cron.log 2>&1
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import psycopg2  # pip install psycopg2-binary
import psycopg2.extras


# ============================================================
# KONFIGURACJA — uzupełnij dane połączenia (lub załaduj z .env)
# ============================================================
DB_CONFIG = {
    "host": os.environ.get("MAGGO_HOST", "localhost"),
    "port": int(os.environ.get("MAGGO_PORT", "5432")),
    "dbname": os.environ.get("MAGGO_DB", "maggo"),
    "user": os.environ.get("MAGGO_USER", ""),
    "password": os.environ.get("MAGGO_PASSWORD", ""),
}

OUTPUT_PATH = os.environ.get("CACHE_PATH", "indeksy_dostawcow.json")


# ============================================================
# SQL — pobiera trójkąt artykuł × dostawca × cena
# ============================================================
SQL_POBIERZ_DOSTAWCOW = """
    SELECT
        a."artID"               AS art_id,
        a."artIndeks"           AS indeks_glowny,
        a."artNazwa"            AS nazwa,
        k."konID"               AS kon_id,
        k."konNazwa"            AS dostawca,
        ak."artkIndeks"         AS indeks_u_dostawcy,
        ak."artkCena"           AS cena_netto,
        ak."artkCenaBrutto"     AS cena_brutto,
        ak."walID"              AS waluta_id,
        ak."artkWiodacy"        AS czy_glowny,
        ak."artkDataTw"::date   AS data_aktualizacji,
        kp."kpaKodProducenta"   AS kod_producenta,
        kp."kpaNazwaProducenta" AS nazwa_producenta
    FROM "public"."wmsArtykulyKontrahenci" ak
    JOIN "public"."wmsArtykuly"     a ON a."artID" = ak."artID"
    LEFT JOIN "public"."wmsKontrahenci" k ON k."konID" = ak."konID"
    LEFT JOIN "public"."wmsArtykulyKodProducenta" kp ON kp."kpaID" = ak."kpaID"
    WHERE a."artAktywny" = true
      AND ak."artkIndeks" IS NOT NULL
      AND ak."artkIndeks" <> ''
    ORDER BY a."artIndeks", ak."artkWiodacy" DESC, ak."artkCena"
"""


def normalizuj(tekst: str) -> str:
    """Normalizacja indeksu dla fuzzy-match: usuń spacje/kropki/myślniki, lowercase.

    Przykłady:
      'N.40000.S05.H100'   -> 'n40000s05h100'
      'N 40000 H100 SNR'   -> 'n40000h100snr'
      'SNR N.40000.S05.H100' -> 'snrn40000s05h100'
    """
    if not tekst:
        return ""
    # usuń wszystkie znaki niealfanumeryczne (kropki, spacje, myślniki, slashe)
    return re.sub(r"[^a-z0-9]", "", tekst.lower())


def zbuduj_cache() -> dict:
    """Łączy się z bazą, pobiera dane i buduje strukturę cache."""
    print(f"[{datetime.now().isoformat()}] Łączę z bazą {DB_CONFIG['host']}/{DB_CONFIG['dbname']}...")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SQL_POBIERZ_DOSTAWCOW)
            rows = cur.fetchall()
    finally:
        conn.close()

    print(f"Pobrano {len(rows)} wierszy z wmsArtykulyKontrahenci.")

    # Strukturę robię tak żeby lookup był O(1):
    #   po_dostawcy[dostawca][indeks_u_dostawcy] -> {indeks_glowny, cena, ...}
    #   po_dostawcy[dostawca][indeks_znormalizowany] -> to samo (fallback)
    # konflikty (ten sam znormalizowany klucz dla 2 różnych dostawców) NIE są problemem
    # bo klucz jest 2-poziomowy: najpierw nazwa dostawcy, potem indeks.

    po_dostawcy = {}          # dostawca_nazwa -> {indeks_dostawcy: dane}
    po_dostawcy_norm = {}     # dostawca_nazwa -> {indeks_znormalizowany: dane}
    artykuly_wszystkie = {}   # indeks_glowny -> nazwa (do walidacji)

    duplikaty = 0
    konflikty_norm = 0

    for r in rows:
        dostawca = (r["dostawca"] or "").strip()
        if not dostawca:
            # rekordy bez dostawcy (jeśli LEFT JOIN nie dał kontrahenta) — pomijam
            continue

        indeks_dost = (r["indeks_u_dostawcy"] or "").strip()
        if not indeks_dost:
            continue

        # kod producenta tego dostawcy (z wmsArtykulyKodProducenta przez kpaID).
        # Gdy pusty (NULL) — część często ma indeks magazynowy = kod (np. numer VAG),
        # więc w fallbacku bierzemy indeks_glowny, żeby maggo miało po czym dopasować.
        kod_prod = (r.get("kod_producenta") or "").strip()
        if not kod_prod:
            kod_prod = (r["indeks_glowny"] or "").strip()

        # przygotuj wpis
        dane = {
            "indeks_glowny": r["indeks_glowny"],
            "nazwa": r["nazwa"],
            "cena_netto": float(r["cena_netto"]) if r["cena_netto"] is not None else None,
            "cena_brutto": float(r["cena_brutto"]) if r["cena_brutto"] is not None else None,
            "waluta_id": r["waluta_id"],
            "czy_glowny": bool(r["czy_glowny"]),
            "data_aktualizacji": r["data_aktualizacji"].isoformat() if r["data_aktualizacji"] else None,
            "kod_producenta": kod_prod,
            "nazwa_producenta": (r.get("nazwa_producenta") or "").strip(),
        }

        # poziom 1: dokładny klucz
        po_dostawcy.setdefault(dostawca, {})
        if indeks_dost in po_dostawcy[dostawca]:
            duplikaty += 1
            # nadpisuję — ostatni wins (już posortowane: wiodący first, najtańszy first)
        po_dostawcy[dostawca][indeks_dost] = dane

        # poziom 2: znormalizowany klucz
        klucz_norm = normalizuj(indeks_dost)
        if klucz_norm:
            po_dostawcy_norm.setdefault(dostawca, {})
            if klucz_norm in po_dostawcy_norm[dostawca]:
                if po_dostawcy_norm[dostawca][klucz_norm]["indeks_glowny"] != dane["indeks_glowny"]:
                    konflikty_norm += 1
                    # konflikt: ten sam znormalizowany ciąg mapuje się na 2 różne
                    # indeksy główne u tego samego dostawcy. To ALARM — zostawiam
                    # pierwszy i loguję. Apka OCR powinna w takim wypadku zwrócić None
                    # zamiast zgadywać.
                    print(f"  ⚠ KONFLIKT norm: dostawca='{dostawca}' "
                          f"klucz_norm='{klucz_norm}' "
                          f"->{po_dostawcy_norm[dostawca][klucz_norm]['indeks_glowny']} "
                          f"vs {dane['indeks_glowny']}")
                    continue
            po_dostawcy_norm[dostawca][klucz_norm] = dane

        artykuly_wszystkie[r["indeks_glowny"]] = r["nazwa"]

    cache = {
        "_meta": {
            "zbudowano": datetime.now(timezone.utc).isoformat(),
            "wierszy_pobranych": len(rows),
            "unikalnych_artykulow": len(artykuly_wszystkie),
            "unikalnych_dostawcow": len(po_dostawcy),
            "duplikaty_pominiete": duplikaty,
            "konflikty_normalizacji": konflikty_norm,
        },
        "po_dostawcy_dokladnie": po_dostawcy,
        "po_dostawcy_znormalizowane": po_dostawcy_norm,
    }

    return cache


def zapisz(cache: dict, sciezka: str):
    with open(sciezka, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    rozmiar_kb = os.path.getsize(sciezka) / 1024
    print(f"Zapisano {sciezka} ({rozmiar_kb:.1f} kB)")
    print(f"Meta: {json.dumps(cache['_meta'], indent=2, ensure_ascii=False)}")


if __name__ == "__main__":
    try:
        cache = zbuduj_cache()
        zapisz(cache, OUTPUT_PATH)
        print("✅ Cache zbudowany pomyślnie.")
        sys.exit(0)
    except Exception as e:
        print(f"❌ BŁĄD: {e}", file=sys.stderr)
        sys.exit(1)
