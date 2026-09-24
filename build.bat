@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM One-click local build, uv based.
REM ASCII only: a Chinese Windows cmd decodes .bat as GBK and
REM would garble any UTF-8 text.
REM
REM Output: dist\jev-chat-windows\jev-chat-windows.exe
REM Ship the WHOLE dist\jev-chat-windows folder: onedir, so the
REM exe needs the files sitting next to it.
REM ============================================================

REM Tsinghua PyPI mirror: far faster than the default index in China.
set "MIRROR=-i https://pypi.tuna.tsinghua.edu.cn/simple"

REM --- locate uv ----------------------------------------------
REM UVCMD holds a whole command line, so it stays unquoted below.
REM uv often lands in a per-user dir that is not on PATH yet, and
REM it can also be installed as a plain Python package, so try a
REM few spots before giving up.
set "UVCMD=uv"
where uv >nul 2>nul || set "UVCMD="
if not defined UVCMD if exist "%USERPROFILE%\.local\bin\uv.exe" set "UVCMD="%USERPROFILE%\.local\bin\uv.exe""
if not defined UVCMD if exist "%LOCALAPPDATA%\uv\uv.exe" set "UVCMD="%LOCALAPPDATA%\uv\uv.exe""
if not defined UVCMD if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" set "UVCMD="%LOCALAPPDATA%\Programs\uv\uv.exe""
if not defined UVCMD (
    REM last resort: pip install uv
    python -m uv --version >nul 2>nul && set "UVCMD=python -m uv"
)
if not defined UVCMD goto :nouv

echo Using uv: %UVCMD%
%UVCMD% --version || goto :fail

REM --- virtualenv ---------------------------------------------
REM A uv-made .venv has no pip inside, that is fine: everything
REM below goes through uv pip and python -m PyInstaller.
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtualenv .venv ...
    REM 3.11 matches the released builds. Fall back to any Python.
    %UVCMD% venv .venv --python 3.11 || %UVCMD% venv .venv || goto :fail
)

set "PY=.venv\Scripts\python.exe"

REM --- dependencies -------------------------------------------
REM uv takes the index from -i, so requirements.txt needs no edits.
echo Installing dependencies from the Tsinghua mirror ...
%UVCMD% pip install --python "%PY%" %MIRROR% -r requirements.txt pyinstaller || goto :fail

REM --- build --------------------------------------------------
echo Building ...
"%PY%" -m PyInstaller --noconfirm --clean jev.spec || goto :fail

echo.
echo Build OK.
echo   %cd%\dist\jev-chat-windows\jev-chat-windows.exe
echo Ship the whole dist\jev-chat-windows folder: the exe needs the files next to it.
pause
exit /b 0

:nouv
echo.
echo uv not found. Install it first, then run this script again:
echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo Or grab a release from https://github.com/astral-sh/uv/releases
pause
exit /b 1

:fail
echo.
echo Build FAILED. Scroll up for the error.
pause
exit /b 1
