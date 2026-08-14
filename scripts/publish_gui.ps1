# publish_gui.ps1 - one-command dev release for the 印消 GUI package.
#
# ONEDIR release: the build produces a folder release-onefile/印消/ containing
# 印消.exe + _internal/ (built from scripts/build_gui.spec), plus LICENSE /
# DEPENDENCIES.txt / THIRD_PARTY_NOTICES.txt / SHA256SUMS.txt written inside
# the same folder by the spec. Onedir starts instantly (no per-launch
# extraction of 227MB to %TEMP% like onefile), at the cost of shipping a folder.
# This script assembles the release folder, labels it (signed or
# UNSIGNED-DEV-BUILD.txt), zips it and writes SHA256SUMS.txt with both exe and
# zip hashes. Hash updates retry on transient file locks.
#
# Usage:
#   ./scripts/publish_gui.ps1                 # dev package (unsigned, labeled)
#   ./scripts/publish_gui.ps1 -AllowUnsigned  # same, explicit
#   $env:SIGNCERT_THUMBPRINT=...; ./scripts/publish_gui.ps1 -Signed  # formal
#
# Callers: build_gui.spec must already have produced
#   release-onefile/印消/ (usually via
#   uv run --frozen --extra gui pyinstaller --clean --noconfirm --distpath release-onefile --workpath build-tmp-onedir scripts/build_gui.spec)

param(
    [switch]$AllowUnsigned,
    [switch]$Signed
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$appName = "印消"
$version = (Select-String -Path (Join-Path $root "pyproject.toml") -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
$srcDir = Join-Path $root "release-onefile\$appName"
$srcExe = Join-Path $srcDir "$appName.exe"
$relDir = Join-Path $root "release-new\$appName"
$zipPath = Join-Path $root "release-new\$appName-$version-dev.zip"
$exePath = Join-Path $relDir "$appName.exe"
$sumsPath = Join-Path $relDir "SHA256SUMS.txt"

if (-not (Test-Path -LiteralPath $srcExe)) {
    Write-Error "Build output not found: $srcExe (run the onedir pyinstaller build first)."
    exit 1
}

# Stop any running copy so the exe is not locked.
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -eq "RemoveAIWatermarks" -or $_.ProcessName -eq $appName } |
    ForEach-Object { Stop-Process -Id $_.Id -Force }
Start-Sleep -Milliseconds 500

# 1) Swap in the fresh build: whole app folder (exe + _internal/ + release
#    files written by the spec inside it).
if (Test-Path -LiteralPath $relDir) {
    Remove-Item -LiteralPath $relDir -Recurse -Force
}
New-Item -ItemType Directory -Path $relDir -Force | Out-Null
Copy-Item -LiteralPath $srcDir -Destination $relDir -Recurse -Force

# 2) Sign or label. Formal requires a certificate env var; -Signed asserts that
#    (sign_gui.ps1 fails loudly when no cert is present).
if ($Signed) {
    & (Join-Path $PSScriptRoot "sign_gui.ps1") -ExePath $exePath
} else {
    & (Join-Path $PSScriptRoot "sign_gui.ps1") -ExePath $exePath -AllowUnsigned
}

# 3) Zip. Compress-Archive keeps a short-lived read handle on files it just
#    read, so write the sums with retries afterwards.
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -Path $relDir -DestinationPath $zipPath -CompressionLevel Optimal

# 4) SHA256SUMS.txt: rewrite both exe and zip lines, retrying on lock.
function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

$exeHash = Get-Sha256 $exePath
$zipHash = Get-Sha256 $zipPath
# Keep any pre-existing checksum lines (LICENSE etc.); the exe + zip lines are
# rewritten below. The file may not exist on a first publish.
$keptLines = if (Test-Path -LiteralPath $sumsPath) {
    Get-Content -LiteralPath $sumsPath | Where-Object { $_ -notmatch '\.zip$' -and $_ -notmatch '\.exe$' }
} else {
    @()
}
$newLines = @($keptLines)
$newLines += "$exeHash  $appName.exe"
$newLines += "$zipHash  $appName-$version-dev.zip"

$attempts = 0
while ($attempts -lt 10) {
    try {
        Set-Content -LiteralPath $sumsPath -Value $newLines -Encoding UTF8
        # Verify: the file must contain exactly the exe + zip hashes we wrote.
        $after = Get-Content -LiteralPath $sumsPath
        if ($after -contains "$zipHash  $appName-$version-dev.zip" -and $after -contains "$exeHash  $appName.exe") {
            break
        }
    } catch {
        Start-Sleep -Milliseconds 200
    }
    $attempts++
}
if ($attempts -ge 10) {
    Write-Error "Could not write $sumsPath after retries (file locked)."
    exit 1
}

Write-Host "Published $appName v${version}:"
Write-Host "  EXE  $exeHash"
Write-Host "  ZIP  $zipHash"
Write-Host "  at   $zipPath"
