param(
    [string]$RepoPath = "C:\Users\jakea\Desktop\agent",
    [string]$Ref = "main",
    [string]$MuscleImage = "",
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

Set-Location $RepoPath

if (-not (Test-Path ".git")) {
    throw "RepoPath does not appear to be a git repository: $RepoPath"
}

$dirty = git status --porcelain
if (-not $AllowDirty -and $dirty) {
    throw "Working tree is dirty. Commit/stash changes first, or rerun with -AllowDirty."
}

Write-Host "Fetching latest from origin..." -ForegroundColor Cyan
git fetch --all --prune

Write-Host "Checking out ref: $Ref" -ForegroundColor Cyan
git checkout $Ref

# Pull only when on a branch. For tags/SHAs, checkout already pins revision.
$currentBranch = git rev-parse --abbrev-ref HEAD
if ($currentBranch -ne "HEAD") {
    git pull --ff-only origin $currentBranch
}

if ($MuscleImage) {
    Write-Host "Deploying from image: $MuscleImage" -ForegroundColor Cyan

    $envPath = Join-Path $RepoPath "deployments/win11/.env"
    if (-not (Test-Path $envPath)) {
        throw "Win11 env file not found: $envPath"
    }

    $envContent = Get-Content $envPath -Raw
    if ($envContent -match "(?m)^MUSCLE_IMAGE=") {
        $envContent = [regex]::Replace($envContent, "(?m)^MUSCLE_IMAGE=.*$", "MUSCLE_IMAGE=$MuscleImage")
    } else {
        if (-not $envContent.EndsWith("`n")) {
            $envContent += "`n"
        }
        $envContent += "MUSCLE_IMAGE=$MuscleImage`n"
    }
    Set-Content -Path $envPath -Value $envContent

    docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml pull
    docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml up -d
} else {
    Write-Host "Deploying Muscle container from local source build..." -ForegroundColor Cyan
    docker build -f cmd/muscle/Dockerfile -t win11-muscle:latest .
    docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml up -d
}

Write-Host "Deployment status:" -ForegroundColor Green
docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml ps
