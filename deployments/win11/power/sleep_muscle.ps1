param(
    [string]$RepoPath = "C:\Users\jakea\Desktop\agent",
    [ValidateSet("sleep", "shutdown")]
    [string]$Mode = "sleep",
    [switch]$SkipDockerStop
)

$ErrorActionPreference = "Stop"

if (-not $SkipDockerStop) {
    Set-Location $RepoPath
    docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml down
}

if ($Mode -eq "shutdown") {
    shutdown.exe /s /t 0
    exit 0
}

# Sleep (S3/S0ix support depends on motherboard/firmware settings)
Start-Process -FilePath "rundll32.exe" -ArgumentList "powrprof.dll,SetSuspendState 0,1,0" -WindowStyle Hidden
