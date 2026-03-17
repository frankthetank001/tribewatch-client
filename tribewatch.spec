# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TribeWatch client (no server/standalone)."""

import os, importlib
block_cipher = None

# Locate rapidocr_onnxruntime package directory for data files
_rapidocr_dir = os.path.dirname(importlib.import_module('rapidocr_onnxruntime').__file__)

a = Analysis(
    ['tribewatch/client_main.py'],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(_rapidocr_dir, 'config.yaml'), 'rapidocr_onnxruntime'),
        (os.path.join(_rapidocr_dir, 'models'), 'rapidocr_onnxruntime/models'),
    ],
    hiddenimports=[
        'tribewatch.eos',
        'tribewatch.updater',
        'tribewatch.reconnect',
        'rapidocr_onnxruntime',
        'ch_ppocr_v2_cls',
        'ch_ppocr_v3_det',
        'ch_ppocr_v3_rec',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'scipy', 'pandas',
        # EasyOCR + its heavy deps (optional engine, not shipped in installer)
        'easyocr', 'torch', 'torchvision',
        'skimage', 'sympy',
        # Full PaddlePaddle not needed (we use rapidocr-onnxruntime instead)
        'paddleocr', 'paddlepaddle',
        # Server components — not needed in client build
        'fastapi', 'uvicorn', 'starlette', 'uvloop',
        'watchfiles', 'websockets', 'httptools',
        'itsdangerous', 'python_multipart',
        # Test/dev deps
        'pytest', 'httpx', '_pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TribeWatch',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=None,  # TODO: add icon
    version_info=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TribeWatch',
)
