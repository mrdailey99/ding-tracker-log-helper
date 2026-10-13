param([string]$OutputDirectory, [string]$BuildDirectory, [string]$PythonExecutable = 'python')
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Build the Windows executable on Windows.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot 'dist' }
if (-not $BuildDirectory) { $BuildDirectory = Join-Path $PSScriptRoot '.build' }
$dingVersion = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'VERSION') -Raw).Trim()
if ($dingVersion -notmatch '^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$') { throw 'Invalid helper version.' }
New-Item -ItemType Directory -Path $OutputDirectory, $BuildDirectory -Force | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$BuildDirectory = (Resolve-Path -LiteralPath $BuildDirectory).Path
$dingVenv = Join-Path $BuildDirectory 'venv'
$dingPython = Join-Path $dingVenv 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $dingPython)) {
    & $PythonExecutable -m venv $dingVenv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the build environment.' }
}
& $dingPython -c 'import struct; assert struct.calcsize(''P'') == 8, ''Use 64-bit Python to build Windows x64'''
if ($LASTEXITCODE -ne 0) { throw '64-bit Python is required.' }
& $dingPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned build tools.' }

$dingStage = Join-Path $BuildDirectory ('stage-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $dingStage -Force | Out-Null
$dingParts = @($dingVersion.Split('-')[0].Split('.') | ForEach-Object { [int]$_ }) + @(0)
$dingNumbers = $dingParts -join ', '
$dingVersionInfo = @'
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({NUMBERS}), prodvers=({NUMBERS}), mask=0x3f,
                   flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'Ding Tracker Log Helper'),
    StringStruct('FileVersion', '{VERSION}'),
    StringStruct('ProductName', 'Ding Tracker Log Helper'),
    StringStruct('ProductVersion', '{VERSION}'),
    StringStruct('OriginalFilename', 'DingTracker-LogHelper.exe')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])
'@
$dingVersionFile = Join-Path $dingStage 'version-info.txt'
$dingVersionInfo.Replace('{NUMBERS}', $dingNumbers).Replace('{VERSION}', $dingVersion) | Set-Content -LiteralPath $dingVersionFile -Encoding UTF8
& $dingPython -m PyInstaller --noconfirm --onefile --console --noupx --name DingTracker-LogHelper `
    --distpath $dingStage --workpath (Join-Path $BuildDirectory 'work') --specpath $BuildDirectory `
    --add-data ((Join-Path $PSScriptRoot 'VERSION') + ';.') --version-file $dingVersionFile `
    --hidden-import journal_transfer --hidden-import tkinter --hidden-import tkinter.ttk --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox `
    (Join-Path $PSScriptRoot 'log_import.py')
if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }

$dingBundle = Join-Path $dingStage 'bundle'
New-Item -ItemType Directory -Path $dingBundle -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $dingStage 'DingTracker-LogHelper.exe') -Destination $dingBundle
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $dingBundle
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'CHANGELOG.md') -Destination $dingBundle
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Start-JournalTransfer.cmd') -Destination $dingBundle
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination (Join-Path $dingBundle 'LICENSE-DingTracker-LogHelper.txt')
# Include the licenses for the embedded Python runtime and PyInstaller bootloader.
$dingLicenseScript = @'
import importlib.metadata as metadata
import json, platform, shutil, sys
from pathlib import Path
bundle, version = Path(sys.argv[1]), sys.argv[2]
shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', bundle / 'LICENSE-Python.txt')
distribution = metadata.distribution('pyinstaller')
license_path = next(p for p in distribution.files if p.name == 'COPYING.txt')
shutil.copyfile(distribution.locate_file(license_path), bundle / 'LICENSE-PyInstaller.txt')
(bundle / 'BUILD-INFO.json').write_text(json.dumps({
    'helperVersion': version, 'pythonVersion': platform.python_version(),
    'pyinstallerVersion': metadata.version('pyinstaller'), 'platform': 'windows-x64',
    'signed': False
}, indent=2) + '\n', encoding='utf-8')
'@
& $dingPython -c $dingLicenseScript $dingBundle $dingVersion
if ($LASTEXITCODE -ne 0) { throw 'Could not collect runtime licenses and build information.' }
$dingExecutable = Join-Path $dingBundle 'DingTracker-LogHelper.exe'
function Get-DingSHA256([string]$FilePath) {
    $dingHash = [System.Security.Cryptography.SHA256]::Create()
    $dingStream = [System.IO.File]::OpenRead($FilePath)
    try { return [System.BitConverter]::ToString($dingHash.ComputeHash($dingStream)).Replace('-', '').ToLowerInvariant() }
    finally { $dingStream.Dispose(); $dingHash.Dispose() }
}
$dingExeHash = Get-DingSHA256 $dingExecutable
($dingExeHash + '  DingTracker-LogHelper.exe') | Set-Content -LiteralPath (Join-Path $dingBundle 'SHA256SUMS.txt') -Encoding ASCII
$dingZipName = 'DingTracker-LogHelper-' + $dingVersion + '-windows-x64.zip'
$dingArchive = Join-Path $OutputDirectory $dingZipName
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path -LiteralPath $dingArchive) { Remove-Item -LiteralPath $dingArchive }
[System.IO.Compression.ZipFile]::CreateFromDirectory($dingBundle, $dingArchive)
$dingZipHash = Get-DingSHA256 $dingArchive
($dingZipHash + '  ' + $dingZipName) | Set-Content -LiteralPath ($dingArchive + '.sha256') -Encoding ASCII
Write-Output $dingArchive
