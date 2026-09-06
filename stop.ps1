$ErrorActionPreference = 'Stop'
$propertyScript = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'server.py'))
$propertyProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'"
foreach ($propertyProcess in $propertyProcesses) {
    if ($propertyProcess.CommandLine -and $propertyProcess.CommandLine.Contains($propertyScript) -and -not $propertyProcess.CommandLine.Contains('--port')) {
        Stop-Process -Id $propertyProcess.ProcessId -ErrorAction Stop
    }
}
