[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundlePath,
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [ValidateSet('Install', 'Repair')][string]$Mode = 'Install',
    [ValidateSet('local16', 'local32')][string]$Profile = 'local16',
    [string]$DataRoot = ($InstallRoot + '-data'),
    [string]$PythonPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$bootstrap = Join-Path $PSScriptRoot 'lifecycle.py'
$launcher = if ($PythonPath) { $PythonPath } else { 'py.exe' }
$version = @()
if (-not $PythonPath) { $version = @('-3.11') }
& $launcher @version -I -S -B $bootstrap install $BundlePath $InstallRoot $DataRoot $Mode $Profile
exit $LASTEXITCODE
