#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Retrieves pending app consent requests for an M365 tenant.
.DESCRIPTION
    Queries Microsoft Graph for application consent requests that are
    pending admin review, including requested permissions and requestor details.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tenant,
    [Parameter(Mandatory)][string]$ClientId,
    [Parameter(Mandatory)][string]$CertificatePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
Import-Module $sharedModule -Force

try {
    $token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId -CertificatePath $CertificatePath
    $headers = @{ Authorization = "Bearer $token" }

    Write-Host "$(Get-Date) INFO: Fetching app consent requests for $Tenant"

    $requests = @()
    try {
        $requests = Invoke-GraphRequest `
            -Uri "https://graph.microsoft.com/v1.0/identityGovernance/appConsent/appConsentRequests" `
            -Headers $headers
    }
    catch {
        Write-Host "$(Get-Date) WARN: Could not fetch consent requests (may require additional permissions): $_"
    }

    $requestList = [System.Collections.Generic.List[PSCustomObject]]::new()
    foreach ($r in $requests) {
        $requestList.Add([PSCustomObject]@{
            Id          = $r.id
            AppId       = $r.appId
            AppName     = $r.appDisplayName
            Status      = $r.consentType
            Pending     = ($r.pendingScopes | ForEach-Object { $_.displayName }) -join ", "
        })
    }

    Write-Host "$(Get-Date) INFO: Found $($requestList.Count) consent request(s)"

    $output = @{
        ok       = $true
        requests = @($requestList | ForEach-Object { @{
            id      = $_.Id
            appId   = $_.AppId
            appName = $_.AppName
            status  = $_.Status
            pending = $_.Pending
        }})
        count    = $requestList.Count
    }
    $output | ConvertTo-Json -Depth 5 -Compress
}
catch {
    Write-Host "$(Get-Date) ERROR: $_"
    @{ ok = $false; error = "$_" } | ConvertTo-Json -Compress
}
