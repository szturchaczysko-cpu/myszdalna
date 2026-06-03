@echo off
REM Wersja PROBNA - nic nie wpisuje do maggo, tylko pokazuje co by zrobila.
REM Bezpieczna do pierwszego testu.
chcp 65001 >nul
title Wpisywanie do maggo - PROBA (nic nie wysyla)
cd /d "%~dp0"
echo.
echo ============================================================
echo   TRYB PROBNY - nic nie zostanie wpisane do maggo
echo ============================================================
echo.
where python >nul 2>nul
if errorlevel 1 ( echo [BLAD] Brak Pythona - zainstaluj z python.org & pause & exit /b 1 )
if not exist "%~dp0fakt_filler.py" ( echo [BLAD] Brak fakt_filler.py w tym folderze & pause & exit /b 1 )
python "%~dp0fakt_filler.py" --dry-run
echo.
pause >nul
