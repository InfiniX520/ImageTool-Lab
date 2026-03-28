@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD="

if defined CONDA_PREFIX (
  if exist "%CONDA_PREFIX%\python.exe" set "PY_CMD=%CONDA_PREFIX%\python.exe"
)

if not defined PY_CMD (
  if exist "E:\Miniconda03\envs\JuiLiu0\python.exe" set "PY_CMD=E:\Miniconda03\envs\JuiLiu0\python.exe"
)

if not defined PY_CMD (
  where python >nul 2>nul
  if %errorlevel%==0 set "PY_CMD=python"
)

if not defined PY_CMD (
  where py >nul 2>nul
  if %errorlevel%==0 set "PY_CMD=py -3"
)

echo [1/4] Resolve Python interpreter...
if not defined PY_CMD (
  echo [ERROR] Python not found.
  echo [HINT] Run: conda activate JuiLiu0
  pause
  exit /b 1
)

echo [OK] Using: %PY_CMD%

echo [2/4] Install requirements...
%PY_CMD% -m pip --disable-pip-version-check install -r requirements.txt
if %errorlevel% neq 0 (
  echo [ERROR] Dependency installation failed.
  pause
  exit /b 1
)

echo [3/4] Launch GUI...
echo [INFO] If no window appears, check the taskbar first.
%PY_CMD% main.py
if %errorlevel% neq 0 (
  echo [ERROR] Program exited with code %errorlevel%.
  pause
)

endlocal
