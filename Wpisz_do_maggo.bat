@echo off
REM ============================================================
REM  Wpisz_do_maggo.bat - odpalacz wpisywania faktur do maggo.
REM  Dwuklik w ten plik uruchamia fakt_filler.py lezacy obok.
REM  Nie trzeba zadnych komend ani sciezek.
REM ============================================================
chcp 65001 >nul
title Wpisywanie faktury do maggo
cd /d "%~dp0"

echo.
echo ============================================================
echo   WPISYWANIE FAKTURY DO MAGGO
echo ============================================================
echo.

REM --- sprawdz czy Python jest dostepny ---
where python >nul 2>nul
if errorlevel 1 (
    echo [BLAD] Nie znaleziono Pythona.
    echo.
    echo Trzeba raz zainstalowac Python ze strony python.org
    echo (przy instalacji zaznaczyc dodanie do PATH^).
    echo.
    pause
    exit /b 1
)

REM --- sprawdz czy fakt_filler.py jest obok ---
if not exist "%~dp0fakt_filler.py" (
    echo [BLAD] Brak pliku fakt_filler.py w tym folderze:
    echo   %~dp0
    echo.
    echo Upewnij sie, ze Wpisz_do_maggo.bat i fakt_filler.py
    echo leza razem w tym samym folderze.
    echo.
    pause
    exit /b 1
)

REM --- uruchom skrypt ---
python "%~dp0fakt_filler.py" %*

echo.
echo (Okno mozesz teraz zamknac.)
pause >nul
