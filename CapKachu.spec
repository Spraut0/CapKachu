# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for CapKachu.
Build:  pyinstaller CapKachu.spec
Output: dist/CapKachu.exe

NOTE: Filename intentionally simple (no special chars) so it survives
OneDrive sync, SMB shares, and email attachments without renaming.
"""

a = Analysis(
    ['fast_capture.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy science stack — never used
        'numpy', 'scipy', 'matplotlib', 'pandas',
        # Test frameworks bundled with stdlib/Pillow
        'tkinter.test', 'unittest', 'test', 'pytest', 'doctest',
        # Build/packaging tooling
        'pip', 'setuptools', 'distutils', 'lib2to3',
        # pywin32 — replaced by pure ctypes calls in our code
        'pywin32', 'pythoncom', 'pywintypes', 'win32com',
        # Misc rarely-needed stdlib paths
        'pydoc', 'curses', 'sqlite3',
    ],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CapKachu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
