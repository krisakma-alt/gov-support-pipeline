@echo off
chcp 65001 >nul
echo 작업 스케줄러에 GovSupportPipeline 등록 중...
schtasks /create /tn "GovSupportPipeline" /xml "%~dp0schedule_task.xml" /f
if %ERRORLEVEL%==0 (
    echo 등록 완료! 매일 오전 9시에 자동 실행됩니다.
    echo 확인: schtasks /query /tn "GovSupportPipeline"
) else (
    echo 등록 실패. 관리자 권한으로 다시 실행해주세요.
)
pause
