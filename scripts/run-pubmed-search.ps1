[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardArgs
)

$ErrorActionPreference = "Stop"

try {
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
}

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
}

$launcher = $null
foreach ($candidate in @("python", "py", "python3")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $launcher = $candidate
        break
    }
}

if (-not $launcher) {
    Write-Error "Could not find python, py, or python3 on PATH. Install Python and ensure one launcher is available."
    exit 1
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $scriptDir "pubmed_search.py"

& $launcher -B $scriptPath @ForwardArgs
exit $LASTEXITCODE
