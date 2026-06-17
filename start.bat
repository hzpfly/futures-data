@echo off
set PYTHON=C:\Users\hzpfly\AppData\Local\Programs\Python\Python312\python.exe

echo ============================================
echo  Egg Futures Monitor (JD / DCE)
echo ============================================
echo.
echo [0] Installing/updating dependencies...
"%PYTHON%" -m pip install -r requirements.txt -q
echo.
echo Select mode:
echo   [1] Terminal mode   (egg_futures_1min.py)
echo   [2] Chart mode      (egg_futures_chart.py)
echo.
set /p choice=Enter 1 or 2: 
if "%choice%"=="2" (
    "%PYTHON%" egg_futures_chart.py
) else (
    "%PYTHON%" egg_futures_1min.py
)
pause
