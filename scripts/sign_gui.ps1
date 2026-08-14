# sign_gui.ps1 — Authenticode signing / dev-package labeling for the GUI release.
#
# Formal release (requires a certificate; secrets come ONLY from the environment,
# never from the repo):
#   $env:SIGNCERT_THUMBPRINT = "<thumbprint of the code-signing cert>"
#   ./scripts/sign_gui.ps1 -ExePath release-new/RemoveAIWatermarks/RemoveAIWatermarks.exe
#
# Dev package (no certificate available):
#   ./scripts/sign_gui.ps1 -ExePath release-new/RemoveAIWatermarks/RemoveAIWatermarks.exe -AllowUnsigned
#   -> writes UNSIGNED-DEV-BUILD.txt next to the exe and exits 0. The package is
#      clearly labeled; it is NOT reported as a signed formal release.
#
# Without -AllowUnsigned and without a certificate, the script FAILS (exit 1):
# a formal release must never silently ship unsigned.

param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,
    [switch]$AllowUnsigned
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Error "EXE not found: $ExePath"
    exit 1
}

$exeDir = Split-Path -Parent $ExePath
$exeName = Split-Path -Leaf $ExePath
$thumbprint = $env:SIGNCERT_THUMBPRINT

if (-not $thumbprint) {
    if ($AllowUnsigned) {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss 'UTC'"
        $marker = @"
UNSIGNED DEVELOPMENT BUILD
==========================
$exeName was built from source but has NOT been Authenticode-signed.
It carries no code-signing certificate, so Windows may warn when it runs.
Do not distribute this as a signed formal release.
Built at: $stamp
"@
        $markerPath = Join-Path $exeDir "UNSIGNED-DEV-BUILD.txt"
        Set-Content -LiteralPath $markerPath -Value $marker -Encoding UTF8
        Write-Host "Unsigned DEV package labeled at $markerPath (exit 0)."
        exit 0
    }
    Write-Error "No code-signing certificate: set `$env:SIGNCERT_THUMBPRINT or pass -AllowUnsigned for a labeled dev package."
    exit 1
}

$cert = Get-Item -Path "Cert:\CurrentUser\My\$thumbprint" -ErrorAction SilentlyContinue
if (-not $cert) {
    $cert = Get-Item -Path "Cert:\LocalMachine\My\$thumbprint" -ErrorAction SilentlyContinue
}
if (-not $cert) {
    Write-Error "Certificate with thumbprint $thumbprint not found in My stores."
    exit 1
}

# Timestamp with a public RFC 3161 service (DigiCert), so the signature survives
# certificate expiry. The service URL is public infrastructure, not a secret.
Set-AuthenticodeSignature `
    -FilePath $ExePath `
    -Certificate $cert `
    -TimestampServer "http://timestamp.digicert.com" | Out-Null

$sig = Get-AuthenticodeSignature -FilePath $ExePath
if ($sig.Status -ne "Valid") {
    Write-Error "Signature verification failed: $($sig.Status) — $($sig.StatusMessage)"
    exit 1
}
Write-Host "Signed OK: $ExePath (Status=$($sig.Status), Signer=$($sig.SignerCertificate.Subject))"
