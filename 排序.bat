@echo off
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 并加入 PATH。
    pause
    exit /b 1
)
python "聊天记录排序.py" %*
echo.
pause
