@echo off
rem Command line: musickit-cli.bat list --iso "Burnout 3 - Takedown.iso"   (see README.md)
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"%~dp0.venv\Scripts\python.exe" -m musickit %*
