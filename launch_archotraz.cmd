@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
for %%V in (3.13 3.12 3.11) do (
  if not defined PYEXE (
    py -%%V -c "import sys; print(sys.version)" >nul 2>&1 && set "PYEXE=py -%%V"
  )
)
if not defined PYEXE (
  python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
  echo ARCHOTRAZ requires CPython 3.11 or newer.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating isolated ARCHOTRAZ environment...
  %PYEXE% -m venv .venv || goto :fail
)

set "PYTHONPATH=%CD%\src"
".venv\Scripts\python.exe" -m archotraz --data-dir "%CD%\.archotraz" serve --open-browser
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo ARCHOTRAZ failed to start. The window will remain open so the error can be inspected.
pause
exit /b 1
