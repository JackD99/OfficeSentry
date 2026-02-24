# Setup-Cert.ps1
# Generates a self-signed cert and exports CER + PFX.
# Outputs a JSON result line that portal/app.py can parse.
# Called non-interactively by the Flask portal.

param(
    [Parameter(Mandatory)][string] $Subject,
    [Parameter(Mandatory)][string] $CerPath,
    [Parameter(Mandatory)][string] $PfxPath,
    [Parameter(Mandatory)][int]    $YearsValid,
    [Parameter(Mandatory)][string] $PfxPassword   # plaintext — passed via ConvertTo-SecureString in caller
)

$ErrorActionPreference = "Stop"

try {
    $pfxSecure = ConvertTo-SecureString $PfxPassword -AsPlainText -Force
    $notAfter  = (Get-Date).AddYears($YearsValid)

    $cert = New-SelfSignedCertificate `
        -Subject            $Subject `
        -CertStoreLocation  "Cert:\CurrentUser\My" `
        -KeyAlgorithm       RSA `
        -KeyLength          2048 `
        -HashAlgorithm      "SHA256" `
        -KeyExportPolicy    Exportable `
        -NotAfter           $notAfter `
        -KeySpec            Signature

    $cerDir = Split-Path $CerPath -Parent
    $pfxDir = Split-Path $PfxPath -Parent
    if (-not (Test-Path $cerDir)) { New-Item -ItemType Directory -Path $cerDir | Out-Null }
    if (-not (Test-Path $pfxDir)) { New-Item -ItemType Directory -Path $pfxDir | Out-Null }

    Export-Certificate    -Cert $cert -FilePath $CerPath -Force | Out-Null
    Export-PfxCertificate -Cert $cert -FilePath $PfxPath -Password $pfxSecure -Force | Out-Null

    @{
        ok         = $true
        thumbprint = $cert.Thumbprint
        notAfter   = $cert.NotAfter.ToString("yyyy-MM-dd")
        cerPath    = $CerPath
        pfxPath    = $PfxPath
    } | ConvertTo-Json -Compress
}
catch {
    @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
