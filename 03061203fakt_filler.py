"""
maggo - automatyczny wypełniacz pozycji faktury.
v0.2 - tryb okienkowy dla laika: wybór CSV przez okno, czytelne kroki, bez komend.
       (v0.1 - bezpośrednie POST-y na /Dokumenty/addDokFaktPoz z sesją z Playwright.)

JAK DZIAŁA (tryb domyślny - dwuklik w Wpisz_do_maggo.bat):
  1. Otwiera się przeglądarka. Logujesz się do maggo i wchodzisz na fakturę
     -> zakładka "Pozycje" (musi być widoczny przycisk "Dodaj").
  2. Wracasz do czarnego okna i naciskasz ENTER.
  3. Wyskakuje okienko "wybierz plik CSV" - wskazujesz pobrany z apki plik
     pozycje_xxx.csv (gdziekolwiek go masz).
  4. Skrypt przejmuje Twoją zalogowaną sesję, wykrywa którą to faktura,
     i wpisuje wszystkie pozycje (pokazuje ptaszki). Nic nie klikasz ręcznie.

WYMAGA (instalacja raz):
  pip install playwright requests
  playwright install chromium

UŻYCIE ZAAWANSOWANE (z linii poleceń, opcjonalne):
  python fakt_filler.py --csv plik.csv            # wskaż plik wprost (bez okienka)
  python fakt_filler.py --dry-run                 # próba: nic nie wysyła, tylko pokazuje
  python fakt_filler.py --base-url https://... --df-id 3055   # wymuś serwer/fakturę
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


# ===== Stałe specyficzne dla maggo (odczytane z HTML formularza) =====
ADD_POZ_PATH = "/Dokumenty/addDokFaktPoz"   # endpoint dodawania pozycji
WAL_ID_PLN = "26"                            # <option value="26">PLN - złotówki
STAWKA_VAT_23 = "0.23"                       # <option value="0.23">23%
TOKEN_FIELD = "__RequestVerificationToken"


@dataclass
class Pozycja:
    indeksDost: str
    ilosc: str
    cena: str   # z kropką dziesiętną


# tkinter jest wbudowany w Pythona - bez instalacji
def wybierz_plik_okienkiem() -> Optional[str]:
    """Okno Windows 'Otwórz plik' do wskazania CSV. Start w Pobranych, jeśli są."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    poczatek = os.path.join(os.path.expanduser("~"), "Downloads")
    if not os.path.isdir(poczatek):
        poczatek = os.path.expanduser("~")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    sciezka = filedialog.askopenfilename(
        title="Wybierz plik CSV z pozycjami faktury (pobrany z apki)",
        initialdir=poczatek,
        filetypes=[("Pliki CSV", "*.csv"), ("Wszystkie pliki", "*.*")],
    )
    root.destroy()
    return sciezka or None


def wczytaj_pozycje(csv_path: str) -> list[Pozycja]:
    """Czyta CSV: kolumny indeksDost, ilosc, cena."""
    pozycje = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            indeks = (row.get("indeksDost") or "").strip()
            ilosc = (row.get("ilosc") or "").strip().replace(",", ".")
            cena = (row.get("cena") or "").strip().replace(",", ".")
            if not indeks:
                continue
            pozycje.append(Pozycja(indeksDost=indeks, ilosc=ilosc, cena=cena))
    return pozycje


class FakturaSession:
    """Łączy Playwright (logowanie + ciasteczka) z requests (POST-y)."""

    def __init__(self, base_url: str = "", headful: bool = True):
        self.base_url = base_url.rstrip("/")
        self.headful = headful
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.http = requests.Session()

    def start_browser(self):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright nie jest zainstalowany.")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=not self.headful)
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 900})
        self.page = self.context.new_page()

    def goto_and_wait_login(self):
        if self.base_url:
            try:
                self.page.goto(self.base_url, wait_until="domcontentloaded")
            except Exception:
                pass
        print("\n" + "=" * 64)
        print("  PRZEGLADARKA OTWARTA.")
        print("  1) Zaloguj sie do maggo (jesli trzeba).")
        print("  2) Wejdz na wlasciwa fakture -> zakladka 'Pozycje'")
        print("     (musi byc widoczny przycisk 'Dodaj' i tabela pozycji).")
        print("  3) Wroc tutaj i nacisnij ENTER.")
        print("=" * 64)
        input("  -> Gotowe? Nacisnij ENTER... ")

    def _wait_page_ready(self):
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

    def _safe_eval(self, js: str, retries: int = 5):
        last_err = None
        for _ in range(retries):
            try:
                return self.page.evaluate(js)
            except Exception as e:
                last_err = e
                msg = str(e)
                if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                    self._wait_page_ready()
                    time.sleep(0.5)
                    continue
                time.sleep(0.5)
        raise RuntimeError(
            "Nie udalo sie odczytac danych ze strony (strona ciagle sie przeladowuje). "
            "Upewnij sie, ze karta jest na widoku faktury z zakladka 'Pozycje'. "
            f"Szczegoly: {last_err}"
        )

    def sync_cookies_to_requests(self):
        for c in self.context.cookies():
            self.http.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        try:
            ua = self._safe_eval("() => navigator.userAgent")
        except Exception:
            ua = "Mozilla/5.0"
        self.http.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": ua,
            "Origin": self.base_url,
        })

    def detect_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        self._wait_page_ready()
        origin = self._safe_eval("() => window.location.origin")
        self.base_url = origin.rstrip("/")
        self.http.headers["Origin"] = self.base_url
        return self.base_url

    def detect_df_id(self) -> Optional[str]:
        js = r"""
        () => {
            const el = document.querySelector('#addDokFaktPozFaktId');
            if (el && el.value) return el.value;
            const btn = document.querySelector("[onclick*='showAddDokFaktPozModal(']");
            if (btn) {
                const m = btn.getAttribute('onclick').match(/showAddDokFaktPozModal\((\d+)\)/);
                if (m) return m[1];
            }
            return null;
        }
        """
        return self._safe_eval(js)

    def get_fresh_token(self) -> str:
        js = r"""
        () => {
            const form = document.querySelector('#addDokFaktPozForm');
            if (form) {
                const t = form.querySelector("input[name='__RequestVerificationToken']");
                if (t) return t.value;
            }
            const any = document.querySelector("input[name='__RequestVerificationToken']");
            return any ? any.value : null;
        }
        """
        token = self._safe_eval(js)
        if not token:
            raise RuntimeError(
                "Nie znaleziono tokenu na stronie. Upewnij sie, ze jestes na fakturze "
                "z zakladka 'Pozycje' (formularz dodawania musi istniec na stronie)."
            )
        return token

    def post_pozycja(self, df_id: str, token: str, p: Pozycja, timeout: int = 30) -> tuple[bool, str]:
        url = self.base_url + ADD_POZ_PATH
        data = {
            "indeksDost": p.indeksDost,
            "ilosc": p.ilosc,
            "cena": p.cena,
            "walId": WAL_ID_PLN,
            "stawkaVAT": STAWKA_VAT_23,
            "dfId": df_id,
            TOKEN_FIELD: token,
        }
        headers = {"Referer": self.base_url + "/Dokumenty/dokPanel"}
        try:
            r = self.http.post(url, data=data, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            return False, f"blad polaczenia: {e}"
        if r.status_code == 403:
            return False, "403 - sesja wygasla lub zly token"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        try:
            j = r.json()
            return (j.get("type") == "SUCCESS"), f"{j.get('type','?')} {j.get('message','')}".strip()
        except ValueError:
            return True, "200 (odpowiedz nie-JSON)"

    def stop(self):
        for closer in (
            lambda: self.context and self.context.close(),
            lambda: self.browser and self.browser.close(),
            lambda: self.playwright and self.playwright.stop(),
        ):
            try:
                closer()
            except Exception:
                pass


def _pokaz_blad_okienkiem(tekst: str):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror("Wpisywanie do maggo - blad", tekst)
        root.destroy()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Automatyczne dodawanie pozycji faktury (maggo).")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--df-id", default="")
    ap.add_argument("--csv", default="")
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--refresh-token-every", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    csv_path = args.csv
    if not csv_path:
        print("Otwieram okno wyboru pliku CSV...")
        csv_path = wybierz_plik_okienkiem()
    if not csv_path:
        msg = "Nie wybrano pliku CSV. Uruchom ponownie i wskaz plik pobrany z apki."
        print(msg); _pokaz_blad_okienkiem(msg); return
    if not os.path.isfile(csv_path):
        msg = f"Nie znaleziono pliku:\n{csv_path}"
        print(msg); _pokaz_blad_okienkiem(msg); return

    try:
        pozycje = wczytaj_pozycje(csv_path)
    except Exception as e:
        msg = f"Nie udalo sie wczytac CSV:\n{e}"
        print(msg); _pokaz_blad_okienkiem(msg); return

    if not pozycje:
        msg = f"Plik CSV nie zawiera pozycji (kolumny: indeksDost, ilosc, cena).\n{csv_path}"
        print(msg); _pokaz_blad_okienkiem(msg); return

    print(f"Wczytano {len(pozycje)} pozycji z:\n  {csv_path}")

    if not PLAYWRIGHT_AVAILABLE:
        msg = ("Playwright nie jest zainstalowany.\n\n"
               "Otworz wiersz polecen i wpisz:\n"
               "  pip install playwright requests\n"
               "  playwright install chromium")
        print(msg); _pokaz_blad_okienkiem(msg); return

    sess = FakturaSession(base_url=args.base_url, headful=True)
    sess.start_browser()
    try:
        sess.goto_and_wait_login()
        base = sess.detect_base_url()
        print(f"  Serwer: {base}")

        df_id = args.df_id or sess.detect_df_id()
        if not df_id:
            msg = ("Nie udalo sie rozpoznac, ktora to faktura.\n\n"
                   "Upewnij sie, ze w przegladarce jestes NA FAKTURZE, na zakladce "
                   "'Pozycje' (widoczny przycisk 'Dodaj'), i sprobuj ponownie.")
            print(msg); _pokaz_blad_okienkiem(msg); return
        print(f"  Faktura (dfId): {df_id}")

        sess.sync_cookies_to_requests()
        token = sess.get_fresh_token()
        print(f"  Token pobrany ({len(token)} znakow).")

        if args.dry_run:
            print("\n[PROBA] Tak wygladalyby zadania (nic nie wysylam):")
            for i, p in enumerate(pozycje, 1):
                print(f"  {i:2d}. indeksDost={p.indeksDost!r} ilosc={p.ilosc} cena={p.cena} dfId={df_id}")
            print("\n[PROBA] Koniec.")
            input("\n  Nacisnij ENTER, aby zamknac... ")
            return

        print(f"\nWpisuje {len(pozycje)} pozycji do faktury {df_id}...\n")
        ok_count = 0
        fail = []
        for i, p in enumerate(pozycje, 1):
            if args.refresh_token_every and i > 1 and (i - 1) % args.refresh_token_every == 0:
                try:
                    token = sess.get_fresh_token()
                except Exception:
                    pass
            success, msg = sess.post_pozycja(df_id, token, p)
            status = "OK " if success else "BLAD"
            print(f"  [{status}] {i:2d}/{len(pozycje)}  {p.indeksDost:<22} il={p.ilosc:<5} cena={p.cena:<8} | {msg}")
            if success:
                ok_count += 1
            else:
                fail.append((i, p.indeksDost, msg))
            time.sleep(args.pause)

        print("\n" + "=" * 64)
        print(f"  GOTOWE: dodano {ok_count}/{len(pozycje)} pozycji do faktury {df_id}.")
        if fail:
            print(f"  Nieudane ({len(fail)}):")
            for i, idx, msg in fail:
                print(f"    {i:2d}. {idx} -> {msg}")
            print("\n  Jesli powtarza sie blad 403, zamknij to okno i uruchom ponownie,")
            print("  logujac sie swiezo do maggo.")
        print("=" * 64)
        input("\n  Nacisnij ENTER, aby zamknac przegladarke... ")
    except Exception as e:
        msg = f"Wystapil blad:\n{e}"
        print(msg); _pokaz_blad_okienkiem(msg)
    finally:
        sess.stop()


if __name__ == "__main__":
    main()
