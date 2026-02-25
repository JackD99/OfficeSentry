#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Backs up the configuration of an M365 tenant.
.DESCRIPTION
    Creates a comprehensive backup of tenant configuration including:
    - Conditional Access policies
    - Authentication method policies
    - Named locations
    - Domain settings
    - Organization settings
    Outputs backup data as JSON on the last line for portal parsing.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tenant,
    [Parameter(Mandatory)][string]$ClientId,
    [Parameter(Mandatory)][string]$CertificatePath,
    [string[]]$Components = @("conditionalAccess", "authMethods", "namedLocations", "organization", "domains")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
Import-Module $sharedModule -Force

try {
    $token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId -CertificatePath $CertificatePath
    $headers = @{ Authorization = "Bearer $token" }

    $backup = @{
        tenant     = $Tenant
        timestamp  = (Get-Date -Format "o")
        components = @{}
    }

    $total = $Components.Count
    $current = 0

    foreach ($component in $Components) {
        $current++
        Write-Progress -Activity "Backing up $Tenant" -Status "$component" `
                       -PercentComplete (($current / $total) * 100)
        Write-Host "$(Get-Date) INFO: Backing up $component ($current/$total)"

        switch ($component) {
            "conditionalAccess" {
                $policies = Invoke-GraphRequest `
                    -Uri "https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies" `
                    -Headers $headers
                $backup.components.conditionalAccess = @{
                    policies = @($policies)
                    count    = $policies.Count
                }
            }
            "authMethods" {
                $policy = Invoke-RestMethod `
                    -Uri "https://graph.microsoft.com/v1.0/policies/authenticationMethodsPolicy" `
                    -Headers $headers -Method Get
                $backup.components.authMethods = @{
                    policy = $policy
                }
            }
            "namedLocations" {
                $locations = Invoke-GraphRequest `
                    -Uri "https://graph.microsoft.com/v1.0/identity/conditionalAccess/namedLocations" `
                    -Headers $headers
                $backup.components.namedLocations = @{
                    locations = @($locations)
                    count     = $locations.Count
                }
            }
            "organization" {
                $org = Invoke-RestMethod `
                    -Uri "https://graph.microsoft.com/v1.0/organization" `
                    -Headers $headers -Method Get
                $backup.components.organization = @{
                    details = $org.value
                }
            }
            "domains" {
                $domains = Invoke-GraphRequest `
                    -Uri "https://graph.microsoft.com/v1.0/domains" `
                    -Headers $headers
                $backup.components.domains = @{
                    domains = @($domains)
                    count   = $domains.Count
                }
            }
        }
    }

    Write-Host "$(Get-Date) INFO: Backup complete — $($Components.Count) component(s) captured"

    @{
        ok     = $true
        backup = $backup
    } | ConvertTo-Json -Depth 15 -Compress
}
catch {
    Write-Host "$(Get-Date) ERROR: $_"
    @{ ok = $false; error = "$_" } | ConvertTo-Json -Compress
}
