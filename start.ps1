$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$propertyUrl = 'http://127.0.0.1:8877'
try {
    $propertyResponse = Invoke-RestMethod -Uri "$propertyUrl/api/state" -TimeoutSec 2
    if ($propertyResponse.settings -and $propertyResponse.houses -is [array]) {
        Start-Process $propertyUrl
        exit
    }
} catch { }
$propertyPython = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $propertyPython) {
    throw 'Python 3 is required. Install Python and try again.'
}
$propertyData = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $propertyData -Force | Out-Null
Start-Process -FilePath $propertyPython -ArgumentList @('-B', ('"' + (Join-Path $PSScriptRoot 'server.py') + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $propertyData 'server.log') -RedirectStandardError (Join-Path $propertyData 'server-error.log')
for ($propertyAttempt = 0; $propertyAttempt -lt 30; $propertyAttempt++) {
    try {
        Invoke-RestMethod -Uri "$propertyUrl/api/state" -TimeoutSec 1 | Out-Null
        exit
    } catch { Start-Sleep -Milliseconds 300 }
}
throw "Service did not start. See data/server-error.log."
