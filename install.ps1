# Coomi installer for Windows — downloads the latest release binary.
#
# Usage:
#   irm https://raw.githubusercontent.com/Septemc/Coomi/main/install.ps1 | iex
#
# Or with options:
#   $env:COOMI_VERSION = "v1.0.0"; irm .../install.ps1 | iex

param(
    [string]$Version = "latest",
    [string]$InstallDir = "$env:USERPROFILE\.local\bin"
)

$ErrorActionPreference = "Stop"
$Repo = "Septemc/Coomi"
$BinaryName = "coomi.exe"

function Write-Info { param($Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Err { param($Msg) Write-Host "Error: $Msg" -ForegroundColor Red; exit 1 }

# Detect architecture
$arch = if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq "X64") { "x86_64" }
        elseif ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq "Arm64") { "aarch64" }
        else { Write-Err "Unsupported architecture" }

$target = "${arch}-pc-windows-msvc"

# Resolve version
if ($Version -eq "latest") {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/${Repo}/releases/latest"
    $Version = $release.tag_name
}
Write-Info "Version: $Version"

# Download
$url = "https://github.com/${Repo}/releases/download/${Version}/coomi-${target}.zip"
Write-Info "Platform: $target"
Write-Info "Downloading: $url"

$tmpDir = Join-Path $env:TEMP "coomi-install-$(Get-Random)"
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
$zipPath = Join-Path $tmpDir "coomi.zip"

try {
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
} catch {
    Write-Err "Failed to download: $_"
}

# Extract
Write-Info "Extracting..."
Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force

# Find binary
$binary = Get-ChildItem -Path $tmpDir -Filter $BinaryName -Recurse | Select-Object -First 1
if (-not $binary) {
    Write-Err "Binary not found in archive"
}

# Install
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$dest = Join-Path $InstallDir $BinaryName
Copy-Item $binary.FullName $dest -Force

Write-Info "Installed to $dest"

# Check PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$InstallDir*") {
    Write-Warning "$InstallDir is not in your PATH."
    Write-Host ""
    Write-Host "To add it permanently:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', '$InstallDir;' + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')"
    Write-Host ""
}

Write-Info "Done! Run 'coomi' to start."

# Cleanup
Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
