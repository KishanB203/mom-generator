# PyInstaller spec: builds dist\MoMGenerator\MoMGenerator.exe (one folder, no console window).
# Run through installer\build.ps1, which then wraps the folder into MoMGenerator-Setup.exe with Inno Setup.
import comtypes.client
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# Generate the UI Automation wrapper now, so the installed app doesn't have to write it at runtime.
comtypes.client.GetModule("UIAutomationCore.dll")

ROOT = SPECPATH + "\\.."

a = Analysis(
    [ROOT + "\\run.pyw"],
    pathex=[ROOT],
    binaries=collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("pyaudiowpatch"),
    datas=collect_data_files("faster_whisper") + [(ROOT + "\\mom.ico", ".")],
    hiddenimports=(
        collect_submodules("comtypes.gen")
        + collect_submodules("pystray")
        + ["pynput.keyboard._win32", "pynput.mouse._win32", "win32timezone"]
    ),
    excludes=["torch", "tensorflow", "matplotlib", "pandas", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MoMGenerator",
    icon=ROOT + "\\mom.ico",
    console=False,
    upx=False,
    version=SPECPATH + "\\version_info.txt",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="MoMGenerator")
