#requires -Modules MSAL.PS

<#
.SYNOPSIS
    Fetches the Microsoft Secure Score for a tenant via the Microsoft Graph API.
.DESCRIPTION
    Uses certificate-based app-only authentication to retrieve the latest Secure Score
    data from Microsoft Graph (/security/secureScores). Returns a JSON object with
    currentScore, maxScore, averageComparativeScores, and control scores.
.PARAMETER Tenant
    The tenant domain or GUID (e.g., contoso.onmicrosoft.com or tenant GUID).
.PARAMETER ClientId
    The Entra (Azure AD) application Client ID.
.PARAMETER CertificatePath
    Path to the PFX certificate file for app-only auth.
.EXAMPLE
    pwsh -File Get-SecureScore.ps1 -Tenant contoso.onmicrosoft.com -ClientId <guid> -CertificatePath ./cert.pfx
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
Import-Module $sharedModule -Force

# ---- Get certificate password ----
$pfxPassword = Get-AuditCertificatePassword

# ---- Load certificate for thumbprint ----
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
    $CertificatePath, $pfxPassword, "EphemeralKeySet"
)
$thumbprint = $cert.Thumbprint

# ---- Acquire Graph token via MSAL ----
Write-Host "$(Get-Date) INFO: Acquiring Graph API token for $Tenant..."

$tokenParams = @{
    ClientId              = $ClientId
    TenantId              = $Tenant
    ClientCertificate     = $cert
    Scopes                = @("https://graph.microsoft.com/.default")
}

$tokenResult = Get-MsalToken @tokenParams
$accessToken = $tokenResult.AccessToken

if (-not $accessToken) {
    $result = @{ ok = $false; error = "Failed to acquire Graph API token." }
    Write-Output ($result | ConvertTo-Json -Compress)
    exit 1
}

# ---- Fetch Secure Score ----
Write-Host "$(Get-Date) INFO: Fetching Secure Score..."

$headers = @{
    Authorization  = "Bearer $accessToken"
    "Content-Type" = "application/json"
}

try {
    $response = Invoke-RestMethod `
        -Uri "https://graph.microsoft.com/v1.0/security/secureScores?`$top=1" `
        -Headers $headers `
        -Method Get

    if (-not $response.value -or $response.value.Count -eq 0) {
        $result = @{ ok = $false; error = "No Secure Score data available for this tenant." }
        Write-Output ($result | ConvertTo-Json -Compress)
        exit 1
    }

    $score = $response.value[0]

    # Extract average comparative scores
    $comparatives = @{}
    foreach ($avg in $score.averageComparativeScores) {
        $comparatives[$avg.basis] = $avg.averageScore
    }

    # Extract control scores (top improvement actions)
    $controlScores = @()
    foreach ($ctrl in ($score.controlScores | Sort-Object { $_.scoreInPercentage } | Select-Object -First 20)) {
        $controlScores += @{
            name        = $ctrl.controlName
            description = $ctrl.description
            score       = $ctrl.score
            maxScore    = $ctrl.maxScore
            percentage  = $ctrl.scoreInPercentage
            category    = $ctrl.controlCategory
        }
    }

    $result = @{
        ok                    = $true
        currentScore          = $score.currentScore
        maxScore              = $score.maxScore
        percentage            = if ($score.maxScore -gt 0) { [math]::Round(($score.currentScore / $score.maxScore) * 100, 1) } else { 0 }
        createdDateTime       = $score.createdDateTime
        averageComparatives   = $comparatives
        enabledServices       = $score.enabledServices
        controlScores         = $controlScores
    }

    Write-Host "$(Get-Date) INFO: Secure Score: $($score.currentScore)/$($score.maxScore)"
    Write-Output ($result | ConvertTo-Json -Depth 5 -Compress)
}
catch {
    Write-Host "$(Get-Date) ERROR: $($_.Exception.Message)"
    $result = @{
        ok    = $false
        error = "Failed to fetch Secure Score: $($_.Exception.Message)"
    }
    Write-Output ($result | ConvertTo-Json -Compress)
    exit 1
}
