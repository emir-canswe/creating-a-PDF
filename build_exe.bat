@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] PyInstaller kuruluyor / guncelleniyor...
python -m pip install -q pyinstaller

echo [2/2] EXE olusturuluyor (bir kac dakika surebilir)...
pyinstaller --noconfirm AkilliMetinPDF.spec

if errorlevel 1 (
  echo.
  echo HATA: Derleme basarisiz.
  pause
  exit /b 1
)

echo.
echo Tamam. Calistirilabilir dosya:
echo   %~dp0dist\AkilliMetinPDF.exe
echo.
pause
