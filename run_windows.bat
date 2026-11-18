@echo off
cd /d "%~dp0"
py -m pip install -q -r requirements.txt
py hsb_gui.py
