"""
Apka OCR-fakturowa Autos.

Dwie zakładki:
  • KROK 1 — Lookup dostawców (walidacja danych + ręczne sprawdzanie pozycji).
  • KROK 2 — Odczyt faktury (OCR przez Gemini Vision na Vertex AI):
      upload PDF/zdjęcia → odczyt pozycji → walidacja arytmetyki →
      lookup indeksu wewnętrznego → tabela do przejrzenia/poprawy →
      eksport CSV dla fakt_filler (indeksDost, ilosc, cena).

Architektura danych lookupu: plik-na-dysku (indeksy_dostawcow.json w repo).
OCR łączy się z Vertex tak jak reszta apek Autos (FIREBASE_CREDS, GCP_PROJECT_IDS).

Uruchomienie:
    pip install -r requirements.txt
    streamlit run app.py
"""
from __future__ import annotations

import io
import csv

import pandas as pd
import streamlit as st

from indeks_lookup import LookupDostawcow

CACHE_PATH = "indeksy_dostawcow.json"

st.set_page_config(page_title="Autos — faktury", page_icon="🔧", layout="centered")


@st.cache_resource(show_spinner="Ładuję dane z pliku...")
def zaladuj_lookup() -> LookupDostawcow:
    """Ładuje lookup raz na sesję (cache_resource = jeden wspólny obiekt w RAM)."""
    return LookupDostawcow(CACHE_PATH)


# ══════════════════════════════════════════════════════════════════════════
#  ŁADOWANIE LOOKUPU (wspólne dla obu zakładek)
# ══════════════════════════════════════════════════════════════════════════
st.title("🔧 Autos — faktury zakupowe")

try:
    lookup = zaladuj_lookup()
except FileNotFoundError:
    st.error(
        f"Nie znaleziono pliku **{CACHE_PATH}**.\n\n"
        "Plik z danymi musi leżeć w tym samym folderze co app.py "
        "(i być commitowany do repo)."
    )
    st.stop()
except Exception as e:
    st.error(f"Nie udało się wczytać danych z {CACHE_PATH}.\n\nSzczegóły: {e}")
    st.stop()


tab_ocr, tab_lookup = st.tabs(["📄 Odczyt faktury (OCR)", "🔍 Lookup dostawców"])


# ══════════════════════════════════════════════════════════════════════════
#  ZAKŁADKA 1 — OCR (KROK 2)
# ══════════════════════════════════════════════════════════════════════════
with tab_ocr:
    st.subheader("Odczyt faktury i przygotowanie do wpisania")
    st.caption(
        "Wrzuć PDF (cyfrowy lub skan) albo zdjęcie faktury. Apka odczyta pozycje przez "
        "Gemini, sprawdzi arytmetykę, dopasuje indeksy wewnętrzne i przygotuje CSV "
        "dla fakt_filler. **Nic nie idzie do maggo automatycznie — Ty zatwierdzasz.**"
    )

    brak_sekretow = [
        k for k in ("FIREBASE_CREDS", "GCP_PROJECT_IDS")
        if k not in st.secrets
    ]
    if brak_sekretow:
        st.warning(
            "OCR wymaga skonfigurowania dostępu do Vertex AI. Brakuje w Secrets: "
            f"**{', '.join(brak_sekretow)}**.\n\n"
            "Dodaj te same wartości, których używają inne apki Autos (Settings → Secrets):\n"
            "- `FIREBASE_CREDS` — JSON service accountu (jako string),\n"
            "- `GCP_PROJECT_IDS` — lista projektów GCP,\n"
            "- `GCP_LOCATION` — np. \"us-central1\" (opcjonalne, domyślnie us-central1)."
        )

    plik = st.file_uploader(
        "Faktura (PDF / JPG / PNG)",
        type=["pdf", "jpg", "jpeg", "png", "webp"],
        accept_multiple_files=False,
    )

    if plik is not None and not brak_sekretow:
        if st.button("📖 Odczytaj fakturę", type="primary"):
            with st.spinner("Gemini czyta fakturę... (kilka–kilkanaście sekund)"):
                try:
                    import vision_ocr
                except Exception as e:
                    st.error(f"Nie udało się załadować modułu OCR: {e}")
                    st.stop()

                wynik, err = vision_ocr.odczytaj_fakture(
                    file_bytes=plik.getvalue(),
                    nazwa_pliku=plik.name,
                    secrets=st.secrets,
                )

            if err:
                st.error(f"❌ {err}")
            else:
                st.session_state["ocr_wynik"] = wynik

    wynik = st.session_state.get("ocr_wynik")
    if wynik is not None:
        st.divider()

        st.markdown("#### Odczytana faktura")
        c1, c2, c3 = st.columns(3)
        c1.metric("Pozycji", wynik.liczba_pozycji)
        c2.metric("Suma netto (pozycje)", f"{wynik.suma_netto_pozycji:,.2f}".replace(",", " ")
                  if wynik.suma_netto_pozycji is not None else "—")
        c3.metric("Suma netto (faktura)", f"{wynik.suma_netto_faktury:,.2f}".replace(",", " ")
                  if wynik.suma_netto_faktury is not None else "—")

        linia = []
        if wynik.dostawca:
            linia.append(f"dostawca: **{wynik.dostawca}**")
        if wynik.numer_dokumentu:
            linia.append(f"dokument: {wynik.numer_dokumentu}")
        if wynik.data:
            linia.append(f"data: {wynik.data}")
        if wynik.waluta:
            linia.append(f"waluta: {wynik.waluta}")
        if linia:
            st.caption(" · ".join(linia))

        # skąd pochodzi odczyt — tekst (pewny) czy OCR (do weryfikacji wzrokiem)
        if getattr(wynik, "zrodlo_odczytu", "") == "tekst PDF":
            st.success("📝 Odczytano z **warstwy tekstowej PDF** (dokładne — bez OCR, bez zgadywania).")
        elif getattr(wynik, "zrodlo_odczytu", "") == "OCR (vision)":
            st.info("👁️ Odczytano przez **OCR (Gemini Vision)** — to skan/zdjęcie. Zerknij na pozycje, bo OCR bywa omylny.")

        if wynik.suma_ok is True:
            st.success("✅ Suma pozycji zgadza się z sumą na fakturze.")
        elif wynik.suma_ok is False:
            st.error(
                "⚠️ Suma pozycji NIE zgadza się z sumą na fakturze. "
                "Sprawdź wiersze oznaczone poniżej, zanim cokolwiek wpiszesz."
            )

        for o in wynik.ostrzezenia:
            st.warning(o)

        wiersze = []
        nieznane = 0
        for p in wynik.pozycje:
            res = lookup.znajdz(wynik.dostawca, p.indeks) if wynik.dostawca else None
            if res is None:
                indeks_wew = ""
                dop = "BRAK"
                nieznane += 1
            else:
                indeks_wew = res.indeks_glowny
                dop = res.dopasowanie

            wiersze.append({
                "lp": p.lp,
                "indeks_dostawcy": p.indeks,
                "ilosc": p.ilosc,
                "cena": p.cena_netto,
                "indeks_wewnetrzny": indeks_wew,
                "dopasowanie": dop,
                "arytmetyka": ("OK" if p.arytmetyka_ok else "BŁĄD") if p.arytmetyka_ok is not None else "—",
                "uwaga": p.uwaga,
            })

        df = pd.DataFrame(wiersze)

        dokladne = (df["dopasowanie"] == "dokladne").sum()
        znorm = (df["dopasowanie"] == "znormalizowane").sum()
        st.markdown("#### Pozycje + dopasowanie indeksów")
        st.caption(
            f"Dopasowano dokładnie: **{dokladne}** · przez normalizację (sprawdź wzrokowo): "
            f"**{znorm}** · nieznane (do uzupełnienia ręcznie): **{nieznane}**"
        )
        if znorm:
            st.warning(
                "Pozycje 'znormalizowane' dopasowano mimo drobnej różnicy w zapisie "
                "(możliwa literówka OCR) — zerknij, czy indeks wewnętrzny się zgadza."
            )
        if nieznane:
            st.info(
                f"{nieznane} pozycji nie ma dopasowania u tego dostawcy. Możesz wpisać "
                "indeks wewnętrzny ręcznie w tabeli poniżej (kolumna 'indeks_wewnetrzny')."
            )

        st.caption(
            "Możesz poprawić każdą wartość w tabeli (ilość, cena, indeks wewnętrzny). "
            "Kolumna 'Arytm.' bywa pusta ('—'), gdy faktura nie ma osobnej kolumny wartości "
            "per wiersz (jak Inter Cars) — wtedy sprawdzana jest sama suma całej faktury."
        )
        df_edit = st.data_editor(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "lp": st.column_config.NumberColumn("Lp", disabled=True, width="small"),
                "indeks_dostawcy": st.column_config.TextColumn("Indeks dostawcy"),
                "ilosc": st.column_config.NumberColumn("Ilość"),
                "cena": st.column_config.NumberColumn("Cena netto", format="%.2f"),
                "indeks_wewnetrzny": st.column_config.TextColumn(
                    "Indeks wewnętrzny", help="To trafi do maggo. Możesz uzupełnić ręcznie."),
                "dopasowanie": st.column_config.TextColumn("Dopasowanie", disabled=True),
                "arytmetyka": st.column_config.TextColumn("Arytm.", disabled=True, width="small"),
                "uwaga": st.column_config.TextColumn("Uwaga"),
            },
            key="ocr_tabela",
        )

        st.divider()
        st.markdown("#### Eksport dla fakt_filler")

        gotowe = df_edit[df_edit["indeks_wewnetrzny"].astype(str).str.strip() != ""].copy()
        bez_indeksu = len(df_edit) - len(gotowe)

        st.caption(
            f"Do CSV trafi **{len(gotowe)}** pozycji z wypełnionym indeksem wewnętrznym."
            + (f" Pominięto **{bez_indeksu}** bez indeksu — uzupełnij je w tabeli, jeśli mają wejść."
               if bez_indeksu else "")
        )

        if len(gotowe):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["indeksDost", "ilosc", "cena"])
            for _, r in gotowe.iterrows():
                cena = r["cena"]
                ilosc = r["ilosc"]
                w.writerow([
                    str(r["indeks_wewnetrzny"]).strip(),
                    "" if pd.isna(ilosc) else (int(ilosc) if float(ilosc).is_integer() else ilosc),
                    "" if pd.isna(cena) else f"{float(cena):.2f}",
                ])
            csv_bytes = buf.getvalue().encode("utf-8")

            nazwa_csv = f"pozycje_{(wynik.numer_dokumentu or 'faktura').replace('/', '_')}.csv"
            st.download_button(
                "⬇️ Pobierz CSV dla fakt_filler",
                data=csv_bytes,
                file_name=nazwa_csv,
                mime="text/csv",
                type="primary",
            )
            st.caption(
                "Ten CSV wgrywasz lokalnie przez fakt_filler.py (Playwright otwiera maggo, "
                "Ty się logujesz, skrypt wpisuje pozycje). Apka w chmurze nie ma dostępu do maggo."
            )

        with st.expander("🐛 Surowa odpowiedź modelu (debug)"):
            st.caption(f"Model: {wynik.model}")
            st.code(wynik.raw_odpowiedz, language="json")


# ══════════════════════════════════════════════════════════════════════════
#  ZAKŁADKA 2 — LOOKUP (KROK 1, bez zmian merytorycznych)
# ══════════════════════════════════════════════════════════════════════════
with tab_lookup:
    st.subheader("Lookup dostawców")
    st.caption("Ręczne sprawdzanie: dostawca + indeks z faktury → indeks wewnętrzny.")

    meta = lookup._cache.get("_meta", {})
    wiek = lookup.wiek_cache_godzin()

    c1, c2, c3 = st.columns(3)
    c1.metric("Wpisów (źródło)", meta.get("wierszy_uwzglednionych", meta.get("wierszy_pobranych", "—")))
    c2.metric("Dostawców", meta.get("unikalnych_dostawcow", len(lookup.lista_dostawcow())))
    c3.metric("Artykułów", meta.get("unikalnych_artykulow", "—"))

    linia = []
    if wiek is not None:
        if wiek < 48:
            linia.append(f"dane zbudowane {wiek:.1f} h temu")
        else:
            linia.append(f"dane zbudowane {wiek / 24:.0f} dni temu")
    if meta.get("zrodlo"):
        linia.append(f"źródło: {meta['zrodlo']}")
    if meta.get("duplikaty_pominiete") is not None:
        linia.append(f"duplikatów pominięto: {meta['duplikaty_pominiete']}")
    if meta.get("konflikty_normalizacji"):
        linia.append(f"konfliktów normalizacji: {meta['konflikty_normalizacji']}")
    if linia:
        st.caption(" · ".join(linia))

    if meta.get("konflikty_normalizacji"):
        st.info(
            f"W danych jest {meta['konflikty_normalizacji']} konflikt(ów) normalizacji "
            "(ten sam znormalizowany indeks → różne artykuły u tego samego dostawcy). "
            "Dla tych przypadków zadziała tylko dopasowanie dokładne — fallback wyłączony."
        )

    st.divider()
    with st.form("lookup_form"):
        nazwa_dostawcy = st.text_input("Nazwa dostawcy (jak na fakturze)", placeholder="np. INTER CARS S.A.")
        indeks = st.text_input("Indeks z faktury", placeholder="np. N.40000.S05.H100")
        szukaj = st.form_submit_button("Szukaj", type="primary")

    if szukaj:
        if not nazwa_dostawcy.strip() or not indeks.strip():
            st.warning("Podaj nazwę dostawcy i indeks.")
        else:
            res = lookup.znajdz(nazwa_dostawcy.strip(), indeks.strip())
            if res is None:
                st.error("❌ Nie znaleziono tej pozycji.")
                kanon = lookup._dopasuj_dostawce(nazwa_dostawcy.strip())
                if kanon:
                    st.info(f"Dostawcę rozpoznano jako: **{kanon}** — ale tego indeksu u niego nie ma.")
                else:
                    st.info("Tego dostawcy w ogóle nie ma w danych (sprawdź pisownię).")
            else:
                if res.dopasowanie == "dokladne":
                    st.success(f"✅ Znaleziono (dopasowanie dokładne): **{res.indeks_glowny}**")
                else:
                    st.success(f"✅ Znaleziono (dopasowanie znormalizowane): **{res.indeks_glowny}**")
                    st.warning("Dopasowano przez normalizację (możliwa literówka OCR).")

                kol1, kol2 = st.columns(2)
                with kol1:
                    st.write("**Indeks wewnętrzny:**", res.indeks_glowny)
                    st.write("**Nazwa:**", res.nazwa)
                    st.write("**Cena netto (katalogowa):**", res.cena_netto)
                    st.write("**Cena brutto:**", res.cena_brutto)
                with kol2:
                    st.write("**Waluta ID:**", res.waluta_id)
                    st.write("**Wiodący dostawca:**", "tak" if res.czy_glowny else "nie")
                    st.write("**Data aktualizacji ceny:**", res.data_aktualizacji)

                st.caption(
                    "Uwaga: cena to wartość KATALOGOWA z bazy (do identyfikacji pozycji), "
                    "nie cena z konkretnej faktury — i bywa stara (2022-2025)."
                )

    st.divider()
    with st.expander(f"Lista dostawców w danych ({len(lookup.lista_dostawcow())})"):
        for d in lookup.lista_dostawcow():
            st.write("·", d)
