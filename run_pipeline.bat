@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [%date% %time%] 파이프라인 시작
python main.py
echo [%date% %time%] 파이프라인 종료 (exit code: %ERRORLEVEL%)
