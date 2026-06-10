"""
vision_ocr.py — odczyt faktur zakupowych przez Gemini Vision na Vertex AI.

Obsługuje OBA typy faktur jednym silnikiem:
  - cyfrowy PDF (tekst zaznaczalny, np. Inter Cars) — renderowany do obrazu,
  - zdjęcie/skan papieru (np. Bobrek) — podany wprost jako obraz.

Łączy się z Vertex AI dokładnie tak jak istniejące apki Autos:
  credentials z service_account (FIREBASE_CREDS), projekt z GCP_PROJECT_IDS,
  lokalizacja z GCP_LOCATION, model gemini-2.5-pro.

KLUCZOWE: model może się pomylić/zahalucynować, więc każdy odczyt przechodzi
WALIDACJĘ ARYTMETYKI po stronie Pythona (ilość×cena=wartość w wierszu;
suma pozycji=suma z dołu faktury). Rozbieżności są oznaczane flagą — nic
nie idzie dalej "na ślepo".

Wymaga: google-cloud-aiplatform, pdf2image (+ poppler), pillow.
"""

from __future__ import annotations

import io
import json
import re
import random
from dataclasses import dataclass, field, asdict
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────
#  STRUKTURY WYNIKU
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class PozycjaFaktury:
    """Jedna pozycja (wiersz) z faktury."""
    lp: Optional[int]
    indeks: str                       # kod towaru u dostawcy (np. "62422MASIERO", "12921AM")
    nazwa: str
    ilosc: float
    cena_netto: float                 # cena jednostkowa netto
    wartosc_netto: Optional[float]    # wartość pozycji wg faktury (ilość×cena) — jeśli podana
    # pola walidacji wypełniane po stronie Pythona:
    wartosc_wyliczona: Optional[float] = None
    arytmetyka_ok: Optional[bool] = None
    uwaga: str = ""


@dataclass
class WynikOCR:
    """Pełny wynik odczytu faktury."""
    dostawca: str
    numer_dokumentu: str
    data: str
    waluta: str
    pozycje: list[PozycjaFaktury] = field(default_factory=list)
    # sumy:
    suma_netto_faktury: Optional[float] = None   # zadeklarowana na dole faktury
    suma_netto_pozycji: Optional[float] = None   # policzona z pozycji
    suma_ok: Optional[bool] = None
    # diagnostyka:
    liczba_pozycji: int = 0
    ostrzezenia: list[str] = field(default_factory=list)
    model: str = ""
    zrodlo_odczytu: str = ""          # "tekst PDF" albo "OCR (vision)"
    raw_odpowiedz: str = ""           # surowa odpowiedź modelu (debug)

    def do_dict(self) -> dict:
        d = asdict(self)
        return d


# ──────────────────────────────────────────────────────────────────────────
#  POŁĄCZENIE Z VERTEX (wzorzec z istniejących apek Autos)
# ──────────────────────────────────────────────────────────────────────────

def init_vertex(secrets) -> tuple[Optional[str], Optional[str]]:
    """
    Inicjalizuje Vertex AI. Zwraca (project_id, blad).
    `secrets` to obiekt z dostępem [klucz] i .get(...) — w Streamlit podaj st.secrets.

    Używa tego samego wzorca co reszta apek:
      - credentials: service_account z FIREBASE_CREDS,
      - projekt: losowy z GCP_PROJECT_IDS (load balancing),
      - location: GCP_LOCATION (domyślnie us-central1).
    """
    try:
        import vertexai
        from google.oauth2 import service_account
    except ImportError:
        return None, "Brak biblioteki google-cloud-aiplatform (dodaj do requirements.txt)"

    try:
        gcp_projects = secrets.get("GCP_PROJECT_IDS", [])
        if isinstance(gcp_projects, str):
            gcp_projects = [gcp_projects]
        gcp_projects = list(gcp_projects)
        if not gcp_projects:
            return None, "Brak GCP_PROJECT_IDS w secrets"

        project_id = random.choice(gcp_projects)
        location = secrets.get("GCP_LOCATION", "us-central1")

        creds_info = json.loads(secrets["FIREBASE_CREDS"])
        creds = service_account.Credentials.from_service_account_info(creds_info)

        vertexai.init(project=project_id, location=location, credentials=creds)
        return project_id, None
    except Exception as e:
        return None, f"Błąd inicjalizacji Vertex AI: {e}"


# ──────────────────────────────────────────────────────────────────────────
#  PRZYGOTOWANIE OBRAZÓW (PDF → obrazy / obraz → obraz)
# ──────────────────────────────────────────────────────────────────────────

def _plik_na_obrazy(file_bytes: bytes, nazwa_pliku: str) -> list[bytes]:
    """
    Zamienia plik na listę obrazów PNG (bytes).
    PDF → każda strona osobno (przez pdf2image/poppler).
    Obraz (jpg/png/...) → zwraca jak jest (jeden element).
    """
    nazwa = (nazwa_pliku or "").lower()

    if nazwa.endswith(".pdf"):
        try:
            from pdf2image import convert_from_bytes
        except ImportError:
            raise RuntimeError("Brak pdf2image — dodaj 'pdf2image' do requirements i poppler do packages.txt")
        # 200 dpi to dobry kompromis czytelność/rozmiar dla faktur
        strony = convert_from_bytes(file_bytes, dpi=200)
        wynik = []
        for s in strony:
            buf = io.BytesIO()
            s.save(buf, format="PNG")
            wynik.append(buf.getvalue())
        return wynik

    # obraz — podajemy bez zmian (Gemini akceptuje jpg/png/webp)
    return [file_bytes]


def _mime_dla(nazwa_pliku: str) -> str:
    nazwa = (nazwa_pliku or "").lower()
    if nazwa.endswith(".png"):
        return "image/png"
    if nazwa.endswith(".webp"):
        return "image/webp"
    # PDF został już zrenderowany do PNG; reszta to JPEG
    if nazwa.endswith(".pdf"):
        return "image/png"
    return "image/jpeg"


# ──────────────────────────────────────────────────────────────────────────
#  PROMPT
# ──────────────────────────────────────────────────────────────────────────

_PROMPT = """Jesteś precyzyjnym ekstraktorem danych z faktur zakupowych (polskie faktury za części samochodowe).

Na obrazie/obrazach jest JEDNA faktura. Odczytaj z niej pozycje (wiersze towarów) oraz dane nagłówka.

ZASADY ODCZYTU — KRYTYCZNE:
1. Przepisuj liczby DOKŁADNIE tak, jak są na fakturze. NIE zaokrąglaj, NIE przeliczaj, NIE poprawiaj.
2. Liczby na fakturze są w formacie polskim: przecinek = część dziesiętna, spacja = separator tysięcy.
   Przykład: "1 232,28" oznacza tysiąc dwieście trzydzieści dwa i 28/100. W JSON zapisz jako 1232.28 (kropka, bez spacji).
3. "indeks" to KOD TOWARU u dostawcy (kolumna typu "Kod towaru", "Indeks", "Towar/opis", "Symbol").
   Przepisz go znak w znak, łącznie z literami, spacjami i sufiksami (np. "12921AM", "62422MASIERO", "HP200 529").
   NIE odczytuj nazwy towaru — jest niepotrzebna, pomiń ją całkowicie (oszczędza miejsce).
4. Jeśli jakiejś wartości NIE MA na fakturze, wpisz null. Nie zgaduj.
5. Czytaj WSZYSTKIE pozycje, także te przy zagięciach/fałdach kartki. Jeśli wiersz jest nieczytelny, dodaj go z polem "uwaga".

ZWRÓĆ WYŁĄCZNIE JSON (bez markdown, bez komentarzy, bez ```), w strukturze:
{
  "dostawca": "nazwa sprzedawcy z nagłówka",
  "numer_dokumentu": "numer faktury/dokumentu",
  "data": "data wystawienia (YYYY-MM-DD jeśli się da)",
  "waluta": "PLN/EUR/...",
  "suma_netto_faktury": <liczba: SUMA wartości netto z dołu faktury (pole 'Netto'/'Wartość netto'/'Razem netto'), albo null>,
  "pozycje": [
    {
      "lp": <numer porządkowy lub null>,
      "indeks": "kod towaru u dostawcy",
      "ilosc": <liczba sztuk>,
      "cena_netto": <cena jednostkowa NETTO; UWAGA: jeśli faktura podaje tylko cenę jednostkową BRUTTO (a nie netto) — wstaw null, NIE wpisuj tu ceny brutto>,
      "wartosc_netto": <wartość pozycji netto (kolumna 'Wartość netto'); podaj ZAWSZE gdy jest na fakturze — z niej wyliczymy cenę jednostkową, gdy brak ceny netto>,
      "uwaga": "tekst tylko jeśli coś nieczytelne/wątpliwe, inaczej pusty"
    }
  ]
}

Zwróć surowy JSON i nic więcej."""


# ──────────────────────────────────────────────────────────────────────────
#  PARSOWANIE ODPOWIEDZI
# ──────────────────────────────────────────────────────────────────────────

def _wyciagnij_json(tekst: str) -> dict:
    """Wyciąga obiekt JSON z odpowiedzi modelu (zdejmuje ew. ```json fences)."""
    s = tekst.strip()
    s = re.sub(r"^```json\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^```\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    s = s.strip()
    # gdyby model dorzucił tekst dookoła — wytnij od pierwszej { do ostatniej }
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j != -1 and j > i:
            s = s[i:j + 1]
    return json.loads(s)


def _na_float(v) -> Optional[float]:
    """Zamienia '1 232,28' / '1232.28' / 1232.28 → 1232.28. None gdy się nie da."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace(" ", "").replace("\u00a0", "")  # spacje zwykłe i twarde
    # jeśli jest i kropka i przecinek — przecinek to dziesiętne, kropka tysiące
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ──────────────────────────────────────────────────────────────────────────
#  WALIDACJA ARYTMETYKI (po stronie Pythona — NIE ufamy modelowi na słowo)
# ──────────────────────────────────────────────────────────────────────────

_TOLERANCJA = 0.01  # 1 grosz

def _waliduj(wynik: WynikOCR) -> None:
    """Uzupełnia pola walidacji: arytmetyka wierszy + zgodność sumy. Modyfikuje w miejscu.

    WAŻNE: do sumy bierzemy cena×ilość (czyli to, co realnie pojedzie do maggo jako
    cena jednostkowa), a NIE deklarowaną 'wartosc_netto' z faktury. Dzięki temu, jeśli
    model zahalucynuje cenę jednostkową (a wartość wiersza odczyta poprawnie), suma
    przestanie się zgadzać i podniesiemy flagę. Gdyby było odwrotnie — błąd ceny
    "schowałby się" za poprawną wartością i przeszedł niezauważony.
    """
    suma_poz = 0.0
    for p in wynik.pozycje:
        # FALLBACK: brak ceny jednostkowej netto, ale jest wartość netto pozycji i ilość
        # -> wylicz cenę jako wartość_netto / ilość (cena PO rabacie). Dotyczy faktur,
        # które podają tylko cenę brutto + wartość netto (np. Krotoski). Liczymy TYLKO
        # gdy cena jest pusta — nie ruszamy faktur z podaną ceną jednostkową netto.
        if (p.cena_netto is None and p.wartosc_netto is not None
                and p.ilosc not in (None, 0)):
            p.cena_netto = round(p.wartosc_netto / p.ilosc, 2)
            p.uwaga = (p.uwaga + " | " if p.uwaga else "") + \
                "cena wyliczona z wartości netto ÷ ilość (po rabacie)"

        # wylicz wartość wiersza z ceny i ilości
        if p.ilosc is not None and p.cena_netto is not None:
            p.wartosc_wyliczona = round(p.ilosc * p.cena_netto, 2)
        # porównaj z deklarowaną (jeśli jest)
        if p.wartosc_netto is not None and p.wartosc_wyliczona is not None:
            p.arytmetyka_ok = abs(p.wartosc_netto - p.wartosc_wyliczona) <= _TOLERANCJA
            if not p.arytmetyka_ok:
                p.uwaga = (p.uwaga + " | " if p.uwaga else "") + \
                    f"ilość×cena={p.wartosc_wyliczona}, na fakturze {p.wartosc_netto}"
        # Do sumy: PREFERUJEMY cena×ilość (to idzie do maggo). Dopiero gdy nie da się
        # policzyć (brak ceny/ilości), spadamy na deklarowaną wartość z faktury.
        skladnik = p.wartosc_wyliczona if p.wartosc_wyliczona is not None else p.wartosc_netto
        if skladnik is not None:
            suma_poz += skladnik

    wynik.suma_netto_pozycji = round(suma_poz, 2)
    wynik.liczba_pozycji = len(wynik.pozycje)

    if wynik.suma_netto_faktury is not None:
        wynik.suma_ok = abs(wynik.suma_netto_faktury - wynik.suma_netto_pozycji) <= _TOLERANCJA
        if not wynik.suma_ok:
            wynik.ostrzezenia.append(
                f"Suma pozycji ({wynik.suma_netto_pozycji}) ≠ suma na fakturze "
                f"({wynik.suma_netto_faktury}). Różnica {round(wynik.suma_netto_pozycji - wynik.suma_netto_faktury, 2)}."
            )
    else:
        wynik.ostrzezenia.append("Brak sumy netto na fakturze do porównania — sprawdź pozycje ręcznie.")

    zle_wiersze = [p.lp for p in wynik.pozycje if p.arytmetyka_ok is False]
    if zle_wiersze:
        wynik.ostrzezenia.append(f"Wiersze z niezgodną arytmetyką (lp): {zle_wiersze}")


# ──────────────────────────────────────────────────────────────────────────
#  GŁÓWNA FUNKCJA
# ──────────────────────────────────────────────────────────────────────────

def _wyglada_na_ksef_xml(file_bytes: bytes) -> bool:
    """Szybkie wykrycie XML KSeF po nagłówku pliku (gdy brak rozszerzenia .xml)."""
    try:
        glowa = file_bytes[:400].decode("utf-8", errors="ignore").lower()
    except Exception:
        return False
    return glowa.lstrip().startswith("<?xml") and "faktura" in glowa


def odczytaj_fakture(
    file_bytes: bytes,
    nazwa_pliku: str,
    secrets,
    model_name: str = "gemini-2.5-pro",
) -> tuple[Optional[WynikOCR], Optional[str]]:
    """
    Odczytuje fakturę z pliku (PDF lub obraz). Zwraca (WynikOCR, blad).

    Parametry:
      file_bytes  — zawartość pliku,
      nazwa_pliku — nazwa (po rozszerzeniu poznajemy PDF vs obraz),
      secrets     — st.secrets (musi mieć FIREBASE_CREDS, GCP_PROJECT_IDS, GCP_LOCATION),
      model_name  — model Gemini (domyślnie ten sam co reszta apek).
    """
    # ── XML-FIRST: faktura KSeF w XML to najlepsze źródło (pola jednoznaczne) ──
    nazwa_low = (nazwa_pliku or "").lower()
    if nazwa_low.endswith(".xml") or _wyglada_na_ksef_xml(file_bytes):
        try:
            import ksef_xml_parser as kx
            if kx.czy_to_ksef_xml(file_bytes):
                pozycje_xml, naglowek, blad_xml = kx.parsuj_ksef_xml(file_bytes)
                if pozycje_xml:
                    wynik = WynikOCR(
                        dostawca=naglowek.get("dostawca", ""),
                        numer_dokumentu=naglowek.get("numer_dokumentu", ""),
                        data=naglowek.get("data", ""),
                        waluta=naglowek.get("waluta", "PLN"),
                        pozycje=[PozycjaFaktury(
                            lp=p["lp"], indeks=p["indeks"], nazwa="",
                            ilosc=p["ilosc"], cena_netto=p["cena_netto"], wartosc_netto=None,
                        ) for p in pozycje_xml],
                        suma_netto_faktury=naglowek.get("suma_netto_faktury"),
                        model="— (parser XML KSeF, bez modelu)",
                        zrodlo_odczytu="XML KSeF",
                        raw_odpowiedz="(odczyt z pliku XML KSeF — model nieużyty)",
                    )
                    _waliduj(wynik)
                    return wynik, None
        except Exception:
            pass  # gdyby parser XML zawiódł, spróbuj dalszych ścieżek

    # ── DIGITAL-FIRST: dla cyfrowego PDF czytamy tekst wprost (bez OCR, bez zgadywania) ──
    if nazwa_low.endswith(".pdf"):
        try:
            import pdf_text_parser as ptp
            if ptp.czy_ma_warstwe_tekstowa(file_bytes):
                # dyspozytor: KSeF albo Inter Cars (wg formatu PDF)
                pozycje_txt, naglowek, blad_txt = ptp.parsuj_auto(file_bytes)
                if pozycje_txt:  # rozpoznano układ — budujemy wynik z TEKSTU
                    zrodlo = "tekst PDF (KSeF)" if ptp.czy_to_ksef(file_bytes) else "tekst PDF"
                    wynik = WynikOCR(
                        dostawca=naglowek.get("dostawca", ""),
                        numer_dokumentu=naglowek.get("numer_dokumentu", ""),
                        data=naglowek.get("data", ""),
                        waluta=naglowek.get("waluta", "PLN"),
                        pozycje=[PozycjaFaktury(
                            lp=p["lp"], indeks=p["indeks"], nazwa="",
                            ilosc=p["ilosc"], cena_netto=p["cena_netto"], wartosc_netto=None,
                        ) for p in pozycje_txt],
                        suma_netto_faktury=naglowek.get("suma_netto_faktury"),
                        model="— (parser tekstowy, bez modelu)",
                        zrodlo_odczytu=zrodlo,
                        raw_odpowiedz="(odczyt z warstwy tekstowej PDF — model nieużyty)",
                    )
                    _waliduj(wynik)
                    return wynik, None
                # brak rozpoznania układu -> spadamy na OCR poniżej
        except Exception:
            pass  # cokolwiek się stanie z parserem tekstowym, próbujemy OCR

    # 1) Vertex (OCR fallback — skany, zdjęcia, nierozpoznane układy PDF)
    project_id, err = init_vertex(secrets)
    if err:
        return None, err

    try:
        from vertexai.generative_models import GenerativeModel, Part
    except ImportError:
        return None, "Brak biblioteki google-cloud-aiplatform"

    # 2) plik → obrazy
    try:
        obrazy = _plik_na_obrazy(file_bytes, nazwa_pliku)
    except Exception as e:
        return None, f"Nie udało się przygotować obrazu faktury: {e}"
    if not obrazy:
        return None, "Plik nie zawiera żadnej strony/obrazu."

    mime = _mime_dla(nazwa_pliku)

    # 3) budujemy zawartość: prompt + wszystkie strony jako obrazy
    czesci = [_PROMPT]
    for img in obrazy:
        czesci.append(Part.from_data(data=img, mime_type=mime))

    # 4) wywołanie modelu — temperatura 0 = maksymalna dosłowność (anty-halucynacja)
    try:
        model = GenerativeModel(model_name)
        response = model.generate_content(
            czesci,
            generation_config={"temperature": 0, "max_output_tokens": 32768},
        )
        raw = response.text
        # wykryj urwanie z powodu limitu tokenów (finish_reason MAX_TOKENS = 2)
        try:
            fr = response.candidates[0].finish_reason
            if int(fr) == 2:  # MAX_TOKENS
                return None, (
                    "Odpowiedź modelu urwała się przez limit długości (za dużo pozycji). "
                    "Zgłoś to — trzeba jeszcze podnieść limit lub podzielić fakturę."
                )
        except Exception:
            pass
    except Exception as e:
        return None, f"Błąd wywołania modelu Vertex: {e}"

    # 5) parsowanie JSON
    try:
        dane = _wyciagnij_json(raw)
    except Exception as e:
        return None, f"Model nie zwrócił poprawnego JSON: {e}\n\nSurowa odpowiedź:\n{raw[:2000]}"

    # 6) budowa struktury + normalizacja liczb
    pozycje = []
    for poz in dane.get("pozycje", []):
        pozycje.append(PozycjaFaktury(
            lp=poz.get("lp"),
            indeks=str(poz.get("indeks", "")).strip(),
            nazwa=str(poz.get("nazwa", "")).strip(),
            ilosc=_na_float(poz.get("ilosc")),
            cena_netto=_na_float(poz.get("cena_netto")),
            wartosc_netto=_na_float(poz.get("wartosc_netto")),
            uwaga=str(poz.get("uwaga", "")).strip(),
        ))

    wynik = WynikOCR(
        dostawca=str(dane.get("dostawca", "")).strip(),
        numer_dokumentu=str(dane.get("numer_dokumentu", "")).strip(),
        data=str(dane.get("data", "")).strip(),
        waluta=str(dane.get("waluta", "PLN")).strip() or "PLN",
        pozycje=pozycje,
        suma_netto_faktury=_na_float(dane.get("suma_netto_faktury")),
        model=f"{model_name} (projekt {project_id})",
        zrodlo_odczytu="OCR (vision)",
        raw_odpowiedz=raw,
    )

    # 7) WALIDACJA ARYTMETYKI
    _waliduj(wynik)

    return wynik, None
