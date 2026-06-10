"""
pdf_text_parser.py — odczyt pozycji z CYFROWEGO PDF (tekst zaznaczalny).

Dla faktur, które mają warstwę tekstową (np. Inter Cars), to jest właściwa droga
zamiast OCR z obrazka: tekst jest w pliku dokładny, więc nie ma sensu go
"fotografować" i zgadywać modelem. Czytamy go wprost.

KLUCZ: parsujemy po WSPÓŁRZĘDNYCH KOLUMN (gdzie fizycznie na stronie jest
"Ilość", gdzie "Netto"), a nie po kolejności słów w linii. Dzięki temu sklejone
w tekście cyfry (producent+ilość typu "EURORICAM1", albo ilość+cena z separatorem
tysięcy "LUK1 1 1 502,56") rozdzielają się poprawnie — bo siedzą w różnych
miejscach strony.

Layout zweryfikowany na fakturach Inter Cars (Potwierdzenie sprzedaży).
Jeśli inny dostawca ma inny układ kolumn — parser zwróci mało/zero pozycji i
apka spadnie na OCR (vision) jako fallback.

Wymaga: pdfplumber.
"""

from __future__ import annotations

import re
from typing import Optional


# Granice kolumn (środek X słowa wpada w przedział) — z nagłówków faktury Inter Cars:
#   "Ilość" ~ x 355-390, "Netto" (cena jedn.) ~ x 393-416, "VAT" ~ x 450
_X_ILOSC = (344, 392)
_X_CENA = (392, 449)


def _srodek(w) -> float:
    return (w["x0"] + w["x1"]) / 2


def czy_ma_warstwe_tekstowa(file_bytes: bytes) -> bool:
    """Szybki test: czy PDF ma sensowną warstwę tekstową (cyfrowy), czy to skan."""
    try:
        import pdfplumber
    except ImportError:
        return False
    try:
        import io
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            if not pdf.pages:
                return False
            txt = pdf.pages[0].extract_text() or ""
            return len(txt.strip()) > 100
    except Exception:
        return False


def parsuj_inter_cars(file_bytes: bytes) -> tuple[Optional[list[dict]], Optional[dict], Optional[str]]:
    """
    Parsuje pozycje z cyfrowego PDF (layout Inter Cars).

    Zwraca (pozycje, naglowek, blad):
      pozycje  — lista dict {lp, indeks, ilosc, cena_netto},
      naglowek — dict {dostawca, numer_dokumentu, data, waluta, suma_netto_faktury},
      blad     — komunikat albo None.
    Gdy nie rozpozna układu (mało pozycji), zwraca pozycje=None -> apka użyje OCR.
    """
    try:
        import pdfplumber
    except ImportError:
        return None, None, "Brak pdfplumber"

    import io
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pelny_tekst = "\n".join((p.extract_text() or "") for p in pdf.pages)

            seen: dict[int, dict] = {}
            for p in pdf.pages:
                slowa = p.extract_words(use_text_flow=False)
                # grupowanie słów w wiersze po współrzędnej Y (top), tolerancja ~3px
                wiersze: dict[int, list] = {}
                for w in slowa:
                    wiersze.setdefault(round(w["top"] / 3), []).append(w)

                for ws in wiersze.values():
                    ws_sorted = sorted(ws, key=lambda w: w["x0"])
                    tekst = " ".join(w["text"] for w in ws_sorted)
                    m = re.match(r"^\s*(\d+)\.\s", tekst)
                    if not m:
                        continue
                    lp = int(m.group(1))
                    if lp in seen:
                        continue  # pierwsze wystąpienie = wiersz pozycji (kolejne to opis)

                    # ── ilość ──
                    ilosc = None
                    # 1) czysta liczba w kolumnie ilości
                    for w in ws_sorted:
                        if _X_ILOSC[0] <= _srodek(w) <= _X_ILOSC[1] and re.fullmatch(r"\d+", w["text"]):
                            ilosc = int(w["text"])
                    # 2) ilość doklejona na końcu producenta (np. "EURORICAM1", "PEUGEOT7")
                    if ilosc is None:
                        for w in ws_sorted:
                            if w["x1"] >= _X_ILOSC[0] and w["x0"] < _X_ILOSC[1]:
                                mm = re.search(r"(\d+)$", w["text"])
                                if mm and not re.fullmatch(r"\d+", w["text"]):
                                    ilosc = int(mm.group(1))
                                    break

                    # ── cena (kolumna Netto) ── skleja ew. rozbite "1" + "502,56"
                    cena = None
                    kawalki = [
                        w["text"] for w in ws_sorted
                        if _X_CENA[0] <= _srodek(w) <= _X_CENA[1] and re.search(r"[\d,]", w["text"])
                    ]
                    if kawalki:
                        s = "".join(kawalki).replace(" ", "").replace(".", "")
                        mc = re.search(r"(\d{1,3}(?:\d{3})*,\d{2})", s)
                        if mc:
                            cena = float(mc.group(1).replace(",", "."))

                    # ── indeks ── to co między numerem LP a nazwą; bierzemy z tekstu liniowego
                    # (indeks bywa ze spacjami: "626 3032 00", "HP200 529")
                    indeks = _wytnij_indeks(tekst)

                    if ilosc is not None and cena is not None:
                        seen[lp] = {"lp": lp, "indeks": indeks, "ilosc": ilosc, "cena_netto": cena}

            pozycje = [seen[k] for k in sorted(seen)]

            # heurystyka: jeśli złapaliśmy za mało, to nie jest ten layout — niech zadecyduje OCR
            if len(pozycje) < 3:
                return None, None, "Nie rozpoznano układu tabeli (za mało pozycji) — użyję OCR."

            naglowek = _wytnij_naglowek(pelny_tekst)
            return pozycje, naglowek, None

    except Exception as e:
        return None, None, f"Błąd parsowania tekstu PDF: {e}"


def _wytnij_indeks(tekst_wiersza: str) -> str:
    """
    Wyciąga indeks z linii pozycji. Indeks zaczyna się po 'LP. ' i kończy przed
    nazwą towaru. Nazwy zaczynają się od dużej litery + małe (np. 'Uszczelniacz').
    Indeks może zawierać spacje, kropki, ukośniki ('626 3032 00', 'AB.41376.V',
    'L68149/L68110'). Bierzemy wszystko od początku aż do pierwszego "słowa-nazwy".
    """
    # zdejmij "N. "
    t = re.sub(r"^\s*\d+\.\s+", "", tekst_wiersza)
    tokeny = t.split()
    indeks_czesci: list[str] = []
    for tok in tokeny:
        # token wyglądający jak początek NAZWY: zaczyna się Dużą literą, dalej małe,
        # i nie jest częścią kodu (kody mają cyfry/kropki/ukośniki lub same wielkie litery)
        if re.match(r"^[A-ZŁŚŻŹĆĄĘÓŃ][a-ząćęłńóśźż]", tok):
            break
        indeks_czesci.append(tok)
        # bezpiecznik: indeks rzadko ma >4 segmenty
        if len(indeks_czesci) >= 5:
            break
    return " ".join(indeks_czesci).strip()


def _wytnij_naglowek(pelny_tekst: str) -> dict:
    """Wyciąga dane nagłówka faktury z tekstu."""
    h = {"dostawca": "", "numer_dokumentu": "", "data": "", "waluta": "PLN",
         "suma_netto_faktury": None}

    m = re.search(r"nr\s+([0-9A-Z/]+)\s+z dnia\s+(\d{4}-\d{2}-\d{2})", pelny_tekst)
    if m:
        h["numer_dokumentu"] = m.group(1)
        h["data"] = m.group(2)

    m = re.search(r"Sprzedawca:\s*([^\n]+?)(?:\s{2,}|Odbiorca|$)", pelny_tekst)
    if m:
        h["dostawca"] = m.group(1).strip()

    # suma netto: "Netto: 35 255,59 PLN"
    m = re.search(r"Netto:\s*([\d\s]+,\d{2})\s*(PLN|EUR|GBP)?", pelny_tekst)
    if m:
        h["suma_netto_faktury"] = float(m.group(1).replace(" ", "").replace(",", "."))
        if m.group(2):
            h["waluta"] = m.group(2)

    return h


# ══════════════════════════════════════════════════════════════════════════
#  PARSER FAKTUR KSeF (Krajowy System e-Faktur) — format państwowy, jednolity.
#  Rozpoznawalny po "Krajowy System e-Faktur" / "Numer KSEF:" w treści.
#  Działa dla KAŻDEGO dostawcy wystawiającego w KSeF (nie tylko ROMBOR).
# ══════════════════════════════════════════════════════════════════════════

# wiersz pozycji KSeF: Lp + 32-znakowy hex-ID + nazwa + ogon liczbowy
_KSEF_RE_START = re.compile(r"^(\d+)\s+([0-9A-Fa-f]{32})\s+(.*)$")
# ogon kotwiczony do końca: cena ilość miara VAT% znacznik wartość
_KSEF_RE_OGON = re.compile(
    r"\s(\d{1,3}(?:\s\d{3})*,\d{2}|\d+,\d{2})"        # cena jedn. netto
    r"\s+(\d+)"                                        # ilość
    r"\s+(?:szt\.|kpl\.|kg|m|l|mb|opak\.|para|kmpl\.)" # miara
    r"\s+\d+%"                                         # VAT
    r"\s+\d+"                                          # znacznik zał.15
    r"\s+(\d{1,3}(?:\s\d{3})*,\d{2}|\d+,\d{2})\s*$"    # wartość netto
)
_KSEF_STOPKI = (
    "Lp.", "Kwota", "Podsumowanie", "Faktura", "Pozycje", "Adnotacje",
    "Mechanizm", "Płatność", "Forma", "Termin", "Numer", "Rejestry",
    "Sprawdź", "Warunki", "Zamówienie", "Data ", "Pełna", "Wytworzona",
    "Nie możesz", "https", "Kod ", "Rachunek", "Nazwa banku", "Opis",
)
_KSEF_GTIN_ROW = re.compile(r"^\d+\s+\w*\d{5,}")  # wiersz tabeli GTIN


def czy_to_ksef(file_bytes: bytes) -> bool:
    """Wykrywa fakturę KSeF po charakterystycznych frazach."""
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            txt = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
        t = txt.lower()
        return ("krajowy system e-faktur" in t) or ("numer ksef" in t) or ("numer ksef:" in t)
    except Exception:
        return False


def _ksef_na_float(s):
    if s is None:
        return None
    s = str(s).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def parsuj_ksef(file_bytes: bytes):
    """
    Parser faktur w formacie KSeF. Zwraca (pozycje, naglowek, blad) — tak jak
    parsuj_inter_cars. Cena liczona z wartość÷ilość (jednoznaczne), bo numery
    katalogowe w opisie bywają mylone z ceną przy parsowaniu wprost.
    """
    try:
        import pdfplumber, io
    except ImportError:
        return None, None, "Brak pdfplumber"

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            linie = []
            for page in pdf.pages:
                t = page.extract_text() or ""
                linie.extend(t.split("\n"))
            pelny_tekst = "\n".join(linie)

        pozycje = []

        def sparsuj_wiersz(lp, reszta):
            m = _KSEF_RE_OGON.search(reszta)
            if not m:
                return None
            ilosc = _ksef_na_float(m.group(2))
            wartosc = _ksef_na_float(m.group(3))
            cena = round(wartosc / ilosc, 2) if (wartosc and ilosc) else _ksef_na_float(m.group(1))
            return {"lp": lp, "indeks": reszta[:m.start()].strip(),
                    "ilosc": ilosc, "cena_netto": cena, "_wartosc": wartosc}

        sekcja_gtin = False
        for ln in linie:
            s = ln.strip()
            if not s:
                continue
            if s.startswith("Lp.") and "GTIN" in s:
                sekcja_gtin = True
                continue
            if sekcja_gtin:
                continue
            m = _KSEF_RE_START.match(s)
            if m:
                lp = int(m.group(1))
                poz = sparsuj_wiersz(lp, m.group(3))
                if poz is not None:
                    pozycje.append(poz)
                else:
                    pozycje.append({"lp": lp, "_buf": m.group(3), "indeks": "",
                                    "ilosc": None, "cena_netto": None, "_wartosc": None})
            else:
                if s.startswith(_KSEF_STOPKI) or _KSEF_GTIN_ROW.match(s):
                    continue
                if not pozycje:
                    continue
                ost = pozycje[-1]
                if ost.get("cena_netto") is None and "_buf" in ost:
                    ost["_buf"] += " " + s
                    dom = sparsuj_wiersz(ost["lp"], ost["_buf"])
                    if dom is not None:
                        pozycje[-1] = dom
                else:
                    ost["indeks"] = (ost["indeks"] + " " + s).strip()

        pozycje = [p for p in pozycje if p.get("cena_netto") is not None]
        # posprzątaj klucze pomocnicze
        for p in pozycje:
            p.pop("_buf", None)
            p.pop("_wartosc", None)

        if len(pozycje) < 2:
            return None, None, "KSeF: nie rozpoznano pozycji — użyję OCR."

        naglowek = _wytnij_naglowek_ksef(pelny_tekst)
        return pozycje, naglowek, None

    except Exception as e:
        return None, None, f"Błąd parsowania KSeF: {e}"


def _wytnij_naglowek_ksef(pelny_tekst: str) -> dict:
    """Nagłówek faktury KSeF."""
    h = {"dostawca": "", "numer_dokumentu": "", "data": "", "waluta": "PLN",
         "suma_netto_faktury": None}

    m = re.search(r"Numer Faktury:\s*\n?\s*([^\n]+)", pelny_tekst)
    if m:
        h["numer_dokumentu"] = m.group(1).strip()

    # data wystawienia: "...ustawy: 11.05.2026" -> zamień na ISO
    m = re.search(r"Data wystawienia[^:]*:\s*(\d{2})\.(\d{2})\.(\d{4})", pelny_tekst)
    if m:
        h["data"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    # dostawca: "Nazwa:" po "Sprzedawca", ale ucięte zanim wejdzie w "Nazwa:" nabywcy
    # albo w kolejne pole. KSeF skleja kolumny Sprzedawca|Nabywca, więc bierzemy
    # tylko do pierwszego markera następnego pola.
    m = re.search(r"Sprzedawca.*?Nazwa:\s*(.+?)(?:\s+Nazwa:|\s+Adres|\s+NIP:|\s{2,}|$)",
                  pelny_tekst, re.DOTALL)
    if m:
        dost = m.group(1).strip().strip('"').strip()
        # gdyby mimo to złapało nazwę nabywcy po spacjach — utnij na "AUTOS"
        # (nasza firma jako nabywca nie powinna być dostawcą)
        dost = re.split(r"\s+(?:Nazwa:|AUTOS\b)", dost)[0].strip().strip('"').strip()
        h["dostawca"] = dost

    m = re.search(r"Kod waluty:\s*([A-Z]{3})", pelny_tekst)
    if m:
        h["waluta"] = m.group(1)

    # suma netto: z podsumowania stawek "23% lub 22% 28 410,17 ..."
    # bierzemy pierwszą kwotę po frazie "Kwota netto" albo z wiersza stawki
    m = re.search(r"(\d{1,3}(?:\s\d{3})*,\d{2})\s+\d{1,3}(?:\s\d{3})*,\d{2}\s+\d{1,3}(?:\s\d{3})*,\d{2}",
                  pelny_tekst)
    if m:
        h["suma_netto_faktury"] = float(m.group(1).replace(" ", "").replace(",", "."))

    return h


def parsuj_auto(file_bytes: bytes):
    """
    Dyspozytor: wybiera właściwy parser wg formatu PDF.
    Zwraca (pozycje, naglowek, blad) — wspólny kontrakt.
    Kolejność: najpierw KSeF (jednoznaczna sygnatura), potem Inter Cars.
    """
    if czy_to_ksef(file_bytes):
        return parsuj_ksef(file_bytes)
    return parsuj_inter_cars(file_bytes)
