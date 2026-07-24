@echo off
REM ============================================================
REM  Odoo Backup Tool — Windows EXE builder
REM
REM  Salida:
REM    output\dist\OdooBackupTool.exe   <- ejecutable final
REM    output\build\                    <- archivos intermedios (ignorados por git)
REM
REM  Requisitos: pip install -r requirements.txt
REM ============================================================

echo.
echo  Instalando dependencias...
pip install -r requirements.txt

echo.
echo  Generando icono...
python create_icon.py
if not exist icon.ico (
    echo  ADVERTENCIA: no se pudo generar icon.ico. El exe usara icono por defecto.
)

echo.
echo  Cerrando instancias de OdooBackupTool.exe en ejecucion (bloquean el archivo)...
taskkill /F /IM OdooBackupTool.exe >nul 2>&1

echo.
echo  Compilando ejecutable...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "OdooBackupTool" ^
    --icon icon.ico ^
    --distpath output\dist ^
    --workpath output\build ^
    --hidden-import paramiko ^
    --hidden-import paramiko.transport ^
    --hidden-import paramiko.sftp_client ^
    --hidden-import cryptography ^
    --hidden-import PIL ^
    --hidden-import PIL.Image ^
    --hidden-import google.oauth2.service_account ^
    --hidden-import google.auth.transport.requests ^
    --hidden-import google_auth_httplib2 ^
    --hidden-import httplib2 ^
    --hidden-import googleapiclient.discovery ^
    --hidden-import googleapiclient.http ^
    --hidden-import googleapiclient.errors ^
    --hidden-import plyer ^
    --hidden-import plyer.platforms ^
    --hidden-import plyer.platforms.win ^
    --hidden-import plyer.platforms.win.notification ^
    --add-data "icon.ico;." ^
    main.py

set PYI_EXITCODE=%ERRORLEVEL%

echo.
REM Verifica el codigo de salida real de PyInstaller, no solo si el .exe
REM existe: un build fallido (p.ej. PermissionError porque el .exe anterior
REM seguia abierto) deja el archivo VIEJO en su lugar, y "if exist" por si
REM solo reportaria "LISTO" con un ejecutable desactualizado.
if not "%PYI_EXITCODE%"=="0" (
    echo  ERROR: PyInstaller termino con codigo %PYI_EXITCODE%. Revisa los mensajes anteriores.
    echo  El archivo en output\dist\OdooBackupTool.exe, si existe, es el build ANTERIOR — no se actualizo.
) else if exist output\dist\OdooBackupTool.exe (
    echo  LISTO: output\dist\OdooBackupTool.exe generado correctamente.
) else (
    echo  ERROR: PyInstaller reporto exito pero no se encontro el ejecutable. Revisa los mensajes anteriores.
)
echo.
pause
