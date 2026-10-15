@echo off
rem Vallotton on Windows: double-click to start.
rem
rem Everything lives in .runtime\ inside this folder: a virtual environment
rem with the backend's dependencies, and -- only if this computer has no
rem Python 3.12 or newer -- a downloaded CPython next to it. Nothing is
rem installed anywhere else and no environment variable is changed; delete
rem .runtime\ and it is as if nothing happened.
setlocal
cd /d "%~dp0"
set "PY=.runtime\venv\Scripts\python.exe"
if exist "%PY%" goto run

echo [Vallotton] First run: preparing a project-local Python under .runtime\ ...
if not exist ".runtime" mkdir ".runtime"

rem ---- a Python already on this computer? (the py launcher, then python on PATH)
set "BASE="
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
for %%c in ("py -3" "python") do (
  if not defined BASE (
    %%~c -c "%CHECK%" >nul 2>nul && for /f "delims=" %%p in ('%%~c -c "import sys; print(sys.executable)"') do set "BASE=%%p"
  )
)
if defined BASE (
  echo [Vallotton] Using the Python already installed: %BASE%
  "%BASE%" -m venv ".runtime\venv" || goto venv_failed
  "%PY%" -m pip install -q --disable-pip-version-check -r backend\requirements.txt rich || goto deps_failed
  goto run
)

rem ---- no Python: uv (a small single-file tool) downloads one into .runtime\python
echo [Vallotton] No Python 3.12+ found; downloading one into .runtime\ (about 40 MB) ...
set "UV=.runtime\uv\uv.exe"
if not exist "%UV%" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR = '%CD%\.runtime\uv'; $env:UV_NO_MODIFY_PATH = '1'; irm https://astral.sh/uv/install.ps1 | iex"
)
if not exist "%UV%" goto download_failed
set "UV_PYTHON_INSTALL_DIR=%CD%\.runtime\python"
set "UV_CACHE_DIR=%CD%\.runtime\cache"
"%UV%" python install 3.12 || goto download_failed
"%UV%" venv ".runtime\venv" --python 3.12 --seed || goto venv_failed
"%UV%" pip install --python "%PY%" -r backend\requirements.txt rich || goto deps_failed

:run
"%PY%" quickstart.py %*
if errorlevel 1 pause
endlocal
exit /b

:download_failed
echo [Vallotton] Could not download Python. Check the network (or a proxy), then run start.bat again.
pause
exit /b 1

:venv_failed
echo [Vallotton] Could not create the virtual environment in .runtime\venv.
pause
exit /b 1

:deps_failed
echo [Vallotton] Could not install the backend's dependencies. Check the network, then run start.bat again.
pause
exit /b 1
