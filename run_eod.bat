@echo off
REM OWNDAYS EOD Processor - Scheduled Task entry point
REM Logs go to eod_processor.log (handled by main.py)

cd /d "D:\Source Codes\ai_coding\OWNDAYS AU Sales Data Extraction"

"C:\Python312\python.exe" src\main.py

exit /b %ERRORLEVEL%
