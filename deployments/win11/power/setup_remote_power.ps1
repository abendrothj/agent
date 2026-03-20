param(
    [Parameter(Mandatory = $true)]
    [string]$PiIp,

    [string]$RepoPath = "C:\Users\jakea\Desktop\agent",
    [int]$GrpcPort = 50051,
    [int]$SshPort = 22
)

$ErrorActionPreference = "Stop"

function Require-Admin {
    $current = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $current.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script in an elevated PowerShell session (Run as Administrator)."
    }
}

function Ensure-OpenSshServer {
    $cap = Get-WindowsCapability -Online | Where-Object { $_.Name -like "OpenSSH.Server*" }
    if ($cap.State -ne "Installed") {
        Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    }

    Set-Service -Name sshd -StartupType Automatic
    Start-Service -Name sshd
}

function Ensure-FirewallRule {
    param(
        [string]$Name,
        [int]$Port,
        [string]$RemoteAddress
    )

    $existing = Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Remove-NetFirewallRule -DisplayName $Name
    }

    New-NetFirewallRule -DisplayName $Name `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -RemoteAddress $RemoteAddress | Out-Null
}

function Ensure-DockerAutoStartTask {
    param([string]$Repo)

    $taskName = "Teammate-Muscle-Docker-On-Startup"
    $cmd = "docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml up -d"
    $taskCommand = "Set-Location '{0}'; {1}" -f $Repo, $cmd
    $quote = [char]34
    $taskArgs = "-NoProfile -ExecutionPolicy Bypass -Command $quote$taskCommand$quote"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArgs
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType S4U -RunLevel Highest

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
}

Require-Admin
Ensure-OpenSshServer
Ensure-FirewallRule -Name "Teammate-Muscle-gRPC-From-Pi" -Port $GrpcPort -RemoteAddress $PiIp
Ensure-FirewallRule -Name "Teammate-Muscle-SSH-From-Pi" -Port $SshPort -RemoteAddress $PiIp
Ensure-DockerAutoStartTask -Repo $RepoPath

Write-Host "Remote power control setup complete." -ForegroundColor Green
Write-Host "Pi IP allowed: $PiIp"
Write-Host "SSH service: running"
Write-Host "gRPC/SSH firewall rules: configured"
Write-Host "Startup task: Teammate-Muscle-Docker-On-Startup"
