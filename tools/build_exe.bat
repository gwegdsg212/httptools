@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在打包 HTML 图片替换工具...
python -m pip install -q pyinstaller windnd pillow
python -m PyInstaller --noconfirm --clean HtmlBase64ImageReplacer.spec
if errorlevel 1 (
    echo 打包失败。
    pause
    exit /b 1
)
echo.
echo 完成：dist\HTML图片替换工具.exe
pause
