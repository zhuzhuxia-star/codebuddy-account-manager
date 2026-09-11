@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 商店版 Python 的 Tcl/Tk 不在默认位置，PyInstaller 打包窗口程序前必须设置，
rem 否则生成的 exe 会因为找不到 init.tcl 而启动失败。
for /f "delims=" %%i in ('python -c "import sys,pathlib;print(pathlib.Path(sys.base_prefix)/'tcl')"') do set "TCLROOT=%%i"
if exist "%TCLROOT%\tcl8.6" set "TCL_LIBRARY=%TCLROOT%\tcl8.6"
if exist "%TCLROOT%\tk8.6" set "TK_LIBRARY=%TCLROOT%\tk8.6"
echo TCL_LIBRARY=%TCL_LIBRARY%

echo ==== 安装依赖 ====
python -m pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple

echo ==== PyInstaller 打包 ====
rem version_info.txt 让 exe 属性里带作者（这是哪头猪？）与仓库地址
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name CodeBuddyAccountManager ^
  --version-file version_info.txt ^
  --distpath dist --workpath build ^
  main.py
if errorlevel 1 goto :fail

echo ==== 部署到 %%APPDATA%%\CodeBuddyAccountManager\bin ====
rem dist 目录下的同名 exe 在部分机器上会被安全软件/索引持续占用而无法启动，
rem 因此实际运行的是 %APPDATA% 下的这份副本，计划任务也指向它。
set "BIN=%APPDATA%\CodeBuddyAccountManager\bin"
if not exist "%BIN%" mkdir "%BIN%"
copy /y "dist\CodeBuddyAccountManager.exe" "%BIN%\CodeBuddyAccountManager.exe"
if errorlevel 1 goto :fail

echo ==== 注册签到任务（指向部署副本）====
"%BIN%\CodeBuddyAccountManager.exe" install-autostart 1
"%BIN%\CodeBuddyAccountManager.exe" install-task 09:05

echo.
echo ==== 完成 ====
echo 构建产物：dist\CodeBuddyAccountManager.exe
echo 运行位置：%BIN%\CodeBuddyAccountManager.exe  （请从这里启动）
pause
exit /b 0

:fail
echo [失败] 打包或部署过程中出错，请检查上面的输出。
pause
exit /b 1
