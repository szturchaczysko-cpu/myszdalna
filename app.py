"""
Apka OCR-fakturowa Autos — KROK 1: widok testowy lookup-u dostawców.

Cel tego widoku: udowodnić, że połączenie Firestore + LookupDostawcow działa,
ZANIM dołożymy OCR. Wpisujesz nazwę dostawcy i indeks z faktury, klikasz Szukaj,
widzisz wynik znajdz() w czytelnej formie.

Uruchomienie:
    pip install streamlit firebase-admin
    streamlit run app.py

Wymaga działającego firebase_client.py (z credentials do Firestore) w tym samym folderze.
"""
from __future__ import annotations

import streamlit as st

from lookup_firestore import LookupDostawcow

st.set_page_config(page_title="Autos — lookup dostawców", page_icon="🔧", layout="centered")


@st.cache_resource(show_spinner="Ładuję dane z Firestore...")
def zaladuj_lookup() -> LookupDostawcow:
    """Ładuje lookup raz na sesję (cache_resource = jeden wspólny obiekt)."""
    return LookupDostawcow()


def odswiez_lookup():
    """Czyści cache i przeładowuje z Firestore (przycisk /reload)."""
    zaladuj_lookup.clear()
    return zaladuj_lookup()


st.title("🔧 Lookup dostawców — test")
st.caption("Krok 1: walidacja Firestore + lookup. OCR dokładamy później.")

# --- ładowanie + obsługa błędu połączenia ---
try:
    lookup = zaladuj_lookup()
except Exception as e:
    st.error(
        "Nie udało się połączyć z Firestore albo wczytać danych.\n\n"
        f"Szczegóły: {e}\n\n"
        "Sprawdź, czy firebase_client.py ma poprawne credentials i czy kolekcja "
        "'dostawcy_lookup' istnieje (agent musiał już wrzucić dane)."
    )
    st.stop()

# --- pasek metadanych (kiedy ostatni refresh) ---
st.divider()
stat = lookup.statystyki()
meta = stat["meta"]
wiek = stat["wiek_godzin"]

c1, c2, c3 = st.columns(3)
c1.metric("Wpisów w pamięci", stat["wpisow_w_ram"])
c2.metric("Dostawców", stat["dostawcow_w_ram"])
c3.metric("Artykułów (meta)", meta.get("unikalnych_artykulow", "—"))

linia_meta = []
if wiek is not None:
    linia_meta.append(f"snapshot zbudowany {wiek:.1f} h temu")
if meta.get("wierszy_total") is not None:
    linia_meta.append(f"{meta['wierszy_total']} wierszy w źródle")
if meta.get("agent_version"):
    linia_meta.append(f"agent {meta['agent_version']}")
if linia_meta:
    st.caption(" · ".join(linia_meta))

# ostrzeżenie o starych danych
if wiek is not None and wiek > 48:
    st.warning(f"Dane mają {wiek:.0f} h — ktoś powinien odpalić agenta odświeżającego Firestore.")
elif wiek is None:
    st.info("Brak metadanych snapshotu (dokument dostawcy_lookup_meta/snapshot). To nie blokuje lookup-u.")

if st.button("🔄 Odśwież dane z Firestore"):
    try:
        lookup = odswiez_lookup()
        st.success("Przeładowano.")
        st.rerun()
    except Exception as e:
        st.error(f"Nie udało się odświeżyć: {e}")

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
            st.error("❌ Nie znaleziono tej pozycji w cache.")
            st.caption(
                "W docelowej apce taka pozycja trafi do 'dostawcy_lookup_braki' "
                "do ręcznego uzupełnienia przez Dariusza."
            )
            # pokaż podpowiedź: czy w ogóle znamy tego dostawcę
            kanon = lookup._dopasuj_dostawce(nazwa_dostawcy.strip())
            if kanon:
                st.info(f"Dostawcę rozpoznano jako: **{kanon}** — ale tego indeksu u niego nie ma.")
            else:
                st.info("Tego dostawcy w ogóle nie ma w cache (sprawdź pisownię nazwy).")
        else:
            if wynik.dopasowanie == "dokladne":
                st.success(f"✅ Znaleziono (dopasowanie dokładne): **{wynik.indeks_glowny}**")
            else:
                st.success(f"✅ Znaleziono (dopasowanie znormalizowane): **{wynik.indeks_glowny}**")
                st.warning(
                    "Dopasowano przez normalizację (możliwa literówka OCR). "
                    "W docelowej apce ta pozycja dostanie flagę 'do weryfikacji wzrokowej'."
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
                "nie cena z konkretnej faktury."
            )
