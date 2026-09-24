# -*- mode: python ; coding: utf-8 -*-
"""
hardsub.spec — 把 hardsub.py 打包成 Windows 单文件命令行 exe。

构建：
    python -m PyInstaller --noconfirm --workpath _build\\work --distpath _build\\dist hardsub.spec

产物：_build\\dist\\hardsub.exe  （约 8~10 MB，纯标准库，不需要外部依赖）
ffmpeg / ffprobe 不打包：放到 exe 同目录或加入 PATH 即可（脚本会自动找）。
"""

# 本项目只用标准库，这些都不会被用到，排除掉能显著减小体积
EXCLUDES = [
    'tkinter', '_tkinter', 'unittest', 'pydoc', 'doctest', 'pdb', 'lib2to3',
    'distutils', 'setuptools', 'pip', 'wheel', 'pkg_resources',
    'sqlite3', 'curses', 'multiprocessing', 'concurrent', 'asyncio',
    'numpy', 'PIL', 'pandas', 'matplotlib', 'IPython', 'pytest', 'cv2',
]

a = Analysis(
    ['hardsub.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='hardsub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,                 # 命令行程序，必须有控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
