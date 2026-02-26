# Get-IdentityProtection.ps1
# Retrieves identity protection data from Microsoft Entra ID via Graph API.
# Supports risky users, risky sign-ins, MFA gap analysis, password protection,
# and risk overview report types.
# Requires: MSAL.PS, ImportExcel (optional)

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OutputFolder = ".",
    [Parameter(Mandatory)]
    [ValidateSet("riskyUsers", "riskySignIns", "mfaGap", "passwordProtection",
                 "riskOverview", "dismissRisk")]
    [string] $ReportType,
    [string] $RiskLevel = "",
    [string] $Days = "30",
    [string] $TargetUserId = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Acquiring Graph token..."
$token = Get-AuditGraphToken `
    -Tenant              $Tenant `
    -ClientId            $ClientId `
    -CertificatePath     $CertificatePath `
    -CertificatePassword $CertificatePassword

$accessToken = $token.AccessToken
$safeTenant = ($Tenant -replace '[^\w\-\.]', '_')
$ts = Get-Date -Format "yyyyMMdd-HHmm"

switch ($ReportType) {
    "riskyUsers" {
        Write-Host "$(Get-Date) INFO: Fetching risky users..."
        $uri = "https://graph.microsoft.com/v1.0/identityProtection/riskyUsers"
        if ($RiskLevel) {
            $uri += "?`$filter=riskLevel eq '$RiskLevel'"
        }

        $riskyUsers = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 100
        Write-Host "$(Get-Date) INFO: Found $($riskyUsers.Count) risky user(s)."

        if ($riskyUsers.Count -gt 0) {
            $results = $riskyUsers | Select-Object id, userPrincipalName, userDisplayName,
                riskLevel, riskState, riskDetail, riskLastUpdatedDateTime, isProcessing, isDeleted
            $baseName = "RiskyUsers_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        } else {
            Write-Host "$(Get-Date) INFO: No risky users found."
        }
    }

    "riskySignIns" {
        Write-Host "$(Get-Date) INFO: Fetching risky sign-ins (last $Days days)..."
        $startDT = (Get-Date).AddDays(-[int]$Days).ToString("yyyy-MM-ddT00:00:00Z")
        $filter = "createdDateTime ge $startDT"
        if ($RiskLevel) {
            $filter += " and riskLevelDuringSignIn eq '$RiskLevel'"
        }
        $uri = "https://graph.microsoft.com/v1.0/identityProtection/riskyServicePrincipals?`$filter=$filter"

        # Fallback to sign-in logs with risk info
        $uri = "https://graph.microsoft.com/v1.0/auditLogs/signIns?`$filter=$filter and riskLevelDuringSignIn ne 'none'&`$top=999"

        $riskySignIns = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 100
        Write-Host "$(Get-Date) INFO: Found $($riskySignIns.Count) risky sign-in(s)."

        if ($riskySignIns.Count -gt 0) {
            $results = $riskySignIns | Select-Object createdDateTime, userPrincipalName,
                userDisplayName, appDisplayName, ipAddress, location,
                riskLevelDuringSignIn, riskLevelAggregated, riskDetail, riskState,
                conditionalAccessStatus
            $baseName = "RiskySignIns_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        } else {
            Write-Host "$(Get-Date) INFO: No risky sign-ins found."
        }
    }

    "mfaGap" {
        Write-Host "$(Get-Date) INFO: Running MFA gap analysis..."
        $uri = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails"
        $mfaData = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 200
        Write-Host "$(Get-Date) INFO: Retrieved MFA registration data for $($mfaData.Count) user(s)."

        $noMfa = $mfaData | Where-Object { $_.isMfaRegistered -eq $false }
        Write-Host "$(Get-Date) INFO: Users without MFA: $($noMfa.Count) / $($mfaData.Count)"

        if ($mfaData.Count -gt 0) {
            $results = $mfaData | Select-Object id, userPrincipalName, userDisplayName,
                isMfaRegistered, isMfaCapable, isSsprRegistered, isSsprEnabled, isSsprCapable,
                isPasswordlessCapable, methodsRegistered
            $baseName = "MFAGapAnalysis_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        }
    }

    "passwordProtection" {
        Write-Host "$(Get-Date) INFO: Fetching password protection settings..."
        $uri = "https://graph.microsoft.com/v1.0/settings"
        try {
            $settings = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 1
            Write-Host "$(Get-Date) INFO: Password protection settings retrieved."
            $settings | ConvertTo-Json -Depth 10
        } catch {
            Write-Host "$(Get-Date) WARN: Could not fetch password protection settings: $($_.Exception.Message)"
        }
    }

    "riskOverview" {
        Write-Host "$(Get-Date) INFO: Generating risk overview..."

        # Risky users summary
        $riskyUri = "https://graph.microsoft.com/v1.0/identityProtection/riskyUsers"
        $riskyUsers = Invoke-GraphRequest -Uri $riskyUri -AccessToken $accessToken -MaxPages 50
        $highRisk = ($riskyUsers | Where-Object {$_.riskLevel -eq "high"}).Count
        $medRisk  = ($riskyUsers | Where-Object {$_.riskLevel -eq "medium"}).Count
        $lowRisk  = ($riskyUsers | Where-Object {$_.riskLevel -eq "low"}).Count

        # MFA summary
        $mfaUri = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails"
        $mfaData = Invoke-GraphRequest -Uri $mfaUri -AccessToken $accessToken -MaxPages 200
        $noMfaCount = ($mfaData | Where-Object {$_.isMfaRegistered -eq $false}).Count

        $overview = [PSCustomObject]@{
            Tenant            = $Tenant
            ReportDate        = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
            TotalRiskyUsers   = $riskyUsers.Count
            HighRiskUsers     = $highRisk
            MediumRiskUsers   = $medRisk
            LowRiskUsers      = $lowRisk
            TotalUsers        = $mfaData.Count
            UsersWithoutMFA   = $noMfaCount
            MFACoveragePercent = if ($mfaData.Count -gt 0) {
                [math]::Round((($mfaData.Count - $noMfaCount) / $mfaData.Count) * 100, 1)
            } else { 0 }
        }

        $baseName = "RiskOverview_${safeTenant}_${ts}"
        Export-AuditData -Data @($overview) -OutputFolder $OutputFolder -BaseName $baseName
        Write-Host "$(Get-Date) INFO: Risk overview — Risky users: $($riskyUsers.Count) (H:$highRisk M:$medRisk L:$lowRisk), MFA coverage: $($overview.MFACoveragePercent)%"
    }

    "dismissRisk" {
        if (-not $TargetUserId) {
            Write-Host "$(Get-Date) ERROR: TargetUserId is required for dismissRisk."
            exit 1
        }
        Write-Host "$(Get-Date) INFO: Dismissing risk for user: $TargetUserId..."
        $uri = "https://graph.microsoft.com/v1.0/identityProtection/riskyUsers/dismiss"
        $body = @{ userIds = @($TargetUserId) } | ConvertTo-Json
        $headers = @{
            "Authorization" = "Bearer $accessToken"
            "Content-Type"  = "application/json"
        }
        Invoke-RestMethod -Uri $uri -Method POST -Headers $headers -Body $body
        Write-Host "$(Get-Date) INFO: Risk dismissed for user: $TargetUserId"
    }
}

Write-Host "$(Get-Date) INFO: Identity protection report complete."
