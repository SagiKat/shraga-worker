@echo off
title Shraga Dev Box Setup
echo.
echo   Starting Shraga setup...
echo   (A browser window will open for sign-in)
echo.
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/SagiKat/shraga-worker/main/setup.ps1 | iex"
echo.
pause
