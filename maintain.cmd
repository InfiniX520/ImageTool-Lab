@echo off
setlocal
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
cd /d "%~dp0"

rem ============================================================
rem  CLI for library preflight / backup / verify / plan / scan
rem  Interpreter resolution order:
rem    1) .venv-path in this project folder
rem    2) .venv-path in the parent folder
rem    3) %CONDA_PREFIX%\python.exe  (when a conda env is active)
rem    4) python on PATH
rem  Never hardcode an interpreter path here.
rem  The .venv-path file must be plain text, LF, WITHOUT BOM.
rem ============================================================

set "PY_CMD="

set "VENV_FILE=%~dp0.venv-path"
if not defined PY_CMD if exist "%VENV_FILE%" (
  for /f "usebackq delims=" %%P in ("%VENV_FILE%") do (
    if not defined PY_CMD if exist "%%P" set "PY_CMD=%%P"
  )
)

set "VENV_FILE=%~dp0..\.venv-path"
if not defined PY_CMD if exist "%VENV_FILE%" (
  for /f "usebackq delims=" %%P in ("%VENV_FILE%") do (
    if not defined PY_CMD if exist "%%P" set "PY_CMD=%%P"
  )
)

if not defined PY_CMD if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "PY_CMD=%CONDA_PREFIX%\python.exe"

if not defined PY_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
  echo [ERROR] Python not found. Run: conda activate JuiLiu
  exit /b 1
)

"%PY_CMD%" -s -B -X utf8 "%~dp0maintain.py" %*
set "MAINTAIN_EXIT=%errorlevel%"
endlocal & exit /b %MAINTAIN_EXIT%
