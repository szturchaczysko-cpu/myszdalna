@echo off
REM ============================================================
REM  Wpisz_do_maggo.bat - odpalacz wpisywania faktur do maggo.
REM  Uruchamia Chrome (osobny profil, z portem debugowania),
REM  a potem fakt_filler.py - ktory podlacza sie do tego Chrome.
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
    pause
    exit /b 1
)

REM --- znajdz chrome.exe ---
set "CHROME="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if "%CHROME%"=="" (
    echo [UWAGA] Nie znaleziono Chrome w standardowej lokalizacji.
    echo Skrypt sprobuje otworzyc wlasne okno przegladarki (tryb zapasowy).
    echo.
    goto URUCHOM_SKRYPT
)

REM --- uruchom Chrome z portem debugowania na OSOBNYM profilu ---
REM    osobny profil = nie rusza Twoich zwyklych okien Chrome.
REM    Pierwszy raz zaloguj sie w tym oknie do maggo (zapamieta sesje).
set "PROFIL=%LOCALAPPDATA%\MaggoChromeProfil"
echo Otwieram Chrome (osobne okno do wpisywania faktur)...
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%PROFIL%" "https://maggo.autossilniki.com"

REM --- daj Chrome chwile na start zanim podlaczy sie skrypt ---
timeout /t 3 >nul

:URUCHOM_SKRYPT
REM --- uruchom skrypt (podlaczy sie do Chrome przez port 9222) ---
python "%~dp0fakt_filler.py" %*

echo.
echo (Okno mozesz teraz zamknac.)
pause >nul
