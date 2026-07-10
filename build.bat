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
    --add-data "icon.ico;." ^
    main.py

echo.
if exist output\dist\OdooBackupTool.exe (
    echo  LISTO: output\dist\OdooBackupTool.exe generado correctamente.
) else (
    echo  ERROR: no se genero el ejecutable. Revisa los mensajes anteriores.
)
echo.
pause
