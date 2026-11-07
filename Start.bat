@echo off
rem Starts the GSC X TWS Loadout Blacklister. Python 3 must be installed (python.org, tick "py launcher").
rem customtkinter is installed automatically on first start.
cd /d "%~dp0"
set "PYC=py -3"
where py >nul 2>nul || set "PYC=python"
%PYC% -c "import customtkinter" >nul 2>nul || (echo Installing customtkinter ... & %PYC% -m pip install customtkinter)
%PYC% -c "import customtkinter" >nul 2>nul || (echo Python 3 or customtkinter is missing. Please install Python from python.org. & pause & exit /b)
if "%PYC%"=="py -3" (start "" pyw -3 Blacklister.py) else (start "" pythonw Blacklister.py)
