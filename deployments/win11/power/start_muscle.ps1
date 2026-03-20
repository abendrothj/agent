param(
    [string]$RepoPath = "C:\Users\jakea\Desktop\agent"
)

$ErrorActionPreference = "Stop"
Set-Location $RepoPath

docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml up -d

docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml ps
