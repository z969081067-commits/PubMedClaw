@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-pubmed-search.ps1" %*
exit /b %ERRORLEVEL%
