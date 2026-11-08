$ErrorActionPreference = 'Stop'
try {
    $scriptPath = Join-Path $PSScriptRoot 'RegistryCleaner.pyw'
    $pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $scriptPath + '"') -Verb RunAs -ErrorAction Stop
}
catch {
    Write-Host 'Failed to start as administrator:'
    Write-Host $_.Exception.Message
    exit 1
}
