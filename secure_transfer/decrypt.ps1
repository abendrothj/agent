param(
    [Parameter(Mandatory = $true)]
    [string]$Password
)

$ErrorActionPreference = 'Stop'

$enc = Join-Path $PSScriptRoot 'muscle-certs.zip.enc'
$zip = Join-Path $PSScriptRoot 'muscle-certs.zip'
$outDir = Join-Path $PSScriptRoot 'decrypted-certs'
$openssl = 'C:\Program Files\OpenSSL-Win64\bin\openssl.exe'

if (-not (Test-Path $enc)) {
    throw "Encrypted bundle not found: $enc"
}

if (-not (Test-Path $openssl)) {
    throw "OpenSSL not found at: $openssl"
}

& $openssl enc -d -aes-256-cbc -pbkdf2 -in $enc -out $zip -pass "pass:$Password"

if (Test-Path $outDir) {
    Remove-Item -Recurse -Force $outDir
}
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Expand-Archive -Path $zip -DestinationPath $outDir -Force
Remove-Item $zip -Force

Write-Host "Decrypted certs written to: $outDir"
