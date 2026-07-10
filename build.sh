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

# En Linux el parametro --add-data usa ':' como separador (no ';' como en Windows)
echo ""
echo "  Compilando ejecutable..."
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
    --add-data "icon.ico:." \
    main.py

echo ""
if [ -f "output/dist/OdooBackupTool" ]; then
    chmod +x output/dist/OdooBackupTool
    echo "  LISTO: output/dist/OdooBackupTool generado correctamente."
    echo "  Para distribuir: copiar el archivo 'OdooBackupTool' y ejecutar con ./OdooBackupTool"
else
    echo "  ERROR: no se genero el ejecutable. Revisa los mensajes anteriores."
    exit 1
fi
echo ""
