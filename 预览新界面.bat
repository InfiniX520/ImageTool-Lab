@echo off
setlocal
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PY_CMD="
for %%F in ("%~dp0.venv-path" "%~dp0..\.venv-path") do (
  if not defined PY_CMD if exist "%%~F" for /f "usebackq delims=" %%P in ("%%~F") do (
    if not defined PY_CMD if exist "%%P" set "PY_CMD=%%P"
  )
)
if not defined PY_CMD (
  echo [ERROR] Configure the shared interpreter in .venv-path first.
  pause
  exit /b 1
)
cd /d "%~dp0"
"%PY_CMD%" -s -B -X utf8 "%~dp0main.py" --demo %*
set "JPGPNG_EXIT=%errorlevel%"
if not "%JPGPNG_EXIT%"=="0" pause
endlocal & exit /b %JPGPNG_EXIT%
