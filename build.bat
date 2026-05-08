@echo off
cd /d "%~dp0"
echo.
echo  ============================================
echo   JiraBoard - Build con PyInstaller
echo  ============================================
echo.
echo  Instalando PyInstaller...
uv run --with pyinstaller python -m PyInstaller --version
if %errorlevel% neq 0 (
    echo  ERROR: No se pudo encontrar PyInstaller.
    pause
    exit /b 1
)

echo.
echo  Compilando JiraBoard.exe (--onefile)...
echo  (Esto puede tardar 1-2 minutos)
echo.

uv run --with pyinstaller --with flask --with requests --with selenium ^
    --with webdriver-manager --with keyboard --with pystray --with pillow ^
    --with urllib3 python -m PyInstaller ^
    --onefile ^
    --noconsole ^
    --name JiraBoard ^
    --add-data "templates;templates" ^
    --add-data "version.txt;." ^
    --hidden-import flask ^
    --hidden-import flask.templating ^
    --hidden-import jinja2 ^
    --hidden-import requests ^
    --hidden-import selenium ^
    --hidden-import webdriver_manager ^
    --hidden-import webdriver_manager.chrome ^
    --hidden-import keyboard ^
    --hidden-import pystray ^
    --hidden-import PIL ^
    --hidden-import PIL.Image ^
    --hidden-import PIL.ImageDraw ^
    --hidden-import urllib3 ^
    --hidden-import sqlite3 ^
    --hidden-import tkinter ^
    main.py

if %errorlevel% neq 0 (
    echo.
    echo  ERROR en la compilacion. Revisa los mensajes anteriores.
    pause
    exit /b 1
)

echo.
echo  ============================================
echo   BUILD COMPLETADO
echo  ============================================
echo.
echo  Ejecutable: dist\JiraBoard.exe
echo.
echo  COMO DISTRIBUIR:
echo  1. Copia dist\JiraBoard.exe a cualquier carpeta
echo  2. La primera vez que se ejecute, crea .env con las credenciales
echo     (o usa el panel de control para configurarlas)
echo  3. board.db y screenshots/ se crean automaticamente
echo.
pause
