"""
ksef_xml_parser.py — odczyt faktury z pliku XML KSeF (schemat FA(2)/FA(3)).

To NAJLEPSZE źródło danych: XML KSeF ma urzędowo ustaloną strukturę, więc
jeden parser obsługuje KAŻDEGO dostawcę wystawiającego w KSeF. Każde pole jest
jednoznaczne (cena, ilość, wartość) — zero zgadywania, zero OCR, zero łamania linii.

Mapowanie pól (na pozycję, element <FaWiersz>):
  P_7   — nazwa towaru (u nas: indeks dostawcy)
  P_8A  — miara (szt./kpl./...)
  P_8B  — ilość
  P_9A  — cena jednostkowa netto
  P_11  — wartość netto pozycji
Nagłówek (element <Fa> i <Podmiot1>):
  Podmiot1/DaneIdentyfikacyjne/Nazwa — sprzedawca (dostawca)
  P_1   — data wystawienia
  P_2   — numer faktury
  KodWaluty — waluta
  P_13_1 — suma netto faktury
"""

from __future__ import annotations
import re
from typing import Optional
import xml.etree.ElementTree as ET


def _na_float(s):
    if s is None:
        return None
    s = str(s).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def czy_to_ksef_xml(file_bytes: bytes) -> bool:
    """Wykrywa, czy to faktura XML KSeF (po elemencie Faktura + przestrzeni crd.gov.pl)."""
    try:
        glowa = file_bytes[:2000].decode("utf-8", errors="ignore").lower()
    except Exception:
        return False
    return ("<tns:faktura" in glowa or "<faktura" in glowa) and "crd.gov.pl" in glowa


def _local(tag: str) -> str:
    """Zdejmuje przestrzeń nazw z taga: '{...}P_7' -> 'P_7'."""
    return tag.rsplit("}", 1)[-1]


def _znajdz(elem, nazwa_lokalna: str):
    """Pierwszy potomek (na dowolnej głębokości) o danej nazwie lokalnej."""
    for e in elem.iter():
        if _local(e.tag) == nazwa_lokalna:
            return e
    return None


def _tekst(elem, nazwa_lokalna: str) -> Optional[str]:
    e = _znajdz(elem, nazwa_lokalna)
    return e.text.strip() if (e is not None and e.text) else None


def parsuj_ksef_xml(file_bytes: bytes):
    """
    Parsuje fakturę XML KSeF. Zwraca (pozycje, naglowek, blad) — wspólny kontrakt:
      pozycje  — [{lp, indeks, ilosc, cena_netto}]
      naglowek — {dostawca, numer_dokumentu, data, waluta, suma_netto_faktury}
    """
    try:
        root = ET.fromstring(file_bytes)
    except Exception as e:
        return None, None, f"Błąd parsowania XML: {e}"

    # ── nagłówek ──
    naglowek = {"dostawca": "", "numer_dokumentu": "", "data": "", "waluta": "PLN",
                "suma_netto_faktury": None}

    # sprzedawca = Podmiot1 / DaneIdentyfikacyjne / Nazwa
    podmiot1 = None
    for e in root.iter():
        if _local(e.tag) == "Podmiot1":
            podmiot1 = e
            break
    if podmiot1 is not None:
        nazwa = _tekst(podmiot1, "Nazwa")
        if nazwa:
            naglowek["dostawca"] = nazwa

    # element Fa — dane faktury
    fa = None
    for e in root.iter():
        if _local(e.tag) == "Fa":
            fa = e
            break
    if fa is not None:
        # uwaga: szukamy bezpośrednio w Fa, nie w wierszach (P_1 vs ...).
        for dziecko in fa:
            ln = _local(dziecko.tag)
            if ln == "P_2" and dziecko.text:
                naglowek["numer_dokumentu"] = dziecko.text.strip()
            elif ln == "P_1" and dziecko.text:
                naglowek["data"] = dziecko.text.strip()
            elif ln == "KodWaluty" and dziecko.text:
                naglowek["waluta"] = dziecko.text.strip()
            elif ln == "P_13_1" and dziecko.text:
                naglowek["suma_netto_faktury"] = _na_float(dziecko.text)

    # ── pozycje: wszystkie elementy FaWiersz ──
    pozycje = []
    for w in root.iter():
        if _local(w.tag) != "FaWiersz":
            continue
        lp = _tekst(w, "NrWierszaFa")
        indeks = _tekst(w, "P_7") or ""
        ilosc = _na_float(_tekst(w, "P_8B"))
        cena = _na_float(_tekst(w, "P_9A"))
        wartosc = _na_float(_tekst(w, "P_11"))
        # gdyby ceny jednostkowej brakło, policz z wartość/ilość
        if cena is None and wartosc is not None and ilosc:
            cena = round(wartosc / ilosc, 2)
        if indeks and ilosc is not None and cena is not None:
            pozycje.append({
                "lp": int(lp) if lp and lp.isdigit() else len(pozycje) + 1,
                "indeks": indeks.strip(),
                "ilosc": ilosc,
                "cena_netto": cena,
            })

    if not pozycje:
        return None, None, "XML KSeF: nie znaleziono pozycji (FaWiersz)."

    return pozycje, naglowek, None
