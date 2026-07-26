from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


ROOT = Path(SPECPATH).parent
ICON = ROOT / "assets" / "icon" / "CLI-Downloader.ico"

main_analysis = Analysis(
    [str(ROOT / "cli_downloader" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=collect_data_files("cli_downloader") + copy_metadata("cli-downloader"),
    # External plugins are imported only at runtime, so PyInstaller cannot
    # discover standard-library modules used exclusively by plugin code.
    hiddenimports=collect_submodules("textual") + ["html.parser"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pip", "pytest", "ruff", "unittest"],
    noarchive=False,
    optimize=1,
)
main_pyz = PYZ(main_analysis.pure)
main_executable = EXE(
    main_pyz,
    main_analysis.scripts,
    [],
    exclude_binaries=True,
    name="CLI-Downloader",
    icon=str(ICON),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

installer_analysis = Analysis(
    [str(ROOT / "cli_downloader" / "plugins" / "dependency_installer.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=collect_data_files("pip"),
    hiddenimports=collect_submodules("pip"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["textual", "pytest", "ruff", "unittest"],
    noarchive=False,
    optimize=1,
)
installer_pyz = PYZ(installer_analysis.pure)
installer_executable = EXE(
    installer_pyz,
    installer_analysis.scripts,
    [],
    exclude_binaries=True,
    name="CLI-Downloader.PluginInstaller",
    icon=str(ICON),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

bundle = COLLECT(
    main_executable,
    installer_executable,
    main_analysis.binaries,
    main_analysis.datas,
    installer_analysis.binaries,
    installer_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CLI-Downloader",
)
