@echo off
rem 承認済みのいいね・フォロー・RTを実行する(Museが schtasks /run /tn "Ops-EngageRun" で起動する)
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "logs\engage" mkdir "logs\engage"
".venv\Scripts\python.exe" actions\engage_run.py --drain %* >> "logs\engage\drain.log" 2>&1
