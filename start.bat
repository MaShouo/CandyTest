@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
rem The one-click package is always local-only. A stale system environment
rem must never switch it into unauthenticated server mode.
set "CANDYTEST_DEPLOYMENT=local"
set "CANDYTEST_HOST=127.0.0.1"
set "VENV_DIR=%~dp0.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

echo ========================================
echo   CandyTest 中转站 GPT 智商检测
echo ========================================
echo.

if exist "%VENV_PYTHON%" goto check_dependencies

echo [1/3] 正在创建 Python 虚拟环境...
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv "%VENV_DIR%"
) else (
    where python >nul 2>nul
    if errorlevel 1 goto python_missing
    python -m venv "%VENV_DIR%"
)
if errorlevel 1 goto venv_failed

:check_dependencies
echo [2/3] 正在检查运行依赖...
"%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto python_version_failed
"%VENV_PYTHON%" -c "import flask, waitress" >nul 2>nul
if not errorlevel 1 goto start_app

echo 检测到首次运行或依赖不完整，正在安装...
"%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 goto install_failed

:start_app
echo [3/3] 正在启动 CandyTest...
echo 浏览器将自动打开；关闭本窗口即可停止服务。
echo.
"%VENV_PYTHON%" "%~dp0run.py"
if errorlevel 1 goto app_failed
goto end

:python_missing
echo.
echo [错误] 未找到 Python。请先安装 Python 3.10 或更高版本，并勾选“Add Python to PATH”。
goto failed

:venv_failed
echo.
echo [错误] 创建虚拟环境失败。请确认 Python 安装完整。
goto failed

:python_version_failed
echo.
echo [错误] Python 版本过低，需要 Python 3.10 或更高版本。
echo 请删除项目中的 .venv 目录，安装新版 Python 后重新双击 start.bat。
goto failed

:install_failed
echo.
echo [错误] 依赖安装失败。请检查网络、代理或 pip 配置后重试。
goto failed

:app_failed
echo.
echo [错误] CandyTest 启动失败，请查看上方错误信息。
goto failed

:failed
echo.
pause
exit /b 1

:end
endlocal
