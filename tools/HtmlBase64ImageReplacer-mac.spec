# -*- mode: python ; coding: utf-8 -*-
# macOS 专用打包配置：生成 .app 应用包
# 注意：windnd 仅 Windows 可用，macOS 上不打包（代码已做 ImportError 兜底）。

a = Analysis(
    ['html_base64_image_replacer.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['PIL._tkinter_finder', 'PIL.Image', 'PIL.ImageTk'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['windnd'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HTML图片替换工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,         # 跟随打包机架构：Intel 机出 Intel 包；Apple 机出 arm64 包
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='HTML图片替换工具',
)

app = BUNDLE(
    coll,
    name='HTML图片替换工具.app',
    icon=None,                 # 如有 icon.icns 可填路径
    bundle_identifier='com.dragonplus.htmlimagereplacer',
    info_plist={
        'CFBundleName': 'HTML图片替换工具',
        'CFBundleDisplayName': 'HTML 图片替换工具',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.13.0',
    },
)
