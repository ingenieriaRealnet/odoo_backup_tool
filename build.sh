#!/bin/bash
# ============================================================
#  Odoo Backup Tool — Linux build script
#
#  Salida:
#    output/dist/OdooBackupTool   <- ejecutable final (ELF)
#    output/build/                <- archivos intermedios
#
#  Requisitos: pip3 install -r requirements.txt
#              tkinter disponible (apt: python3-tk)
# ============================================================

set -e  # Detener en el primer error

echo ""
echo "  Instalando dependencias..."
pip3 install -r requirements.txt

echo ""
echo "  Generando icono..."
python3 create_icon.py
if [ ! -f icon.ico ]; then
    echo "  ADVERTENCIA: no se pudo generar icon.ico. El ejecutable usara icono por defecto."
fi

echo ""
echo "  Cerrando instancias de OdooBackupTool en ejecucion (bloquean el archivo)..."
pkill -f "output/dist/OdooBackupTool" 2>/dev/null || true

# En Linux el parametro --add-data usa ':' como separador (no ';' como en Windows).
# Mismos --hidden-import que build.bat (Windows) para que ambos builds tengan
# el mismo contenido funcional — build.sh estaba desincronizado y le faltaban
# los modulos de Google Drive (gdrive.py) y plyer, que PyInstaller no siempre
# detecta solo por analisis estatico (imports dinamicos/condicionales).
# plyer.platforms.win.notification es especifico de Windows; en Linux el
# modulo equivalente es plyer.platforms.linux.notification.
set +e
pyinstaller \
    --onefile \
    --windowed \
    --name "OdooBackupTool" \
    --distpath output/dist \
    --workpath output/build \
    --hidden-import paramiko \
    --hidden-import paramiko.transport \
    --hidden-import paramiko.sftp_client \
    --hidden-import cryptography \
    --hidden-import PIL \
    --hidden-import PIL.Image \
    --hidden-import google.oauth2.service_account \
    --hidden-import google.auth.transport.requests \
    --hidden-import google_auth_httplib2 \
    --hidden-import httplib2 \
    --hidden-import googleapiclient.discovery \
    --hidden-import googleapiclient.http \
    --hidden-import googleapiclient.errors \
    --hidden-import plyer \
    --hidden-import plyer.platforms \
    --hidden-import plyer.platforms.linux \
    --hidden-import plyer.platforms.linux.notification \
    --add-data "icon.ico:." \
    main.py
PYI_EXITCODE=$?
set -e

echo ""
# Verifica el codigo de salida real de PyInstaller, no solo si el ejecutable
# existe: un build fallido deja el archivo VIEJO en su lugar, y un chequeo
# de "existe" por si solo reportaria "LISTO" con un ejecutable desactualizado
# (mismo fix ya aplicado en build.bat).
if [ "$PYI_EXITCODE" -ne 0 ]; then
    echo "  ERROR: PyInstaller termino con codigo $PYI_EXITCODE. Revisa los mensajes anteriores."
    echo "  El archivo en output/dist/OdooBackupTool, si existe, es el build ANTERIOR — no se actualizo."
    exit 1
elif [ -f "output/dist/OdooBackupTool" ]; then
    chmod +x output/dist/OdooBackupTool
    echo "  LISTO: output/dist/OdooBackupTool generado correctamente."
    echo "  Para distribuir: copiar el archivo 'OdooBackupTool' y ejecutar con ./OdooBackupTool"
else
    echo "  ERROR: PyInstaller reporto exito pero no se encontro el ejecutable. Revisa los mensajes anteriores."
    exit 1
fi
echo ""
