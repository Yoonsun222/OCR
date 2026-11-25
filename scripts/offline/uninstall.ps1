[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [ValidateSet('Purge', 'KeepAuditReceipts')][string]$RetentionPolicy = 'Purge',
    [string]$PythonPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$bootstrap = Join-Path $PSScriptRoot 'lifecycle.py'
$launcher = if ($PythonPath) { $PythonPath } else { 'py.exe' }
$version = @()
if (-not $PythonPath) { $version = @('-3.11') }
& $launcher @version -I -S -B $bootstrap uninstall $InstallRoot $DataRoot $RetentionPolicy
exit $LASTEXITCODE
