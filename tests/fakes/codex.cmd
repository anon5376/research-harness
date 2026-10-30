@echo off
@setlocal
@if not defined RH_TEST_PYTHON set "RH_TEST_PYTHON=python"
@"%RH_TEST_PYTHON%" "%~dp0codex" %*
@exit /b %errorlevel%
