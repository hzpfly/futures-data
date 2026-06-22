@echo off
set PYTHON=C:\Users\hzpfly\AppData\Local\Programs\Python\Python312\python.exe

echo ============================================
echo  Futures Monitor (5 contracts: CF JD LH CJ C)
echo ============================================
echo.
echo [0] Installing/updating dependencies...
"%PYTHON%" -m pip install -r requirements.txt -q
echo.
echo Select mode:
echo   [1] Terminal mode         (egg_futures_1min.py)
echo   [2] Chart mode            (egg_futures_chart.py)
echo   [3] EIS monitor           (eis_monitor.py, 25min + daily)
echo   [4] Triple Screen monitor (triple_screen_monitor.py)
echo.
set /p choice=Enter 1, 2, 3 or 4:
if "%choice%"=="2" (
    "%PYTHON%" egg_futures_chart.py
) else if "%choice%"=="3" (
    "%PYTHON%" eis_monitor.py
) else if "%choice%"=="4" (
    "%PYTHON%" triple_screen_monitor.py
) else (
    "%PYTHON%" egg_futures_1min.py
)
pause
