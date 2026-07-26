[CmdletBinding()]
param(
    [int]$Port = 5000,
    [string]$BindAddress = "127.0.0.1",
    [string]$RoxyPath = "H:\Program Files\roxy\RoxyBrowser\RoxyBrowser.exe",
    [int]$RoxyPort = 0,
    [string]$ExtractLinkHost = "154.44.13.150",
    [int]$ExtractLinkPort = 8085,
    [int]$WaitSeconds = 60,
    [switch]$NoBrowser,
    [switch]$SkipExtractLinkTunnel,
    [switch]$SkipDependencyCheck
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectRoot ".runtime"
$VenvDir = Join-Path $ProjectRoot ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$DependencyStamp = Join-Path $RuntimeDir "requirements.sha256"
$NodeDir = Join-Path $RuntimeDir "node"

Set-Location -LiteralPath $ProjectRoot
New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

function Test-Python {
    param([string]$Executable)

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return $false
    }
    & $Executable -c "import sys; assert sys.version_info >= (3, 10)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Resolve-BasePython {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike "*WindowsApps*") {
        if (Test-Python -Executable $command.Source) {
            return $command.Source
        }
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @("-3.13", "-3.12", "-3.11", "-3.10")) {
            & $launcher.Source $version -c "import sys; assert sys.version_info >= (3, 10)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return "$($launcher.Source)|$version"
            }
        }
    }

    throw "Python 3.10 or newer was not found."
}

function Initialize-Venv {
    if (Test-Python -Executable $Python) {
        return
    }

    if (Test-Path -LiteralPath $VenvDir) {
        $resolvedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
        $resolvedVenv = [IO.Path]::GetFullPath($VenvDir).TrimEnd('\') + '\'
        if (-not $resolvedVenv.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to move a virtual environment outside the project: $resolvedVenv"
        }
        $backup = Join-Path $ProjectRoot (".venv.broken-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
        Write-Host "Existing virtual environment is invalid; preserving it as $backup" -ForegroundColor Yellow
        Move-Item -LiteralPath $VenvDir -Destination $backup
    }

    $basePython = Resolve-BasePython
    Write-Host "Creating Python virtual environment..." -ForegroundColor Cyan
    if ($basePython.Contains("|")) {
        $parts = $basePython.Split("|", 2)
        & $parts[0] $parts[1] -m venv $VenvDir
    }
    else {
        & $basePython -m venv $VenvDir
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Python -Executable $Python)) {
        throw "Failed to create the Python virtual environment."
    }
}

function Install-Dependencies {
    if ($SkipDependencyCheck) {
        return
    }

    $requirementsHash = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $DependencyStamp) {
        (Get-Content -LiteralPath $DependencyStamp -Raw).Trim()
    }
    else {
        ""
    }
    if ($requirementsHash -eq $installedHash) {
        & $Python -c "import flask, requests, curl_cffi, pyotp, Crypto, selenium, cryptography, cloakbrowser, playwright, dotenv" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }

    Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
    & $Python -m pip install --disable-pip-version-check -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }
    Set-Content -LiteralPath $DependencyStamp -Value $requirementsHash -Encoding ASCII
}

function Resolve-RoxyPort {
    param([int]$RequestedPort)

    if ($RequestedPort -gt 0) {
        return $RequestedPort
    }

    $envFile = Join-Path $ProjectRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        $line = Get-Content -LiteralPath $envFile -Encoding UTF8 |
            Where-Object { $_ -match '^\s*ROXY_API_BASE\s*=' } |
            Select-Object -Last 1
        if ($line) {
            $raw = (($line -split '=', 2)[1]).Trim().Trim('"').Trim("'")
            try {
                $uri = [Uri]$raw
                if ($uri.Port -gt 0) {
                    return $uri.Port
                }
            }
            catch {
                Write-Warning "Ignoring invalid ROXY_API_BASE in .env: $raw"
            }
        }
    }

    return 50001
}

function Test-TcpListener {
    param([int]$TestPort)

    return [bool](
        Get-NetTCPConnection -State Listen -LocalPort $TestPort -ErrorAction SilentlyContinue |
            Select-Object -First 1
    )
}

function Test-WebUi {
    param([int]$TestPort)

    try {
        $requestArgs = @{
            Uri = "http://127.0.0.1:$TestPort/login"
            UseBasicParsing = $true
            TimeoutSec = 2
        }
        $response = Invoke-WebRequest @requestArgs
        return [bool]($response.Content -match 'GPT\s*Registrator|name="auth_code"')
    }
    catch {
        return $false
    }
}

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSeconds,
        [string]$Label
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (& $Condition) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    Write-Warning "$Label did not become ready within ${TimeoutSeconds}s."
    return $false
}

Write-Host "[1/5] Preparing runtime..." -ForegroundColor Cyan
Initialize-Venv
Install-Dependencies
if (Test-Path -LiteralPath (Join-Path $NodeDir "node.exe")) {
    $env:PATH = "$NodeDir;$env:PATH"
}

$RoxyPort = Resolve-RoxyPort -RequestedPort $RoxyPort
Write-Host "[2/5] Checking RoxyBrowser..." -ForegroundColor Cyan
if (Test-TcpListener -TestPort $RoxyPort) {
    Write-Host "Roxy API is already listening on port $RoxyPort." -ForegroundColor Green
}
else {
    if (-not (Test-Path -LiteralPath $RoxyPath -PathType Leaf)) {
        throw "RoxyBrowser executable not found: $RoxyPath"
    }

    $roxyProcess = Get-Process -Name "RoxyBrowser" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $roxyProcess) {
        Write-Host "Starting RoxyBrowser..."
        Start-Process -FilePath $RoxyPath -WindowStyle Hidden | Out-Null
    }
    else {
        Write-Host "RoxyBrowser is running; waiting for its API..."
    }

    $roxyWaitArgs = @{
        Condition = { Test-TcpListener -TestPort $RoxyPort }
        TimeoutSeconds = $WaitSeconds
        Label = "Roxy API"
    }
    if (-not (Wait-Until @roxyWaitArgs)) {
        throw "Roxy API did not start on port $RoxyPort. Check Roxy API settings."
    }
    Write-Host "Roxy API is ready on port $RoxyPort." -ForegroundColor Green
}

Write-Host "[3/5] Checking extract-link tunnel..." -ForegroundColor Cyan
if ($SkipExtractLinkTunnel) {
    Write-Host "Extract-link tunnel skipped." -ForegroundColor Yellow
}
else {
    $tunnelScript = Join-Path $ProjectRoot "start-extract-link-tunnel.ps1"
    & $tunnelScript `
        -RemoteHost $ExtractLinkHost `
        -RemoteServicePort $ExtractLinkPort `
        -LocalPort $ExtractLinkPort `
        -WaitSeconds ([Math]::Min($WaitSeconds, 30))
}

Write-Host "[4/5] Checking WebUI..." -ForegroundColor Cyan
$SelectedPort = $Port
$urlStateFile = Join-Path $RuntimeDir "webui-url.txt"
if (Test-Path -LiteralPath $urlStateFile) {
    try {
        $savedUrl = [Uri](Get-Content -LiteralPath $urlStateFile -Raw)
        if ($savedUrl.Port -gt 0 -and (Test-WebUi -TestPort $savedUrl.Port)) {
            $SelectedPort = $savedUrl.Port
        }
    }
    catch {
        # Ignore a stale state file.
    }
}

if (-not (Test-WebUi -TestPort $SelectedPort)) {
    if (Test-TcpListener -TestPort $SelectedPort) {
        Write-Warning "Port $SelectedPort is occupied. Selecting another port."
        $SelectedPort = 0
        foreach ($candidate in (($Port + 1)..($Port + 20))) {
            if (-not (Test-TcpListener -TestPort $candidate)) {
                $SelectedPort = $candidate
                break
            }
        }
        if ($SelectedPort -eq 0) {
            throw "No free WebUI port found in range $($Port + 1)-$($Port + 20)."
        }
    }

    $stdoutLog = Join-Path $RuntimeDir "webui-$SelectedPort.stdout.log"
    $stderrLog = Join-Path $RuntimeDir "webui-$SelectedPort.stderr.log"
    Write-Host "Starting WebUI on port $SelectedPort..."
    $processArgs = @{
        FilePath = $Python
        ArgumentList = @("web.py", "--host", $BindAddress, "--port", "$SelectedPort")
        WorkingDirectory = $ProjectRoot
        WindowStyle = "Hidden"
        RedirectStandardOutput = $stdoutLog
        RedirectStandardError = $stderrLog
    }
    Start-Process @processArgs | Out-Null

    $webWaitArgs = @{
        Condition = { Test-WebUi -TestPort $SelectedPort }
        TimeoutSeconds = $WaitSeconds
        Label = "WebUI"
    }
    if (-not (Wait-Until @webWaitArgs)) {
        if (Test-Path -LiteralPath $stderrLog) {
            Write-Host "---- WebUI error log ----" -ForegroundColor Yellow
            Get-Content -LiteralPath $stderrLog -Tail 60
        }
        throw "WebUI failed to start on port $SelectedPort."
    }
}
else {
    Write-Host "WebUI is already running on port $SelectedPort." -ForegroundColor Green
}

$WebUrl = "http://127.0.0.1:$SelectedPort/"
Set-Content -LiteralPath $urlStateFile -Value $WebUrl -Encoding ASCII

Write-Host "[5/5] Ready." -ForegroundColor Cyan
Write-Host "WebUI:   $WebUrl" -ForegroundColor Green
Write-Host "Roxy API: http://127.0.0.1:$RoxyPort" -ForegroundColor Green
if (-not $SkipExtractLinkTunnel) {
    Write-Host "Extract-link API: http://127.0.0.1:$ExtractLinkPort" -ForegroundColor Green
}

if (-not $NoBrowser) {
    Start-Process $WebUrl | Out-Null
}
