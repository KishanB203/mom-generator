# Builds dist\MoMGenerator-Setup.exe: the one file users download and double-click.
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
# Needs: Python with requirements.txt + pyinstaller installed, and Inno Setup 6 (winget install JRSoftware.InnoSetup).
param([string]$Python = "", [string]$Iscc = "")

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

if (-not $Python) {
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    $Python = if (Test-Path $venv) { $venv } else { "python" }
}
if (-not $Iscc) {
    $cands = @((Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
               "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
               "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe")
    $Iscc = $cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $Iscc) { throw "Inno Setup 6 not found. Install it with: winget install JRSoftware.InnoSetup" }
}

$version = & $Python -c "import sys; sys.path.insert(0, r'$root'); import mom_app; print(mom_app.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Could not read the app version." }
Write-Host "== Building MoM Generator $version" -ForegroundColor Cyan

# Windows file properties of MoMGenerator.exe
$v = ($version.Split(".") + @("0", "0", "0"))[0..3] -join ", "
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($v), prodvers=($v)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'MoM Generator'),
    StringStruct('FileDescription', 'MoM Generator'),
    StringStruct('FileVersion', '$version'),
    StringStruct('ProductName', 'MoM Generator'),
    StringStruct('ProductVersion', '$version'),
    StringStruct('OriginalFilename', 'MoMGenerator.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content -Encoding utf8 (Join-Path $here "version_info.txt")

Write-Host "== 1/2 Packaging the app (PyInstaller)" -ForegroundColor Cyan
& $Python -m PyInstaller --noconfirm --clean --log-level WARN `
    --distpath (Join-Path $root "build\dist") --workpath (Join-Path $root "build\work") (Join-Path $here "MoMGenerator.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Write-Host "== 2/2 Creating the installer (Inno Setup)" -ForegroundColor Cyan
& $Iscc /Q "/DAppVersion=$version" (Join-Path $here "MoMGenerator.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }

$setup = Get-Item (Join-Path $root "dist\MoMGenerator-Setup.exe")
Write-Host ("`nDone: {0} ({1:N0} MB)" -f $setup.FullName, ($setup.Length / 1MB)) -ForegroundColor Green
