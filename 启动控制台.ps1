$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$port = 5000

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found: $python"
}

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    Start-Process -FilePath $python -ArgumentList 'web.py --host 127.0.0.1 --port 5000' -WorkingDirectory $projectRoot -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    } until ($listener -or (Get-Date) -gt $deadline)
}

if (-not $listener) {
    throw 'Console startup timed out. Check project logs.'
}

Start-Process 'http://127.0.0.1:5000'
