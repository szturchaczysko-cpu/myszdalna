"""
Apka OCR-fakturowa Autos — KROK 1: widok testowy lookup-u dostawców.

Architektura: plik-na-dysku (NIE Firestore).
Dane: indeksy_dostawcow.json (~870 kB) commitowany do repo razem z kodem.
Lookup: LookupDostawcow z indeks_lookup.py (oryginalny pakiet, bez zmian) —
ładuje JSON raz przy starcie, lookup z RAM.

Update danych: raz na 3-6 miesięcy ręcznie (SQL -> CSV -> build_cache.py -> podmiana
JSON w repo -> redeploy). Brak synchronizacji w runtime, brak zależności od bazy.

Uruchomienie:
    pip install streamlit
    streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from indeks_lookup import LookupDostawcow

CACHE_PATH = "indeksy_dostawcow.json"

st.set_page_config(page_title="Autos — lookup dostawców", page_icon="🔧", layout="centered")


@st.cache_resource(show_spinner="Ładuję dane z pliku...")
def zaladuj_lookup() -> LookupDostawcow:
    """Ładuje lookup raz na sesję (cache_resource = jeden wspólny obiekt w RAM)."""
    return LookupDostawcow(CACHE_PATH)


st.title("🔧 Lookup dostawców — test")
st.caption("Krok 1: walidacja danych + lookup. OCR dokładamy później.")

# --- ładowanie + obsługa braku/uszkodzenia pliku ---
try:
    lookup = zaladuj_lookup()
except FileNotFoundError:
    st.error(
        f"Nie znaleziono pliku **{CACHE_PATH}**.\n\n"
        "Plik z danymi musi leżeć w tym samym folderze co app.py "
        "(i być commitowany do repo). Wygeneruj go przez build_cache.py "
        "albo wgraj do repozytorium."
    )
    st.stop()
except Exception as e:
    st.error(f"Nie udało się wczytać danych z {CACHE_PATH}.\n\nSzczegóły: {e}")
    st.stop()

# --- pasek metadanych (z sekcji _meta JSON-a) ---
st.divider()
meta = lookup._cache.get("_meta", {})
wiek = lookup.wiek_cache_godzin()

c1, c2, c3 = st.columns(3)
c1.metric("Wpisów (źródło)", meta.get("wierszy_uwzglednionych", meta.get("wierszy_pobranych", "—")))
c2.metric("Dostawców", meta.get("unikalnych_dostawcow", len(lookup.lista_dostawcow())))
c3.metric("Artykułów", meta.get("unikalnych_artykulow", "—"))

# druga linia metadanych — szczegóły
linia = []
if wiek is not None:
    if wiek < 48:
        linia.append(f"dane zbudowane {wiek:.1f} h temu")
    else:
        dni = wiek / 24
        linia.append(f"dane zbudowane {dni:.0f} dni temu")
if meta.get("zrodlo"):
    linia.append(f"źródło: {meta['zrodlo']}")
if meta.get("duplikaty_pominiete") is not None:
    linia.append(f"duplikatów pominięto: {meta['duplikaty_pominiete']}")
if meta.get("konflikty_normalizacji"):
    linia.append(f"konfliktów normalizacji: {meta['konflikty_normalizacji']}")
if linia:
    st.caption(" · ".join(linia))

# informacja o konfliktach normalizacji (jeśli są)
if meta.get("konflikty_normalizacji"):
    st.info(
        f"W danych jest {meta['konflikty_normalizacji']} konflikt(ów) normalizacji "
        "(ten sam znormalizowany indeks → różne artykuły u tego samego dostawcy). "
        "Dla tych przypadków zadziała tylko dopasowanie dokładne — fallback jest wyłączony, "
        "żeby nie zgadywać."
    )

# --- formularz lookup ---
st.divider()
st.subheader("Sprawdź pozycję")

with st.form("lookup_form"):
    nazwa_dostawcy = st.text_input("Nazwa dostawcy (jak na fakturze)", placeholder="np. INTER CARS S.A.")
    indeks = st.text_input("Indeks z faktury", placeholder="np. N.40000.S05.H100")
    szukaj = st.form_submit_button("Szukaj", type="primary")

if szukaj:
    if not nazwa_dostawcy.strip() or not indeks.strip():
        st.warning("Podaj nazwę dostawcy i indeks.")
    else:
        wynik = lookup.znajdz(nazwa_dostawcy.strip(), indeks.strip())
        if wynik is None:
            st.error("❌ Nie znaleziono tej pozycji.")
            kanon = lookup._dopasuj_dostawce(nazwa_dostawcy.strip())
            if kanon:
                st.info(f"Dostawcę rozpoznano jako: **{kanon}** — ale tego indeksu u niego nie ma.")
            else:
                st.info(
                    "Tego dostawcy w ogóle nie ma w danych (sprawdź pisownię). "
                    "Dostępni dostawcy są na liście poniżej."
                )
            st.caption(
                "Przy OCR taka pozycja trafi do logu nieznanych pozycji "
                "(do ręcznego uzupełnienia)."
            )
        else:
            if wynik.dopasowanie == "dokladne":
                st.success(f"✅ Znaleziono (dopasowanie dokładne): **{wynik.indeks_glowny}**")
            else:
                st.success(f"✅ Znaleziono (dopasowanie znormalizowane): **{wynik.indeks_glowny}**")
                st.warning(
                    "Dopasowano przez normalizację (możliwa literówka OCR). "
                    "Przy OCR ta pozycja dostanie flagę 'do weryfikacji wzrokowej'."
                )

            kol1, kol2 = st.columns(2)
            with kol1:
                st.write("**Indeks wewnętrzny:**", wynik.indeks_glowny)
                st.write("**Nazwa:**", wynik.nazwa)
                st.write("**Cena netto (katalogowa):**", wynik.cena_netto)
                st.write("**Cena brutto:**", wynik.cena_brutto)
            with kol2:
                st.write("**Waluta ID:**", wynik.waluta_id)
                st.write("**Wiodący dostawca:**", "tak" if wynik.czy_glowny else "nie")
                st.write("**Data aktualizacji ceny:**", wynik.data_aktualizacji)

            st.caption(
                "Uwaga: cena to wartość KATALOGOWA z bazy (do identyfikacji pozycji), "
                "nie cena z konkretnej faktury — i bywa stara (2022-2025)."
            )

# --- lista dostępnych dostawców (pomoc przy wpisywaniu) ---
st.divider()
with st.expander(f"Lista dostawców w danych ({len(lookup.lista_dostawcow())})"):
    for d in lookup.lista_dostawcow():
        st.write("·", d)
