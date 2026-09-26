@echo off
chcp 65001 > nul
rem ============================================================================
rem  silgeorae 매일 자동 업데이트 - Windows 작업 스케줄러용
rem ----------------------------------------------------------------------------
rem  1) 이 파일을 silgeorae.toml 이 있는 폴더에 두세요.
rem     (다른 곳에 두려면 아래 WORKDIR 를 그 폴더 경로로 바꾸세요)
rem  2) 가상환경(.venv)에 설치했다면 그 python.exe 를 자동으로 씁니다.
rem  3) 작업 스케줄러 - 기본 작업 만들기 - 트리거 "매일" -
rem     동작 "프로그램 시작" - 이 파일(run_update.bat) 선택
rem  4) 실행 기록은 WORKDIR\logs\update.log 에 쌓입니다.
rem  인증키를 설정 파일에 적지 않았다면 아래 줄의 rem 을 지우고 키를 넣으세요.
rem set "SILGEORAE_SERVICE_KEY=여기에_일반_인증키"
rem ============================================================================
set "WORKDIR=%~dp0"
if "%WORKDIR:~-1%"=="\" set "WORKDIR=%WORKDIR:~0,-1%"
set "PYTHON=%WORKDIR%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%WORKDIR%" || exit /b 1
if not exist logs mkdir logs

echo [%date% %time%] silgeorae update 시작 >> logs\update.log
"%PYTHON%" -m silgeorae update --report >> logs\update.log 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] 종료 코드 %RC% (0 성공, 1 실행 오류, 2 설정 오류) >> logs\update.log
exit /b %RC%
