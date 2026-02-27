<#
.SYNOPSIS
    Detects hybrid identity / Entra Connect status for a tenant via Graph API.

.DESCRIPTION
    Uses Microsoft Graph to check for directory sync indicators:
    - Organization.onPremisesSyncEnabled
    - Organization.onPremisesLastSyncDateTime
    - Entra Connect health service members (best-effort)

    Outputs JSON result on the last line for portal parsing.

.PARAMETER TenantId
    The tenant ID or domain name.

.PARAMETER ClientId
    The Entra application (client) ID.

.PARAMETER CertPath
    Path to the PFX certificate file.
#>

#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$TenantId,
    [Parameter(Mandatory)][string]$ClientId,
    [Parameter(Mandatory)][string]$CertPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) {
    Import-Module $sharedModule -Force
}

Write-Host "$(Get-Date) INFO: Starting hybrid identity detection for $TenantId"

$result = @{
    ok                = $true
    hybrid_detected   = $false
    sync_enabled      = $false
    last_sync         = $null
    server_names      = @()
    details           = ""
}

try {
    # Get Graph token
    $certPassword = Get-AuditCertificatePassword
    $token = Get-AuditGraphToken -TenantId $TenantId -ClientId $ClientId `
        -CertPath $CertPath -CertPassword $certPassword

    if (-not $token) {
        throw "Failed to acquire Graph token"
    }

    $headers = @{ Authorization = "Bearer $token" }

    # Check organization sync status
    Write-Host "$(Get-Date) INFO: Querying organization sync status"
    $orgUri = "https://graph.microsoft.com/v1.0/organization"
    $orgResponse = Invoke-RestMethod -Uri $orgUri -Headers $headers -ErrorAction Stop

    if ($orgResponse.value -and $orgResponse.value.Count -gt 0) {
        $org = $orgResponse.value[0]
        $syncEnabled = $org.onPremisesSyncEnabled
        $lastSync = $org.onPremisesLastSyncDateTime

        if ($syncEnabled) {
            $result.hybrid_detected = $true
            $result.sync_enabled = $true
            $result.last_sync = $lastSync
            Write-Host "$(Get-Date) INFO: Directory sync IS enabled. Last sync: $lastSync"
        } else {
            Write-Host "$(Get-Date) INFO: Directory sync is NOT enabled"
        }
    }

    # Best-effort: try to get Entra Connect server names via service principals
    # The Entra Connect service typically registers a service principal
    Write-Host "$(Get-Date) INFO: Attempting to detect Entra Connect server names (best-effort)"
    try {
        $spUri = "https://graph.microsoft.com/v1.0/servicePrincipals?" +
                 "`$filter=startswith(displayName,'Microsoft Azure AD Connect') or " +
                 "startswith(displayName,'Azure AD Connect') or " +
                 "startswith(displayName,'Entra Connect')&" +
                 "`$select=displayName,appId,notes"
        $spResponse = Invoke-RestMethod -Uri $spUri -Headers $headers -ErrorAction Stop

        if ($spResponse.value -and $spResponse.value.Count -gt 0) {
            foreach ($sp in $spResponse.value) {
                Write-Host "$(Get-Date) INFO: Found sync service principal: $($sp.displayName)"
                # Notes sometimes contain server name
                if ($sp.notes) {
                    $result.server_names += $sp.notes
                }
            }
        }
    } catch {
        Write-Host "$(Get-Date) WARN: Could not query service principals for server names: $_"
    }

    # Best-effort: check users with onPremisesSyncEnabled to confirm hybrid
    if (-not $result.hybrid_detected) {
        Write-Host "$(Get-Date) INFO: Checking for on-prem synced users as fallback"
        try {
            $userUri = "https://graph.microsoft.com/v1.0/users?" +
                       "`$filter=onPremisesSyncEnabled eq true&`$top=1&`$select=id"
            $userResponse = Invoke-RestMethod -Uri $userUri -Headers $headers -ErrorAction Stop
            if ($userResponse.value -and $userResponse.value.Count -gt 0) {
                $result.hybrid_detected = $true
                Write-Host "$(Get-Date) INFO: Found synced users — hybrid identity confirmed"
            }
        } catch {
            Write-Host "$(Get-Date) WARN: User filter query failed: $_"
        }
    }

    if ($result.hybrid_detected) {
        $result.details = "Hybrid identity detected"
        if ($result.last_sync) { $result.details += ". Last sync: $($result.last_sync)" }
        if ($result.server_names.Count -gt 0) { $result.details += ". Servers: $($result.server_names -join ', ')" }
    } else {
        $result.details = "No hybrid identity detected (cloud-only tenant)"
    }

} catch {
    $result.ok = $false
    $result.details = "Detection failed: $_"
    Write-Host "$(Get-Date) ERROR: $_"
}

Write-Host "$(Get-Date) INFO: Detection complete. Hybrid: $($result.hybrid_detected)"

# Output JSON for portal parsing (last line)
$result | ConvertTo-Json -Compress
