@echo off
set PYTHON=C:\Users\hzpfly\AppData\Local\Programs\Python\Python312\python.exe
set PIP=C:\Users\hzpfly\AppData\Local\Programs\Python\Python312\Scripts\pip.exe
echo ============================================
echo  鸡蛋期货1分钟行情实时监控
echo ============================================
echo.
echo [0] 安装/更新依赖...
%PIP% install -r requirements.txt -q
echo.
echo 启动程序（按 Ctrl+C 退出）
echo   [1] 终端文字模式（egg_futures_1min.py）
echo   [2] 图形K线模式（egg_futures_chart.py）
echo.
set /p choice=请输入选项 (1 or 2): 
if "%choice%"=="2" (
    %PYTHON% egg_futures_chart.py
) else (
    %PYTHON% egg_futures_1min.py
)
pause
