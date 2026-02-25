#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Retrieves Conditional Access policies from an M365 tenant.
.DESCRIPTION
    Queries Microsoft Graph for all Conditional Access policies and named locations,
    returning policy details including conditions, grant controls, and session controls.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tenant,
    [Parameter(Mandatory)][string]$ClientId,
    [Parameter(Mandatory)][string]$CertificatePath,
    [ValidateSet("policies", "namedLocations", "both")]
    [string]$DataType = "both"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
Import-Module $sharedModule -Force

try {
    $token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId -CertificatePath $CertificatePath
    $headers = @{ Authorization = "Bearer $token" }

    $output = @{ ok = $true }

    # Conditional Access Policies
    if ($DataType -in "policies", "both") {
        Write-Host "$(Get-Date) INFO: Fetching Conditional Access policies for $Tenant"

        $policies = Invoke-GraphRequest -Uri "https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies" `
                                        -Headers $headers

        $policyList = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($p in $policies) {
            $policyList.Add([PSCustomObject]@{
                Id              = $p.id
                DisplayName     = $p.displayName
                State           = $p.state
                CreatedDateTime = $p.createdDateTime
                ModifiedDateTime = $p.modifiedDateTime
                Conditions      = ($p.conditions | ConvertTo-Json -Depth 5 -Compress)
                GrantControls   = ($p.grantControls | ConvertTo-Json -Depth 3 -Compress)
                SessionControls = ($p.sessionControls | ConvertTo-Json -Depth 3 -Compress)
            })
        }

        Write-Host "$(Get-Date) INFO: Found $($policyList.Count) CA policy/policies"

        $output.policies = @($policyList | ForEach-Object { @{
            id               = $_.Id
            displayName      = $_.DisplayName
            state            = $_.State
            createdDateTime  = $_.CreatedDateTime
            modifiedDateTime = $_.ModifiedDateTime
            conditions       = $_.Conditions
            grantControls    = $_.GrantControls
            sessionControls  = $_.SessionControls
        }})
        $output.policyCount = $policyList.Count
    }

    # Named Locations
    if ($DataType -in "namedLocations", "both") {
        Write-Host "$(Get-Date) INFO: Fetching Named Locations for $Tenant"

        $locations = Invoke-GraphRequest -Uri "https://graph.microsoft.com/v1.0/identity/conditionalAccess/namedLocations" `
                                         -Headers $headers

        $locationList = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($l in $locations) {
            $locationList.Add([PSCustomObject]@{
                Id              = $l.id
                DisplayName     = $l.displayName
                Type            = $l.'@odata.type'
                CreatedDateTime = $l.createdDateTime
                ModifiedDateTime = $l.modifiedDateTime
                IsTrusted       = $l.isTrusted
                Details         = ($l | ConvertTo-Json -Depth 3 -Compress)
            })
        }

        Write-Host "$(Get-Date) INFO: Found $($locationList.Count) named location(s)"

        $output.namedLocations = @($locationList | ForEach-Object { @{
            id               = $_.Id
            displayName      = $_.DisplayName
            type             = $_.Type
            createdDateTime  = $_.CreatedDateTime
            modifiedDateTime = $_.ModifiedDateTime
            isTrusted        = $_.IsTrusted
            details          = $_.Details
        }})
        $output.locationCount = $locationList.Count
    }

    $output | ConvertTo-Json -Depth 5 -Compress
}
catch {
    Write-Host "$(Get-Date) ERROR: $_"
    @{ ok = $false; error = "$_" } | ConvertTo-Json -Compress
}
