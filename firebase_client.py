"""
Firebase client — połączenie z Firestore (wspólny projekt).

Minimalna wersja: tylko inicjalizacja i get_db().
Funkcje domenowe (lookup dostawców, braki) są w lookup_firestore.py.

Ładowanie credentials — dwa źródła, w kolejności:
  1. STREAMLIT CLOUD: secret FIREBASE_CREDS (Settings → Secrets)
  2. LOKALNIE: plik firebase_key.json w folderze projektu
     (albo ścieżka w zmiennej GOOGLE_APPLICATION_CREDENTIALS)

Klucz NIGDY nie trafia do repo — patrz .gitignore.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

_db = None


def _load_credentials() -> credentials.Certificate:
    # 1) Streamlit Cloud — secret FIREBASE_CREDS
    try:
        import streamlit as st
        if "FIREBASE_CREDS" in st.secrets:
            creds = st.secrets["FIREBASE_CREDS"]
            creds_dict = json.loads(creds) if isinstance(creds, str) else dict(creds)
            return credentials.Certificate(creds_dict)
    except Exception:
        # brak streamlit albo brak sekretu — spróbuj plików niżej
        pass

    # 2) Lokalnie — plik obok projektu
    local_path = Path(__file__).parent / "firebase_key.json"
    if local_path.exists():
        return credentials.Certificate(str(local_path))

    # 3) Lokalnie — ścieżka ze zmiennej środowiskowej
    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path and Path(env_path).exists():
        return credentials.Certificate(env_path)

    raise FileNotFoundError(
        "Nie znaleziono credentials Firebase.\n"
        "  • STREAMLIT CLOUD: ustaw secret FIREBASE_CREDS (Settings → Secrets)\n"
        "  • LOKALNIE: połóż firebase_key.json w folderze projektu"
    )


def get_db():
    """Zwraca klienta Firestore (singleton)."""
    global _db
    if _db is not None:
        return _db

    try:
        app = firebase_admin.get_app()
    except ValueError:
        cred = _load_credentials()
        app = firebase_admin.initialize_app(cred)

    _db = firestore.client(app)
    return _db
