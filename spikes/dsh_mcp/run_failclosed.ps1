# Fail-closed test: the cisco MCP server cannot start (dsh.patch.broken.yml points `command` to a
# python.exe that does not exist). Expected: dsh exits non-zero and emits no successful final.
# Prerequisite: $env:DEEPSEEK_API_KEY exported in this terminal (never written to a file).
#   powershell -ExecutionPolicy Bypass -File .\run_failclosed.ps1
#
# Same raw-byte logging as run_echo.ps1: Start-Process -Redirect* hands the child a file handle.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"
if (-not $env:DEEPSEEK_API_KEY) { throw 'Set $env:DEEPSEEK_API_KEY in this terminal first.' }
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = '1'

$Utf8 = New-Object System.Text.UTF8Encoding $false
$Node = "$PSScriptRoot\.tools\node\node.exe"
$DshBin = "$PSScriptRoot\node_modules\@deepseek-ai\dsh\lib\bin.js"
$Py = "$PSScriptRoot\.venv\Scripts\python.exe"

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

$Ndjson = "$PSScriptRoot\logs\20_failclosed.ndjson"
$Stderr = 'logs\20_failclosed.stderr.txt'
$Prompt = 'Cho tôi thông tin thiết bị SW-ACC-01'
$code = Invoke-Raw $Node "`"$DshBin`" --profile headless --patch dsh.patch.broken.yml --json `"$Prompt`"" `
    $Ndjson "$PSScriptRoot\$Stderr"
Add-Utf8 $Stderr "dsh exit=$code`r`n"
Write-Host "dsh exit=$code"

$head = [IO.File]::ReadAllBytes($Ndjson)
if ($head.Length -eq 0 -or $head[0] -ne 0x7B) {
    $shown = if ($head.Length) { ($head[0..([Math]::Min(3, $head.Length - 1))] | ForEach-Object { '{0:X2}' -f $_ }) -join ' ' } else { '(empty)' }
    Write-Host "ERROR: logs\20_failclosed.ndjson must start with '{' (0x7B); first bytes: $shown" -ForegroundColor Red
    exit 1
}

$Report = 'logs\20_failclosed.check.txt'
$tmpOut = [IO.Path]::GetTempFileName(); $tmpErr = [IO.Path]::GetTempFileName()
$rc = Invoke-Raw $Py "check_failclosed.py logs\20_failclosed.ndjson $Stderr $code" $tmpOut $tmpErr
[IO.File]::WriteAllText("$PSScriptRoot\$Report", '', $Utf8)
Add-FileBytes $Report $tmpOut
Add-FileBytes $Report $tmpErr
Add-Utf8 $Report "exit=$rc`r`n"
Remove-Item $tmpOut, $tmpErr
Write-Host ([IO.File]::ReadAllText("$PSScriptRoot\$Report", $Utf8))
exit $rc
