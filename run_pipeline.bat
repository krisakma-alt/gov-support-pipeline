@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [%date% %time%] 파이프라인 시작
echo.
python main.py
echo.
echo [%date% %time%] 파이프라인 종료
echo.
pause
