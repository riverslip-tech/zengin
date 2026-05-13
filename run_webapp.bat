@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
streamlit run webapp\app.py --server.port=8508 --server.address=127.0.0.1
