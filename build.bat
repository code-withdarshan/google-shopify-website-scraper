@echo off
REM ============================================================
REM  Build standalone ShopifyVerticalScraper.exe
REM  Output:  release\ShopifyVerticalScraper.exe
REM ============================================================
cd /d "%~dp0"

echo.
echo [1/4] Installing build tools...
py -m pip install --upgrade pip pyinstaller websocket-client
if errorlevel 1 (
  echo ERROR: pip install failed. Make sure "py" works in this terminal.
  pause & exit /b 1
)

echo.
echo [2/4] Installing project dependencies...
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo ERROR: requirements install failed.
  pause & exit /b 1
)

echo.
echo [3/4] Running PyInstaller (1-2 minutes)...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist ShopifyVerticalScraper.spec del /q ShopifyVerticalScraper.spec

py -m PyInstaller --noconfirm --clean ^
  --name ShopifyVerticalScraper ^
  --onefile ^
  --console ^
  --add-data "index.html;." ^
  --add-data "README_END_USER.md;." ^
  --collect-all browser_cookie3 ^
  --collect-all openai ^
  --collect-all flask ^
  --collect-all flask_cors ^
  --collect-submodules websocket ^
  --hidden-import=websocket ^
  --hidden-import=bs4 ^
  backend.py

if errorlevel 1 (
  echo ERROR: PyInstaller failed.
  pause & exit /b 1
)

echo.
echo [4/4] Packaging release folder...
if exist release rmdir /s /q release
mkdir release
copy /Y dist\ShopifyVerticalScraper.exe release\ >nul
copy /Y README_END_USER.md release\README.md >nul
powershell -NoProfile -Command "Compress-Archive -Path release\* -DestinationPath ShopifyVerticalScraper.zip -Force"

echo.
echo ============================================================
echo  BUILD COMPLETE
echo ============================================================
echo  EXE:  %CD%\release\ShopifyVerticalScraper.exe
echo  ZIP:  %CD%\ShopifyVerticalScraper.zip
echo ============================================================
echo.
pause
