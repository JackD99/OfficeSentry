#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Retrieves GDAP and DAP partner relationships for an M365 tenant.
.DESCRIPTION
    Queries Microsoft Graph for delegated admin relationships including
    GDAP relationships, partner info, roles, and status.
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

    Write-Host "$(Get-Date) INFO: Fetching partner relationships for $Tenant"

    # GDAP relationships
    $relationships = @()
    try {
        $relationships = Invoke-GraphRequest `
            -Uri "https://graph.microsoft.com/v1.0/tenantRelationships/delegatedAdminRelationships" `
            -Headers $headers
    }
    catch {
        Write-Host "$(Get-Date) WARN: Could not fetch GDAP relationships: $_"
    }

    $relList = [System.Collections.Generic.List[PSCustomObject]]::new()
    foreach ($r in $relationships) {
        $relList.Add([PSCustomObject]@{
            Id          = $r.id
            DisplayName = $r.displayName
            Partner     = $r.partner.displayName
            TenantId    = $r.partner.tenantId
            Status      = $r.status
            Duration    = $r.duration
            CreatedDate = $r.createdDateTime
            EndDate     = $r.endDateTime
            AutoExtend  = $r.autoExtendDuration
            Roles       = ($r.accessDetails.unifiedRoles | ForEach-Object { $_.roleDefinitionId }) -join ", "
        })
    }

    Write-Host "$(Get-Date) INFO: Found $($relList.Count) partner relationship(s)"

    $output = @{
        ok            = $true
        relationships = @($relList | ForEach-Object { @{
            id          = $_.Id
            displayName = $_.DisplayName
            partner     = $_.Partner
            tenantId    = $_.TenantId
            status      = $_.Status
            duration    = $_.Duration
            createdDate = $_.CreatedDate
            endDate     = $_.EndDate
            autoExtend  = $_.AutoExtend
            roles       = $_.Roles
        }})
        count         = $relList.Count
    }
    $output | ConvertTo-Json -Depth 5 -Compress
}
catch {
    Write-Host "$(Get-Date) ERROR: $_"
    @{ ok = $false; error = "$_" } | ConvertTo-Json -Compress
}
