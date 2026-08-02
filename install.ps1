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

# Detect architecture — use PROCESSOR_ARCHITECTURE for robust detection across
# PowerShell versions and environments (Conda, old Windows PowerShell 5.1, etc.)
$procArch = $env:PROCESSOR_ARCHITECTURE
$arch = switch ($procArch) {
    "AMD64"  { "x86_64" }
    "ARM64"  { "aarch64" }
    "x86"    { "x86_64" }   # 32-bit PS on 64-bit OS
    default  { $null }
}
if (-not $arch) {
    Write-Err "Unsupported architecture: $procArch. Expected AMD64 or ARM64."
}

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

# Install — overwrites any previous version
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$dest = Join-Path $InstallDir $BinaryName
if (Test-Path $dest) {
    $existingVersion = & $dest --version 2>$null
    if ($existingVersion) {
        Write-Info "Upgrading from $existingVersion"
    } else {
        Write-Info "Overwriting existing installation at $dest"
    }
}
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
