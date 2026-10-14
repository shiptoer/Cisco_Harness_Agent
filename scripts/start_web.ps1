[CmdletBinding(DefaultParameterSetName = 'Fixed')]
param(
    [Parameter(Mandatory = $true)][string]$Context,
    [Parameter(Mandatory = $true, ParameterSetName = 'Fixed')][string]$Contract,
    [Parameter(Mandatory = $true, ParameterSetName = 'Multi')][switch]$MultiRequest,
    [ValidateSet('viewer','access_operator','network_admin','security_admin')]
    [string]$Role = 'access_operator',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Missing .venv. Install the project with .venv\Scripts\python.exe -m pip install -e ".[dev]".'
}
$nodeExe = (Get-Command node.exe -ErrorAction Stop).Source
$names = @('DSH_HOME', 'DSH_TELEMETRY_DISABLED', 'DSH_TELEMETRY_MODE',
           'PYTHONUTF8', 'PYTHONIOENCODING', 'DEEPSEEK_API_KEY')
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
Push-Location $repoRoot
try {
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $prepareArgs = @('--context', $Context, '--role', $Role, '--node', $nodeExe)
    if ($MultiRequest) { $prepareArgs += '--multi' }
    else { $prepareArgs += @('--contract', $Contract) }
    $output = & $pythonExe (Join-Path $PSScriptRoot 'prepare_web.py') @prepareArgs
    if ($LASTEXITCODE -ne 0) { throw 'Offline setup check failed. Web was not started.' }
    $setup = ($output -join "`n") | ConvertFrom-Json
    Write-Host 'Context, selected mode, model config and CBA/TSA preflight: OK'
    Write-Host "Role: $Role | Level: B4"
    Write-Host "Model: $($setup.model)"
    if ($MultiRequest) { Write-Host 'Multi-request mode: each proposal needs browser approval.' }
    else { Write-Host "Exact intent: $($setup.intent_file)" }
    if ($CheckOnly) {
        Write-Host 'Offline check complete. No web server or model request started.'
        return
    }
    $busy = @(Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue)
    if ($busy.Count -gt 0) {
        throw 'Port 3080 is occupied. Stop the existing web server with Ctrl+C in its terminal.'
    }
    if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
        $secureKey = Read-Host 'DeepSeek API key (hidden)' -AsSecureString
        $keyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        try { $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPtr) }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPtr)
            Remove-Variable secureKey, keyPtr
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) { throw 'API key is empty.' }
    $homePrefix = if ($MultiRequest) { 'cha\web-multi-' } else { 'cha\web-shared-' }
    $env:DSH_HOME = Join-Path $env:LOCALAPPDATA ($homePrefix + $setup.home_id)
    $env:DSH_TELEMETRY_DISABLED = '1'
    $env:DSH_TELEMETRY_MODE = 'DISABLED'
    if ($MultiRequest) {
        Write-Host 'The bridge opens a separate private operator tab. Refresh it after each proposal.'
        Write-Host 'Keep approval links and cookies out of chat. If the tab does not open, restart from this desktop.'
    }
    Write-Host 'Opening web. Sending a request may incur model API charges. No device deployment.'
    & $nodeExe $setup.entry --profile web --patch $setup.patch
    if ($LASTEXITCODE -ne 0) { throw "dsh exited with code $LASTEXITCODE" }
}
finally {
    foreach ($name in $names) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
    }
    Pop-Location
}
