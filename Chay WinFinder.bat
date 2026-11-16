@echo off
REM Nhap dup de mo WinFinder (giu cua so nay mo trong luc dung tool)
cd /d "%~dp0"
python -m winfinder.cli serve 2>nul || py -m winfinder.cli serve
pause
