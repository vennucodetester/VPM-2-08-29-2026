@echo off
setlocal
title Validate VAVE Slide Export
cd /d "%~dp0"

set "CODEX_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEX_PYTHON%" (
    set "PYTHON_EXE=%CODEX_PYTHON%"
    set "PYTHON_ARGS="
    goto :python_found
)

where python >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_EXE=python"
    set "PYTHON_ARGS="
    goto :python_found
)

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
    goto :python_found
)

echo Python was not found. Install Python, then run this validator again.
pause
exit /b 1

:python_found
echo Running VAVE export regression tests...
"%PYTHON_EXE%" %PYTHON_ARGS% -m unittest tests.test_vave_slide_export -v
if errorlevel 1 goto :failed

echo.
echo Generating validation workbook from DG-PH2-6-25.vpmt...
"%PYTHON_EXE%" %PYTHON_ARGS% "%~dp0EXTRA\validate_vave_slide_export.py"
if errorlevel 1 goto :failed

echo.
echo VALIDATION PASSED
if not "%VPM_VALIDATE_NO_OPEN%"=="1" (
    start "" "%~dp0..\outputs\vave-slide-pack\VAVE-Slide-Pack-Validation.xlsx"
)
pause
exit /b 0

:failed
echo.
echo VALIDATION FAILED
pause
exit /b 1
