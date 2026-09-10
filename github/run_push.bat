@echo off
cd /d "%~dp0\.."
echo Working directory: %CD%
echo.
python "%~dp0push_to_github.py"
echo.
pause
