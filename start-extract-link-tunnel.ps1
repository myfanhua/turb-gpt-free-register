[CmdletBinding()]
param(
    [string]$RemoteHost = "154.44.13.150",
    [string]$RemoteUser = "root",
    [int]$SshPort = 22,
    [int]$RemoteServicePort = 8085,
    [int]$LocalPort = 8085,
    [int]$WaitSeconds = 15,
    [string]$KeyPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectRoot ".runtime"
if (-not $KeyPath) {
    $KeyPath = Join-Path $RuntimeDir "extract-link-ssh-ed25519"
}
$KnownHosts = Join-Path $RuntimeDir "extract-link-known-hosts"
$PidFile = Join-Path $RuntimeDir "extract-link-tunnel.pid"
$StdoutLog = Join-Path $RuntimeDir "extract-link-tunnel.stdout.log"
$StderrLog = Join-Path $RuntimeDir "extract-link-tunnel.stderr.log"

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

function Test-ExtractLinkApi {
    try {
        $response = Invoke-WebRequest `
            -Uri "http://127.0.0.1:$LocalPort/api/upi/jobs" `
            -UseBasicParsing `
            -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match '"jobs"'
    }
    catch {
        return $false
    }
}

function Start-ExtractLinkTunnel {
    if (Test-ExtractLinkApi) {
        Write-Host "Extract-link API is ready on local port $LocalPort." -ForegroundColor Green
        return
    }

    $listener = Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        throw "Local port $LocalPort is occupied by PID $($listener.OwningProcess), but it is not the extract-link API."
    }

    if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
        throw "Extract-link SSH key not found: $KeyPath"
    }

    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $forward = "127.0.0.1:${LocalPort}:127.0.0.1:${RemoteServicePort}"
    $sshArgs = @(
        "-N", "-T",
        "-p", "$SshPort",
        "-L", $forward,
        "-i", $KeyPath,
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=$KnownHosts",
        "${RemoteUser}@${RemoteHost}"
    )

    Remove-Item -LiteralPath $StdoutLog, $StderrLog -Force -ErrorAction SilentlyContinue
    $process = Start-Process `
        -FilePath $ssh `
        -ArgumentList $sshArgs `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -PassThru
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        if (Test-ExtractLinkApi) {
            Write-Host "Extract-link tunnel is ready: http://127.0.0.1:$LocalPort -> ${RemoteHost}:$RemoteServicePort" -ForegroundColor Green
            return
        }
        if ($process.HasExited) {
            $detail = if (Test-Path -LiteralPath $StderrLog) {
                (Get-Content -LiteralPath $StderrLog -Raw).Trim()
            }
            else {
                "ssh exited with code $($process.ExitCode)"
            }
            throw "Extract-link SSH tunnel failed: $detail"
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "Extract-link SSH tunnel did not become ready within ${WaitSeconds}s."
}

Start-ExtractLinkTunnel
