@echo off
rem Double-click to start the dashboard; an app-style window opens by itself.
cd /d "%~dp0dashboard"
start "star-assistant-dash" /min pythonw server.py
