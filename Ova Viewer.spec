# -*- mode: python ; coding: utf-8 -*-
import importlib.util
import os

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo)

# Version from git tags (see random_image_viewer/version.py). The frozen app
# cannot ask git, so stamp it into the generated _version.py it imports.
# version.py is loaded by path: importing the package would pull in all of Qt.
_spec = importlib.util.spec_from_file_location(
    "_ova_version", os.path.join(SPECPATH, "random_image_viewer", "version.py"))
_ver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ver)
APP_VERSION = _ver.version_from_git(SPECPATH) or _ver.UNKNOWN
_ver.write_version_file(APP_VERSION)
print(f"Building Ova Viewer {APP_VERSION}")

# Shown in Explorer: exe Properties -> Details.
_nums = _ver.numeric_version(APP_VERSION)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_nums, prodvers=_nums),
    kids=[
        StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', 'designOVA'),
            StringStruct('FileDescription', 'Ova Viewer'),
            StringStruct('FileVersion', APP_VERSION),
            StringStruct('ProductName', 'Ova Viewer'),
            StringStruct('ProductVersion', APP_VERSION),
            StringStruct('OriginalFilename', 'Ova Viewer.exe'),
        ])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])]),
    ],
)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.png', '.'), ('icon.ico', '.'), ('ova_viewer.png', '.')],
    hiddenimports=['random_image_viewer._version', 'PySide6.QtWebSockets',
                   # JPEG XL: imagecodecs loads its codec modules by name
                   # (and _jpegxl needs the Cython runtime they share - left
                   # out, the exe said "could not import name 'jpegxl_decode'")
                   'imagecodecs._jpegxl', 'imagecodecs._shared',
                   'imagecodecs._shared_cython'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# The launcher's own splash: shown by the exe the moment it starts, before
# Python and Qt load - a cold start (the first after a rebuild, Defender
# scanning the DLLs) spent 6-8 s with nothing on screen until the app's Qt
# splash could appear, and a second copy got started. Closed by app.py as
# soon as the Qt splash is up. Just a thin white line - no card, no text.
# (splash_boot.png: tools/make_splash.py)
# ⏳ The line is a loader that moves the whole time Python loads (10-20 s on a
# cold start): splash_boot.tcl draws and animates it in the launcher's own
# splash thread - Python, holding its lock while it imports, could not move
# anything meanwhile. Added to the end of PyInstaller's splash script.
from PyInstaller.building import splash_templates as _splash_templates
_OVA_LOADER_TCL = open(os.path.join(SPECPATH, 'splash_boot.tcl'), encoding='utf-8').read()
if not getattr(_splash_templates.build_script, '_ova', False):
    _pyi_build_script = _splash_templates.build_script

    def _ova_build_script(text_options=None, always_on_top=False):
        return _pyi_build_script(text_options=text_options, always_on_top=always_on_top) \
            + "\n" + _OVA_LOADER_TCL
    _ova_build_script._ova = True
    _splash_templates.build_script = _ova_build_script

splash = Splash(
    'splash_boot.png',
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    minify_script=True,
    always_on_top=True,
)

# One-folder build (dist/Ova Viewer/): a one-file exe unpacks its whole
# bundle to %TEMP% on every launch (~3.5 s of the old ~4.3 s startup).
exe = EXE(
    pyz,
    a.scripts,
    splash,
    [],
    exclude_binaries=True,
    name='Ova Viewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
    version=version_info,
    # DPI-aware (per monitor) from the first moment: the launcher's splash
    # (Tk) is then laid out in real pixels too, and stays put when Qt starts.
    # Unaware, Windows scaled it, and as Qt made the process aware the line
    # jumped and shrank to 2/3 at 150 % - so it had to be hidden right then,
    # and the moving loader could not last through Qt's slow start.
    manifest=(
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
        '<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>'
        '<dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>'
        '<dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">'
        'PerMonitorV2</dpiAwareness>'
        '</windowsSettings></application></assembly>'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    splash.binaries,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Ova Viewer',
)
