"""
Apka OCR-fakturowa Autos.

Zakładki:
  • KROK 2 — Odczyt faktury (OCR/tekst): upload -> odczyt pozycji -> walidacja ->
    lookup indeksu wewnętrznego -> tabela -> obsługa pozycji BRAK (usuń / dopasuj) ->
    eksport CSV dla fakt_filler.
  • KROK 1 — Lookup dostawców (ręczne sprawdzanie pozycji).

Trwała pamięć decyzji (czarna lista + wyuczone dopasowania) w Firestore.
Lookup: plik-na-dysku (indeksy_dostawcow.json w repo).
OCR/tekst: digital-first (tekst PDF), OCR vision jako fallback (Vertex AI).
"""
from __future__ import annotations

import io
import csv

import pandas as pd
import streamlit as st

from indeks_lookup import LookupDostawcow
import pamiec_dopasowan as pdop

CACHE_PATH = "indeksy_dostawcow.json"


def _wczytaj_plik_repo(nazwa: str):
    """
    Czyta plik z repo (obok app.py) jako bajty — do udostępnienia przez download_button.
    Zwraca bytes albo None, gdy pliku nie ma (wtedy przycisk po prostu się nie pokaże).
    """
    try:
        with open(nazwa, "rb") as f:
            return f.read()
    except Exception:
        return None

st.set_page_config(page_title="Autos — faktury", page_icon="🔧", layout="wide")


@st.cache_resource(show_spinner="Ładuję dane z pliku...")
def zaladuj_lookup() -> LookupDostawcow:
    return LookupDostawcow(CACHE_PATH)


@st.cache_resource(show_spinner="Łączę z Firestore...")
def polacz_firestore():
    """Zwraca (db, blad). Cache_resource = jedno połączenie na sesję."""
    if "FIREBASE_CREDS" not in st.secrets:
        return None, "Brak FIREBASE_CREDS w Secrets — pamięć dopasowań wyłączona."
    return pdop.init_firestore(st.secrets)



def _wiersz(p, indeks_wew: str, dopasowanie: str, kod_producenta: str = "") -> dict:
    """Buduje słownik jednego wiersza tabeli z pozycji odczytanej faktury."""
    return {
        "lp": p.lp,
        "indeks_dostawcy": p.indeks,
        "ilosc": p.ilosc,
        "cena": p.cena_netto,
        "indeks_wewnetrzny": indeks_wew,
        "kod_producenta": kod_producenta,
        "dopasowanie": dopasowanie,
        "arytmetyka": ("OK" if p.arytmetyka_ok else "BŁĄD") if p.arytmetyka_ok is not None else "—",
        "uwaga": p.uwaga,
    }

st.title("🔧 Autos — faktury zakupowe")

try:
    lookup = zaladuj_lookup()
except FileNotFoundError:
    st.error(f"Nie znaleziono pliku **{CACHE_PATH}** (musi być w repo obok app.py).")
    st.stop()
except Exception as e:
    st.error(f"Nie udało się wczytać danych z {CACHE_PATH}.\n\nSzczegóły: {e}")
    st.stop()

# Firestore — opcjonalny; bez niego apka działa, tylko bez trwałej pamięci
db, blad_fs = polacz_firestore()


# ── pomocnicze: wczytanie pamięci (czarna lista + dopasowania) z cache sesji ──
def odswiez_pamiec():
    """Ładuje czarną listę i dopasowania do session_state (raz, z możliwością odświeżenia)."""
    if db is None:
        st.session_state["_czarna"] = set()
        st.session_state["_dopas"] = {}
        return
    st.session_state["_czarna"] = pdop.wczytaj_czarna_liste(db)
    st.session_state["_dopas"] = pdop.wczytaj_dopasowania(db)


if "_czarna" not in st.session_state or "_dopas" not in st.session_state:
    odswiez_pamiec()


tab_ocr, tab_lookup, tab_pamiec, tab_pomoc = st.tabs(
    ["📄 Odczyt faktury", "🔍 Lookup dostawców", "🧠 Pamięć dopasowań", "❓ Jak to uruchomić"]
)


# ══════════════════════════════════════════════════════════════════════════
#  ZAKŁADKA 1 — ODCZYT FAKTURY
# ══════════════════════════════════════════════════════════════════════════
with tab_ocr:
    st.subheader("Odczyt faktury i przygotowanie do wpisania")
    st.caption(
        "PDF cyfrowy czytany z tekstu (dokładnie), skan/zdjęcie przez OCR. Apka sprawdza "
        "arytmetykę, dopasowuje indeksy wewnętrzne i przygotowuje CSV dla fakt_filler. "
        "**Nic nie idzie do maggo automatycznie — Ty zatwierdzasz.**"
    )

    if blad_fs:
        st.warning(f"⚠️ {blad_fs} Akcje 'usuń trwale' i 'zapamiętaj dopasowanie' nie zadziałają, "
                   "dopóki nie dodasz FIREBASE_CREDS do Secrets.")

    brak_sekretow = [k for k in ("FIREBASE_CREDS", "GCP_PROJECT_IDS") if k not in st.secrets]
    if brak_sekretow:
        st.info(
            "Do OCR skanów potrzebny Vertex AI. Brakuje w Secrets: "
            f"**{', '.join(brak_sekretow)}**. Cyfrowe PDF (z tekstem) zadziałają i bez tego."
        )

    plik = st.file_uploader("Faktura (PDF / JPG / PNG)",
                            type=["pdf", "jpg", "jpeg", "png", "webp"],
                            accept_multiple_files=False)

    if plik is not None:
        if st.button("📖 Odczytaj fakturę", type="primary"):
            # nowy odczyt — czyścimy stan poprzedniego (decyzje per-pozycja)
            for k in list(st.session_state.keys()):
                if k.startswith("akcja_") or k.startswith("szukaj_") or k == "ocr_wynik" or k == "reczne_dopas":
                    del st.session_state[k]
            st.session_state["reczne_dopas"] = {}  # lp -> (indeks_wew, nazwa) wybrane w tej sesji

            with st.spinner("Czytam fakturę..."):
                try:
                    import vision_ocr
                except Exception as e:
                    st.error(f"Nie udało się załadować modułu odczytu: {e}")
                    st.stop()
                wynik, err = vision_ocr.odczytaj_fakture(
                    file_bytes=plik.getvalue(), nazwa_pliku=plik.name, secrets=st.secrets)
            if err:
                st.error(f"❌ {err}")
            else:
                st.session_state["ocr_wynik"] = wynik

    wynik = st.session_state.get("ocr_wynik")
    if wynik is not None:
        st.divider()

        # nagłówek
        st.markdown("#### Odczytana faktura")
        c1, c2, c3 = st.columns(3)
        c1.metric("Pozycji (odczytane)", wynik.liczba_pozycji)
        c2.metric("Suma netto (pozycje)", f"{wynik.suma_netto_pozycji:,.2f}".replace(",", " ")
                  if wynik.suma_netto_pozycji is not None else "—")
        c3.metric("Suma netto (faktura)", f"{wynik.suma_netto_faktury:,.2f}".replace(",", " ")
                  if wynik.suma_netto_faktury is not None else "—")

        linia = []
        if wynik.dostawca: linia.append(f"dostawca: **{wynik.dostawca}**")
        if wynik.numer_dokumentu: linia.append(f"dokument: {wynik.numer_dokumentu}")
        if wynik.data: linia.append(f"data: {wynik.data}")
        if wynik.waluta: linia.append(f"waluta: {wynik.waluta}")
        if linia: st.caption(" · ".join(linia))

        if getattr(wynik, "zrodlo_odczytu", "") == "tekst PDF":
            st.success("📝 Odczytano z **warstwy tekstowej PDF** (dokładne — bez OCR).")
        elif getattr(wynik, "zrodlo_odczytu", "") == "OCR (vision)":
            st.info("👁️ Odczytano przez **OCR (Gemini Vision)** — skan/zdjęcie. Zerknij na pozycje.")

        if wynik.suma_ok is True:
            st.success("✅ Suma pozycji zgadza się z sumą na fakturze.")
        elif wynik.suma_ok is False:
            st.error("⚠️ Suma pozycji NIE zgadza się z sumą na fakturze — sprawdź wiersze niżej.")
        for o in wynik.ostrzezenia:
            st.warning(o)

        # ── przetwarzanie pozycji: czarna lista -> wyuczone -> lookup ──
        czarna = st.session_state.get("_czarna", set())
        dopas = st.session_state.get("_dopas", {})
        reczne = st.session_state.get("reczne_dopas", {})   # lp -> (indeks_wew, nazwa)
        dostawca = wynik.dostawca or ""

        # Pomocniczo: znajdź kod producenta dla danego indeksu wewnętrznego.
        # Dla pozycji rozwiązanych przez lookup mamy kod wprost. Dla wyuczonych/
        # ręcznych (pamięć zna tylko indeks główny) próbujemy go odszukać w cache:
        # bierzemy kod producenta z dowolnego wpisu o tym samym indeksie głównym
        # u TEGO dostawcy, a jak nie ma — u kogokolwiek.
        def kod_producenta_dla_indeksu(indeks_glowny: str) -> str:
            if not indeks_glowny:
                return ""
            cache_dokl = lookup._cache.get("po_dostawcy_dokladnie", {})
            # najpierw u dostawcy z faktury
            for dost in ([dostawca] if dostawca else []) + list(cache_dokl.keys()):
                for wpis in cache_dokl.get(dost, {}).values():
                    if wpis.get("indeks_glowny") == indeks_glowny:
                        kp = (wpis.get("kod_producenta") or "").strip()
                        if kp:
                            return kp
            return ""

        wiersze = []
        pozycje_brak = []   # pozycje wymagające decyzji (BRAK i jeszcze nieobsłużone)
        pominiete_czarna = 0

        for p in wynik.pozycje:
            # 1) czarna lista -> pomiń całkowicie
            if dostawca and pdop.na_czarnej_liscie(czarna, dostawca, p.indeks):
                pominiete_czarna += 1
                continue

            # 2) ręczny wybór w tej sesji (kliknięte "użyj")
            if p.lp in reczne:
                iw, nz = reczne[p.lp]
                wiersze.append(_wiersz(p, iw, "ręczne (ta sesja)", kod_producenta_dla_indeksu(iw)))
                continue

            # 3) wyuczone dopasowanie z Firestore
            if dostawca:
                iw = pdop.pobierz_wyuczone(dopas, dostawca, p.indeks)
                if iw:
                    wiersze.append(_wiersz(p, iw, "wyuczone", kod_producenta_dla_indeksu(iw)))
                    continue

            # 4) zwykły lookup
            res = lookup.znajdz(dostawca, p.indeks) if dostawca else None
            if res is None:
                wiersze.append(_wiersz(p, "", "BRAK", ""))
                pozycje_brak.append(p)
            else:
                wiersze.append(_wiersz(p, res.indeks_glowny, res.dopasowanie,
                                       getattr(res, "kod_producenta", "")))

        df = pd.DataFrame(wiersze)

        # podsumowanie
        dokladne = (df["dopasowanie"] == "dokladne").sum() if len(df) else 0
        znorm = (df["dopasowanie"] == "znormalizowane").sum() if len(df) else 0
        wyu = (df["dopasowanie"].isin(["wyuczone", "ręczne (ta sesja)"])).sum() if len(df) else 0
        brak_n = (df["dopasowanie"] == "BRAK").sum() if len(df) else 0

        st.markdown("#### Pozycje + dopasowanie indeksów")
        info_bits = [f"dokładne: **{dokladne}**", f"znormalizowane: **{znorm}**",
                     f"wyuczone/ręczne: **{wyu}**", f"BRAK: **{brak_n}**"]
        if pominiete_czarna:
            info_bits.append(f"pominięte (czarna lista): **{pominiete_czarna}**")
        st.caption(" · ".join(info_bits))

        st.caption(
            "Tabela jest poglądowa (możesz w niej poprawić wartości). Pozycje **BRAK** "
            "obsłuż w sekcji pod tabelą — usuń trwale albo dopasuj podobną z bazy."
        )
        df_edit = st.data_editor(
            df, use_container_width=True, hide_index=True,
            column_config={
                "lp": st.column_config.NumberColumn("Lp", disabled=True, width="small"),
                "indeks_dostawcy": st.column_config.TextColumn("Indeks dostawcy"),
                "ilosc": st.column_config.NumberColumn("Ilość"),
                "cena": st.column_config.NumberColumn("Cena netto", format="%.2f"),
                "indeks_wewnetrzny": st.column_config.TextColumn("Indeks wewn. (nasz)", disabled=True),
                "kod_producenta": st.column_config.TextColumn(
                    "Kod producenta (→ maggo)",
                    help="Ta wartość jest wysyłana do maggo. Możesz ją poprawić ręcznie."),
                "dopasowanie": st.column_config.TextColumn("Dopasowanie", disabled=True),
                "arytmetyka": st.column_config.TextColumn("Arytm.", disabled=True, width="small"),
                "uwaga": st.column_config.TextColumn("Uwaga"),
            },
            column_order=["lp", "indeks_dostawcy", "ilosc", "cena",
                          "indeks_wewnetrzny", "kod_producenta", "dopasowanie",
                          "arytmetyka", "uwaga"],
            key="ocr_tabela",
        )

        # ══ sekcja decyzji dla pozycji BRAK ══
        if pozycje_brak:
            st.divider()
            st.markdown(f"#### ⚠️ Pozycje bez dopasowania ({len(pozycje_brak)}) — wymagają decyzji")
            st.caption(
                "Dla każdej: **Usuń z listy** (trwale — już nigdy się nie pojawi, nie wejdzie do CSV) "
                "albo **Szukaj podobnych** (przeszukuje całą bazę, literówki/fragmenty; po wyborze "
                "zapamiętuje na stałe)."
            )

            for p in pozycje_brak:
                with st.container(border=True):
                    cl, cr = st.columns([3, 2])
                    with cl:
                        st.markdown(f"**Lp {p.lp}** · indeks dostawcy: `{p.indeks}`")
                        st.caption(f"ilość: {p.ilosc} · cena netto: {p.cena_netto}")
                    with cr:
                        b1, b2 = st.columns(2)
                        # USUŃ TRWALE
                        if b1.button("✖ Usuń z listy", key=f"akcja_usun_{p.lp}",
                                     help="Trwale — nie pojawi się w przyszłych odczytach",
                                     use_container_width=True):
                            if db is None:
                                st.error("Brak Firestore — nie mogę zapisać trwale.")
                            else:
                                ok = pdop.dodaj_do_czarnej_listy(db, dostawca, p.indeks)
                                if ok:
                                    odswiez_pamiec()
                                    st.success(f"Usunięto trwale: {p.indeks}")
                                    st.rerun()
                                else:
                                    st.error("Nie udało się zapisać.")
                        # SZUKAJ PODOBNYCH
                        if b2.button("🔍 Szukaj podobnych", key=f"szukaj_btn_{p.lp}",
                                     use_container_width=True):
                            st.session_state[f"szukaj_open_{p.lp}"] = True

                    # rozwinięcie: kandydaci
                    if st.session_state.get(f"szukaj_open_{p.lp}"):
                        kandydaci = pdop.szukaj_podobnych(lookup, p.indeks, maks=8)
                        if not kandydaci:
                            st.info("Nie znalazłem podobnych w bazie. Wpisz indeks wewnętrzny "
                                    "ręcznie w tabeli wyżej, jeśli ta pozycja ma wejść do CSV.")
                        else:
                            st.caption(f"Znalezione podobne dla `{p.indeks}` (od najlepszego):")
                            for i, k in enumerate(kandydaci):
                                kc1, kc2 = st.columns([5, 1])
                                with kc1:
                                    st.markdown(
                                        f"**{k.indeks_wewnetrzny}** — {k.nazwa or '(bez nazwy)'}  \n"
                                        f"<small>{k.powod} · u dostawcy *{k.dostawca_zrodlowy}* "
                                        f"jako `{k.indeks_dostawcy}`</small>",
                                        unsafe_allow_html=True)
                                with kc2:
                                    if st.button("✓ Użyj", key=f"uzyj_{p.lp}_{i}",
                                                 use_container_width=True):
                                        # zapamiętaj na stałe + zastosuj w tej sesji
                                        if db is not None:
                                            pdop.zapisz_dopasowanie(
                                                db, dostawca, p.indeks,
                                                k.indeks_wewnetrzny, k.nazwa)
                                            odswiez_pamiec()
                                        st.session_state.setdefault("reczne_dopas", {})[p.lp] = \
                                            (k.indeks_wewnetrzny, k.nazwa)
                                        st.session_state[f"szukaj_open_{p.lp}"] = False
                                        st.success(f"Przypisano {k.indeks_wewnetrzny} do Lp {p.lp} "
                                                   "i zapamiętano na stałe.")
                                        st.rerun()

        # ══ panel: WYŚLIJ DO MAGGO ══
        st.divider()
        st.markdown("### 📤 Wyślij do maggo")

        # Do maggo idzie KOD PRODUCENTA (maggo dopasowuje pozycję po parze
        # dostawca + kod producenta). Pozycja jest "gotowa" tylko gdy ma kod producenta.
        gotowe = df_edit[df_edit["kod_producenta"].astype(str).str.strip() != ""].copy()
        bez_kodu = len(df_edit) - len(gotowe)

        # podsumowanie liczbowe
        m1, m2, m3 = st.columns(3)
        m1.metric("Gotowe do wpisania", len(gotowe))
        m2.metric("Bez kodu producenta", bez_kodu)
        suma_gotowych = 0.0
        for _, r in gotowe.iterrows():
            try:
                suma_gotowych += float(r["cena"]) * float(r["ilosc"])
            except Exception:
                pass
        m3.metric("Wartość netto (gotowe)", f"{suma_gotowych:,.2f}".replace(",", " "))

        if bez_kodu:
            st.warning(
                f"⚠️ **{bez_kodu}** pozycji nie ma **kodu producenta** i NIE zostanie wpisanych "
                "(maggo dopasowuje pozycję właśnie po kodzie producenta). Uzupełnij kod w kolumnie "
                "**„Kod producenta (→ maggo)”** w tabeli wyżej, obsłuż pozycję (usuń / dopasuj), "
                "albo wpisz ją w maggo ręcznie."
            )

        if len(gotowe):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["indeksDost", "ilosc", "cena"])
            for _, r in gotowe.iterrows():
                cena, ilosc = r["cena"], r["ilosc"]
                w.writerow([
                    str(r["kod_producenta"]).strip(),   # <-- KOD PRODUCENTA idzie do maggo
                    "" if pd.isna(ilosc) else (int(ilosc) if float(ilosc).is_integer() else ilosc),
                    "" if pd.isna(cena) else f"{float(cena):.2f}",
                ])
            nazwa_csv = f"pozycje_{(wynik.numer_dokumentu or 'faktura').replace('/', '_')}.csv"

            kol_a, kol_b = st.columns([1, 1])
            with kol_a:
                st.download_button(
                    f"⬇️ Pobierz plik ({len(gotowe)} poz.)",
                    data=buf.getvalue().encode("utf-8"),
                    file_name=nazwa_csv, mime="text/csv", type="primary",
                    use_container_width=True,
                )
            with kol_b:
                st.caption(f"Plik: **{nazwa_csv}** · do maggo trafia **kod producenta**")

            st.markdown(
                "##### Co teraz zrobić — 3 kroki:\n"
                "1. **Pobierz plik** przyciskiem powyżej (zapamiętaj gdzie się zapisał).\n"
                "2. Otwórz **maggo** → wejdź na **tę fakturę** → zakładka **Pozycje** "
                "(tak, żeby był widoczny przycisk *Dodaj*).\n"
                "3. Na pulpicie kliknij dwa razy ikonę **Wpisz do maggo** → w oknie, które się "
                "otworzy, zaloguj się (jeśli trzeba), wróć i naciśnij **Enter** → wskaż **pobrany "
                "przed chwilą plik**. Pozycje wpiszą się same."
            )
            st.caption(
                "ℹ️ Wpisywanie odbywa się na Twoim komputerze (apka w chmurze nie ma dostępu do "
                "maggo). Każda osoba robi to u siebie, ze swojego konta maggo."
            )
        else:
            st.info("Brak pozycji gotowych do wpisania (żadna nie ma indeksu wewnętrznego).")

        with st.expander("🐛 Surowa odpowiedź modelu (debug)"):
            st.caption(f"Źródło: {getattr(wynik, 'zrodlo_odczytu', '?')} · Model: {wynik.model}")
            st.code(wynik.raw_odpowiedz, language="json")

    # ── sekcja: pliki do lokalnego wpisywania (widoczna zawsze) ──
    st.divider()
    with st.expander("🔧 Pliki do wpisywania (pierwsze uruchomienie na nowym komputerze)"):
        st.caption(
            "Żeby wpisywać faktury do maggo z danego komputera, trzeba **raz** pobrać tu poniższe "
            "pliki, położyć je razem w jednym folderze i wykonać instalację z instrukcji. "
            "Potem przy każdej fakturze to już tylko klikanie."
        )

        pliki = [
            ("fakt_filler.py", "fakt_filler.py", "text/x-python", "Silnik wpisywania"),
            ("Wpisz_do_maggo.bat", "Wpisz_do_maggo.bat", "application/octet-stream",
             "Ikona do dwukliku — uruchamia wpisywanie"),
            ("Wpisz_do_maggo_PROBA.bat", "Wpisz_do_maggo_PROBA.bat", "application/octet-stream",
             "Wersja PRÓBNA — nic nie wpisuje (do testu)"),
            ("INSTRUKCJA_pierwsze_uruchomienie.txt", "INSTRUKCJA_pierwsze_uruchomienie.txt",
             "text/plain", "Instrukcja krok po kroku"),
        ]

        brakujace = []
        for nazwa_pliku, nazwa_pobrania, mime, opis in pliki:
            dane = _wczytaj_plik_repo(nazwa_pliku)
            cda, cdb = st.columns([1, 2])
            with cda:
                if dane is not None:
                    st.download_button(f"⬇️ {nazwa_pobrania}", data=dane,
                                       file_name=nazwa_pobrania, mime=mime,
                                       use_container_width=True, key=f"dl_{nazwa_pliku}")
                else:
                    st.button(f"⬇️ {nazwa_pobrania}", disabled=True,
                              use_container_width=True, key=f"dl_{nazwa_pliku}")
                    brakujace.append(nazwa_pliku)
            cdb.caption(opis)

        if brakujace:
            st.warning(
                "Niektóre pliki nie są jeszcze w repo (przyciski nieaktywne): "
                f"**{', '.join(brakujace)}**. Wgraj je do repo obok app.py, aby dało się je pobrać."
            )

        st.markdown(
            "##### Instalacja (raz na komputer):\n"
            "1. Pobierz **wszystkie 4 pliki powyżej** i wrzuć je razem do jednego folderu "
            "(np. na pulpicie folder „Maggo wpisywanie”).\n"
            "2. Zainstaluj **Python** z python.org (przy instalacji zaznacz *Add to PATH*), zrestartuj komputer.\n"
            "3. Otwórz **PowerShell** i wpisz:\n"
            "   - `pip install playwright requests`\n"
            "   - `python -m playwright install chromium`\n"
            "4. Gotowe. Od teraz: pobierz CSV z apki → otwórz fakturę w maggo (zakładka Pozycje) → "
            "dwuklik w **Wpisz_do_maggo.bat** → wskaż pobrany CSV.\n\n"
            "Szczegóły i rozwiązywanie problemów są w pobranej instrukcji."
        )


# ══════════════════════════════════════════════════════════════════════════
#  ZAKŁADKA 2 — LOOKUP
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
        linia.append(f"dane zbudowane {wiek:.1f} h temu" if wiek < 48 else f"dane zbudowane {wiek/24:.0f} dni temu")
    if meta.get("zrodlo"): linia.append(f"źródło: {meta['zrodlo']}")
    if meta.get("konflikty_normalizacji"): linia.append(f"konfliktów normalizacji: {meta['konflikty_normalizacji']}")
    if linia: st.caption(" · ".join(linia))

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
                etykieta = "dokładne" if res.dopasowanie == "dokladne" else "znormalizowane"
                st.success(f"✅ Znaleziono (dopasowanie {etykieta}): **{res.indeks_glowny}**")
                if res.dopasowanie != "dokladne":
                    st.warning("Dopasowano przez normalizację (możliwa literówka).")
                kol1, kol2 = st.columns(2)
                with kol1:
                    st.write("**Indeks wewnętrzny:**", res.indeks_glowny)
                    st.write("**Nazwa:**", res.nazwa)
                    st.write("**Cena netto (katalogowa):**", res.cena_netto)
                with kol2:
                    st.write("**Wiodący dostawca:**", "tak" if res.czy_glowny else "nie")
                    st.write("**Data aktualizacji ceny:**", res.data_aktualizacji)

    st.divider()
    with st.expander(f"Lista dostawców w danych ({len(lookup.lista_dostawcow())})"):
        for d in lookup.lista_dostawcow():
            st.write("·", d)


# ══════════════════════════════════════════════════════════════════════════
#  ZAKŁADKA 3 — PAMIĘĆ DOPASOWAŃ (zarządzanie czarną listą i wyuczonymi)
# ══════════════════════════════════════════════════════════════════════════
with tab_pamiec:
    st.subheader("Pamięć dopasowań")
    st.caption("Trwałe decyzje zapisane w Firestore: odrzucone pozycje (czarna lista) "
               "oraz ręcznie nauczone dopasowania. Tu możesz je przejrzeć i cofnąć.")

    if db is None:
        st.warning(f"⚠️ {blad_fs or 'Brak połączenia z Firestore.'}")
    else:
        if st.button("🔄 Odśwież z Firestore"):
            odswiez_pamiec()
            st.rerun()

        czarna_docs = []
        dopas = st.session_state.get("_dopas", {})
        try:
            for d in db.collection(pdop._COL_CZARNA).stream():
                czarna_docs.append(d.to_dict())
        except Exception as e:
            st.error(f"Błąd odczytu czarnej listy: {e}")

        st.markdown(f"##### ✖ Czarna lista — odrzucone pozycje ({len(czarna_docs)})")
        if not czarna_docs:
            st.caption("(pusto)")
        else:
            for w in czarna_docs:
                cc1, cc2 = st.columns([5, 1])
                cc1.write(f"`{w.get('indeks_dostawcy','?')}` · dostawca: {w.get('dostawca','?')}")
                if cc2.button("Cofnij", key=f"cofnij_cz_{w.get('indeks_dostawcy','')}_{w.get('dostawca','')}"):
                    pdop.usun_z_czarnej_listy(db, w.get("dostawca",""), w.get("indeks_dostawcy",""))
                    odswiez_pamiec()
                    st.rerun()

        st.divider()
        st.markdown(f"##### 🧠 Wyuczone dopasowania ({len(dopas)})")
        if not dopas:
            st.caption("(pusto)")
        else:
            rows = []
            for k, w in dopas.items():
                rows.append({
                    "indeks dostawcy": w.get("indeks_dostawcy", "?"),
                    "→ indeks wewnętrzny": w.get("indeks_wewnetrzny", "?"),
                    "nazwa": w.get("nazwa", ""),
                    "dostawca": w.get("dostawca", "?"),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            st.caption("Aby cofnąć konkretne dopasowanie — usuń je w konsoli Firebase "
                       "(kolekcja faktury_dopasowania) lub poproś o przycisk usuwania per wiersz.")


# ══════════════════════════════════════════════════════════════════════════
#  ZAKŁADKA 4 — JAK TO URUCHOMIĆ (instrukcja dla laika, klik po kliku)
# ══════════════════════════════════════════════════════════════════════════
with tab_pomoc:
    st.subheader("Jak uruchomić wpisywanie faktur na swoim komputerze")
    st.caption(
        "Tę konfigurację robisz **raz**. Zajmuje ~15 minut. Potem przy każdej fakturze "
        "to już tylko parę kliknięć. Jeśli na którymś kroku coś pójdzie nie tak — zrób zrzut "
        "ekranu i poproś o pomoc."
    )

    st.info(
        "💡 **Zanim zaczniesz** — pobierz 4 pliki z zakładki „📄 Odczyt faktury” "
        "(na dole, sekcja „🔧 Pliki do wpisywania”) i wrzuć je **razem** do jednego folderu. "
        "Najprościej: na pulpicie kliknij prawym → Nowy → Folder, nazwij go np. **Maggo**, "
        "i przeciągnij do niego wszystkie 4 pobrane pliki."
    )

    st.divider()

    # ── CZĘŚĆ 1: PYTHON ──
    st.markdown("## Część 1 — Instalacja Pythona (raz)")
    st.markdown(
        "Python to program, dzięki któremu działa wpisywanie. Trzeba go zainstalować tylko raz.\n\n"
        "**Krok 1.1** — Wejdź na stronę **python.org/downloads** "
        "(wpisz to w pasku adresu przeglądarki i naciśnij Enter)."
    )

    st.markdown(
        "**Krok 1.2** — Kliknij duży żółty przycisk **„Download Python”**. "
        "Pobierze się plik instalatora (zwykle do folderu Pobrane)."
    )

    st.markdown(
        "**Krok 1.3** — Otwórz pobrany plik (kliknij go dwa razy). "
        "Pojawi się okno instalatora Pythona."
    )

    st.warning(
        "⚠️ **NAJWAŻNIEJSZY MOMENT.** Jeśli w oknie instalatora gdzieś na dole jest haczyk/checkbox "
        "**„Add python.exe to PATH”** (albo podobny) — **MUSISZ go zaznaczyć** przed kliknięciem "
        "instalacji. Bez tego nic później nie zadziała.\n\n"
        "Na nowszych instalatorach zamiast haczyka pojawiają się **pytania w czarnym oknie** "
        "(„Update setting now? [y/N]”, „Add commands directory to your PATH? [y/N]”, "
        "„Install CPython now? [Y/n]”). **Na każde takie pytanie wpisz literę `y` i naciśnij Enter.** "
        "Jeśli Windows zapyta o zgodę administratora — kliknij **Tak**."
    )

    st.markdown(
        "**Krok 1.4** — Poczekaj, aż instalacja się skończy. Gdy zobaczysz, że gotowe "
        "(okno wróci do normalnej linii albo da się zamknąć) — **zrestartuj komputer**. "
        "To ważne, żeby zmiany zaczęły działać."
    )

    st.markdown("### Sprawdzenie, czy Python działa")
    st.markdown(
        "**Krok 1.5** — Po restarcie: kliknij menu **Start**, wpisz **`powershell`** i naciśnij Enter. "
        "Otworzy się niebieskie albo czarne okno (to „PowerShell” — miejsce, gdzie wpisuje się komendy)."
    )

    st.markdown(
        "**Krok 1.6** — W tym oknie wpisz dokładnie poniższą komendę i naciśnij Enter:"
    )
    st.code("python --version", language="text")
    st.markdown(
        "Jeśli pokaże się coś w stylu **`Python 3.14`** — gotowe, Python działa, przejdź do Części 2. \n\n"
        "Jeśli zamiast tego wyskoczy błąd albo otworzy się sklep Microsoft Store — Python nie został "
        "dodany do PATH (Krok 1.3). Wtedy najprościej odinstalować Pythona i zainstalować ponownie, "
        "pilnując tego haczyka/pytań o PATH."
    )

    st.divider()

    # ── CZĘŚĆ 2: DODATKI ──
    st.markdown("## Część 2 — Doinstalowanie dodatków (raz)")
    st.markdown(
        "To dokłada dwie rzeczy, których wpisywanie potrzebuje: bibliotekę sterującą przeglądarką "
        "i samą przeglądarkę. Robisz to raz.\n\n"
        "**Krok 2.1** — W tym samym oknie PowerShell wpisz poniższą komendę i naciśnij Enter. "
        "Poczekaj, aż się skończy (chwilę mieli, na końcu napisze „Successfully installed…”)."
    )
    st.code("pip install playwright requests", language="text")
    st.caption("Podpowiedź: w PowerShell wklejasz **prawym przyciskiem myszy** (Ctrl+V często nie działa).")

    st.markdown(
        "**Krok 2.2** — Gdy poprzednia się skończy, wpisz drugą komendę i naciśnij Enter. "
        "Ta pobiera przeglądarkę — potrwa minutę-dwie, zobaczysz pasek postępu."
    )
    st.code("python -m playwright install chromium", language="text")

    st.markdown("✅ **To wszystko, konfiguracja skończona.** Tego nie trzeba już nigdy powtarzać na tym komputerze.")

    st.divider()

    # ── CZĘŚĆ 3: CODZIENNE UŻYCIE ──
    st.markdown("## Część 3 — Codzienne wpisywanie faktury")
    st.markdown(
        "Te kroki powtarzasz przy **każdej** fakturze. Trwa to dosłownie chwilę.\n\n"
        "**Krok 3.1** — W tej apce, w zakładce **„📄 Odczyt faktury”**, wgraj fakturę i kliknij "
        "**„📖 Odczytaj fakturę”**. Sprawdź pozycje w tabeli."
    )
    st.markdown(
        "**Krok 3.2** — W sekcji **„📤 Wyślij do maggo”** kliknij **„⬇️ Pobierz plik”**. "
        "Pobierze się plik `pozycje_xxx.csv` (zapamiętaj, gdzie się zapisał — zwykle w Pobranych)."
    )
    st.markdown(
        "**Krok 3.3** — Otwórz **maggo** w przeglądarce, wejdź na **tę samą fakturę**, "
        "którą przed chwilą odczytałeś, i kliknij zakładkę **„Pozycje”** "
        "(tak, żeby był widoczny przycisk **„Dodaj”**)."
    )

    st.markdown(
        "**Krok 3.4** — W folderze, gdzie masz pobrane pliki (np. „Maggo” na pulpicie), "
        "kliknij **dwa razy** plik **`Wpisz_do_maggo.bat`**. Otworzy się czarne okno i przeglądarka."
    )
    st.caption(
        "Pierwszy raz radzę użyć **`Wpisz_do_maggo_PROBA.bat`** — działa tak samo, ale niczego "
        "nie wpisuje (tylko pokazuje, co by zrobił). Bezpieczny test."
    )

    st.markdown(
        "**Krok 3.5** — W oknie przeglądarki, które się otworzyło: zaloguj się do maggo "
        "(jeśli poprosi), wejdź na tę fakturę → zakładka **Pozycje**."
    )
    st.markdown(
        "**Krok 3.6** — Wróć do **czarnego okna** i naciśnij **Enter**."
    )
    st.markdown(
        "**Krok 3.7** — Wyskoczy okienko **„Wybierz plik CSV”** — wskaż **plik pobrany w kroku 3.2** "
        "(`pozycje_xxx.csv`) i kliknij **Otwórz**."
    )

    st.markdown(
        "**Krok 3.8** — Gotowe. Pozycje wpiszą się same — w czarnym oknie zobaczysz listę "
        "z **OK** przy każdej. Odśwież fakturę w maggo, żeby je zobaczyć w tabeli."
    )
    st.success("🎉 Tyle. Przy kolejnych fakturach powtarzasz tylko Część 3.")

    st.divider()

    # ── PROBLEMY ──
    st.markdown("## Coś nie działa?")
    st.markdown(
        "- **„Nie znaleziono Pythona”** (czarne okno) → Python nie jest zainstalowany albo nie został "
        "dodany do PATH. Wróć do Części 1 (Krok 1.3 i 1.4, restart).\n"
        "- **Czarne okno od razu się zamyka** → uruchom `Wpisz_do_maggo_PROBA.bat` zamiast zwykłego "
        "i przeczytaj komunikat (albo zrób zrzut ekranu).\n"
        "- **Po pierwszej pozycji lecą błędy „403”** → zamknij okno, zaloguj się świeżo do maggo "
        "i uruchom ponownie.\n"
        "- **„Nie udało się rozpoznać, która to faktura”** → upewnij się, że w przeglądarce jesteś "
        "na fakturze, na zakładce **Pozycje** (widoczny przycisk „Dodaj”), i dopiero wtedy naciskasz Enter."
    )
