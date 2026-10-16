@echo off
rem Vallotton on Windows: double-click to start.
rem
rem Everything lives in .runtime\ inside this folder: a virtual environment
rem with the backend's dependencies, and -- only if this computer has no
rem Python 3.12 or newer that can set that environment up -- a downloaded
rem CPython next to it. Nothing is installed anywhere else and no environment
rem variable is changed; delete .runtime\ and it is as if nothing happened.
setlocal
cd /d "%~dp0"
set "PY=.runtime\venv\Scripts\python.exe"
set "NEED=import django, httpx, PIL, whitenoise, rich"

rem ---- an environment from an earlier run: use it only if it is whole. One
rem whose dependencies never finished installing is set up again, not run.
if exist "%PY%" (
  "%PY%" -c "%NEED%" >nul 2>nul && goto run
  echo [Vallotton] The environment in .runtime\venv is incomplete; setting it up again ...
  rmdir /s /q ".runtime\venv"
)

echo [Vallotton] First run: preparing a project-local Python under .runtime\ ...
if not exist ".runtime" mkdir ".runtime"

rem ---- a Python already on this computer, 3.12 or newer, that can make a
rem venv with pip in it (the py launcher first, then python on PATH)
set "BASE="
set "CHECK=import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
for %%c in ("py -3" "python") do (
  if not defined BASE (
    %%~c -c "%CHECK%" >nul 2>nul && for /f "delims=" %%p in ('%%~c -c "import sys; print(sys.executable)"') do set "BASE=%%p"
  )
)
if defined BASE (
  echo [Vallotton] Using the Python already installed: %BASE%
  call :with_system
  if not errorlevel 1 goto run
  echo [Vallotton] That Python could not set the environment up; downloading a separate one instead ...
  if exist ".runtime\venv" rmdir /s /q ".runtime\venv"
)

rem ---- no usable Python: uv (a small single-file tool) downloads one into .runtime\python
echo [Vallotton] Downloading a Python into .runtime\ (about 40 MB) ...
set "UV=.runtime\uv\uv.exe"
if not exist "%UV%" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR = '%CD%\.runtime\uv'; $env:UV_NO_MODIFY_PATH = '1'; irm https://astral.sh/uv/install.ps1 | iex"
)
if not exist "%UV%" goto download_failed
set "UV_PYTHON_INSTALL_DIR=%CD%\.runtime\python"
set "UV_CACHE_DIR=%CD%\.runtime\cache"
"%UV%" python install 3.12 || goto download_failed
"%UV%" venv ".runtime\venv" --python 3.12 --seed || goto venv_failed
echo [Vallotton] Installing the backend's dependencies (a minute or two) ...
"%UV%" pip install --python "%PY%" -r backend\requirements.txt rich || goto deps_failed
"%PY%" -c "%NEED%" >nul 2>nul || goto deps_failed

:run
"%PY%" quickstart.py %*
if errorlevel 1 pause
endlocal
exit /b

rem ---- the environment from the Python already installed; any step failing
rem returns 1 and the caller falls back to a downloaded Python
:with_system
"%BASE%" -m venv ".runtime\venv" || exit /b 1
"%PY%" -m pip --version >nul 2>nul || exit /b 1
echo [Vallotton] Installing the backend's dependencies (a minute or two; pip shows its progress below) ...
"%PY%" -m pip install --disable-pip-version-check -r backend\requirements.txt rich || exit /b 1
"%PY%" -c "%NEED%" >nul 2>nul || exit /b 1
exit /b 0

:download_failed
echo [Vallotton] Could not download Python. Check the network (or a proxy), then run start.bat again.
pause
exit /b 1

:venv_failed
echo [Vallotton] Could not create the virtual environment in .runtime\venv.
pause
exit /b 1

:deps_failed
echo [Vallotton] Could not install the backend's dependencies; the lines above say why.
echo [Vallotton] Check the network (or a proxy), delete the .runtime folder, then run start.bat again.
pause
exit /b 1
