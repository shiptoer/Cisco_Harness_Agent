# Isolated environment for the dsh + MCP spike. Dot-source before any command:
#   . .\env.ps1
# DEEPSEEK_API_KEY is NOT set here: export it in your own terminal first.
$SpikeDir = $PSScriptRoot
$env:PATH = "$SpikeDir\.tools\node;$SpikeDir\node_modules\.bin;$env:PATH"
$env:DSH_HOME = "$SpikeDir\.dsh_home"
$env:DSH_TELEMETRY_DISABLED = '1'
$env:npm_config_cache = "$SpikeDir\.tools\npm-cache"
if (-not $env:DEEPSEEK_API_KEY) { Write-Warning 'DEEPSEEK_API_KEY is not set in this terminal.' }
