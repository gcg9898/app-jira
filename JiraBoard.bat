@echo off
cd /d "C:\Users\gcarmong\OneDrive - NTT DATA EMEAL\Desktop\desarrollos\app jira"
uv run --with flask --with requests --with selenium --with webdriver-manager --with keyboard --with pystray --with pillow --link-mode=copy python launcher.py
