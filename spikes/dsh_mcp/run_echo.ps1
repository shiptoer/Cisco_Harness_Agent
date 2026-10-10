# Steps 4-5 of the spike: one headless run that should call mcp__hello__echo, then the tool-set check.
# Prerequisite: $env:DEEPSEEK_API_KEY exported in this terminal (never written to a file).
#   powershell -ExecutionPolicy Bypass -File .\run_echo.ps1
#
# Child output never passes through the PowerShell pipeline: Windows PowerShell 5.1 decodes
# native stdout with the OEM code page and `>`/Tee-Object re-encode it as UTF-16 LE.
# Start-Process -Redirect* hands the child a file handle, so the files hold its exact bytes
# (dsh writes UTF-8 without BOM). Header lines are appended as UTF-8 without BOM.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"
if (-not $env:DEEPSEEK_API_KEY) { throw 'Set $env:DEEPSEEK_API_KEY in this terminal first.' }
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = '1'

$Utf8 = New-Object System.Text.UTF8Encoding $false
$Node = "$PSScriptRoot\.tools\node\node.exe"
$DshBin = "$PSScriptRoot\node_modules\@deepseek-ai\dsh\lib\bin.js"
$Py = "$PSScriptRoot\.venv\Scripts\python.exe"

# Runs $Exe with a verbatim argument string; stdout/stderr go byte-for-byte to the given files.
function Invoke-Raw([string]$Exe, [string]$ArgString, [string]$OutFile, [string]$ErrFile) {
    $p = Start-Process -FilePath $Exe -ArgumentList $ArgString -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
    return $p.ExitCode
}

function Add-Utf8([string]$Path, [string]$Text) {
    [IO.File]::AppendAllText("$PSScriptRoot\$Path", $Text, $Utf8)
}

function Add-FileBytes([string]$Path, [string]$From) {
    $bytes = [IO.File]::ReadAllBytes($From)
    $fs = [IO.File]::Open("$PSScriptRoot\$Path", 'Append')
    try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
}

# ── Step 4: headless run ──────────────────────────────────────────────────────
$Ndjson = "$PSScriptRoot\logs\02_echo.ndjson"
$Prompt = 'gọi tool echo với chữ ping'
$code = Invoke-Raw $Node "`"$DshBin`" --profile headless --patch dsh.patch.yml --json `"$Prompt`"" `
    $Ndjson "$PSScriptRoot\logs\02_echo.stderr.txt"
Add-Utf8 'logs\02_echo.stderr.txt' "dsh exit=$code`r`n"
Write-Host "dsh exit=$code"

$head = [IO.File]::ReadAllBytes($Ndjson)
if ($head.Length -eq 0 -or $head[0] -ne 0x7B) {
    $shown = if ($head.Length) { ($head[0..([Math]::Min(3, $head.Length - 1))] | ForEach-Object { '{0:X2}' -f $_ }) -join ' ' } else { '(empty)' }
    Write-Host "ERROR: logs\02_echo.ndjson must start with '{' (0x7B); first bytes: $shown" -ForegroundColor Red
    exit 1
}

# ── Step 5: tool-set check ────────────────────────────────────────────────────
$Report = 'logs\03_check_tools.txt'
[IO.File]::WriteAllText("$PSScriptRoot\$Report", '', $Utf8)
$tmpOut = [IO.Path]::GetTempFileName(); $tmpErr = [IO.Path]::GetTempFileName()
$failed = 0
foreach ($check in @(
        'config logs/00_config_default.txt',
        'config logs/01b_config_patched_v2.txt',
        'ndjson logs/02_echo.ndjson')) {
    Add-Utf8 $Report "## check_tools.py $check`r`n"
    $rc = Invoke-Raw $Py "check_tools.py $check" $tmpOut $tmpErr
    Add-FileBytes $Report $tmpOut
    Add-FileBytes $Report $tmpErr
    Add-Utf8 $Report "exit=$rc`r`n`r`n"
    if ($check -like 'ndjson*' -and $rc -ne 0) { $failed = 1 }
}
Remove-Item $tmpOut, $tmpErr
Write-Host ([IO.File]::ReadAllText("$PSScriptRoot\$Report", $Utf8))
# 00_config_default is expected to FAIL (control); the run fails only if the NDJSON check fails.
exit $failed
