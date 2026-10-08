param([Parameter(Mandatory = $true)][string]$GamePath, [switch]$Watch, [switch]$DryRun,
    [ValidateRange(5, 3600)][int]$Interval = 60, [switch]$Background, [switch]$Stop, [switch]$Status)
$ErrorActionPreference = 'Stop'
$dingExecutable = Join-Path $PSScriptRoot 'DingTracker-LogHelper.exe'
if (Test-Path -LiteralPath $dingExecutable -PathType Leaf) {
    $dingArguments = @('--wow', $GamePath, '--interval', $Interval)
    if ($Watch) { $dingArguments += '--watch' }
    if ($DryRun) { $dingArguments += '--dry-run' }
    if ($Background) { $dingArguments += '--background' }
    if ($Stop) { $dingArguments += '--stop' }
    if ($Status) { $dingArguments += '--status' }
    & $dingExecutable @dingArguments
    exit $LASTEXITCODE
}
$dingPython = Get-Command python -ErrorAction SilentlyContinue
if (-not $dingPython) { throw 'Use the Windows executable download. Running this source checkout requires Python 3.10 or newer.' }
$dingArguments = @((Join-Path $PSScriptRoot 'log_import.py'), '--wow', $GamePath, '--interval', $Interval)
if ($Watch) { $dingArguments += '--watch' }
if ($DryRun) { $dingArguments += '--dry-run' }
if ($Background) { $dingArguments += '--background' }
if ($Stop) { $dingArguments += '--stop' }
if ($Status) { $dingArguments += '--status' }
& $dingPython.Source @dingArguments
exit $LASTEXITCODE
