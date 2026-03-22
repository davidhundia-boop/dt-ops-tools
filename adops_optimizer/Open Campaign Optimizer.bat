@echo off
cd /d "%~dp0"
pip install -q -r requirements.txt 2>nul
streamlit run app_streamlit.py --browser.gatherUsageStats false
