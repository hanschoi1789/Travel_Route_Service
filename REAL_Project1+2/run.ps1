param([int]$Port = 8520)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$travelPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$travelPidFile = Join-Path $PSScriptRoot "artifacts\server-$Port.pid"
if (Test-Path -LiteralPath $travelPidFile) {
    try {
        $travelServer = Get-Process -Id ([int](Get-Content -LiteralPath $travelPidFile -Raw)) -ErrorAction Stop
        if ($travelServer.Path -eq $travelPython) {
            $travelHealth = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/_stcore/health" -TimeoutSec 2
            if ($travelHealth.StatusCode -eq 200) {
                Start-Process -FilePath "http://127.0.0.1:$Port"
                exit 0
            }
        }
    } catch { }
}
if (-not (Test-Path -LiteralPath $travelPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or later is required.' }
}
& $travelPython -c 'import streamlit,pandas,numpy,pydeck,requests,sklearn'
if ($LASTEXITCODE -ne 0) {
    & $travelPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
& $travelPython -m streamlit run app.py --server.port $Port --server.headless false
