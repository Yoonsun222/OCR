[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundlePath,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [ValidateSet('local16', 'local32')][string]$Profile = 'local16',
    [switch]$Resume,
    [string]$ApprovalId = '',
    [string]$CanaryEndpoint = '',
    [string]$PythonPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$bootstrap = Join-Path $PSScriptRoot 'lifecycle.py'
$launcher = if ($PythonPath) { $PythonPath } else { 'py.exe' }
$version = @()
if (-not $PythonPath) { $version = @('-3.11') }
$options = @()
if ($Resume.IsPresent) { $options += '--resume' }
if ($ApprovalId -or $CanaryEndpoint) {
    if (-not ($ApprovalId -and $CanaryEndpoint)) { exit 2 }
    $options += @('--approval-id', $ApprovalId, '--canary-endpoint', $CanaryEndpoint)
}
& $launcher @version -I -S -B $bootstrap acceptance $BundlePath $OutputRoot $Profile @options
exit $LASTEXITCODE
