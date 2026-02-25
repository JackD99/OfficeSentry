#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Retrieves custom domains registered in an M365 tenant.
.DESCRIPTION
    Queries Microsoft Graph for the tenant's verified and unverified domains,
    returning domain name, authentication type, verification status, and capabilities.
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

    Write-Host "$(Get-Date) INFO: Fetching domains for tenant $Tenant"

    $domains = Invoke-GraphRequest -Uri "https://graph.microsoft.com/v1.0/domains" `
                                   -Headers $headers

    $results = [System.Collections.Generic.List[PSCustomObject]]::new()

    foreach ($d in $domains) {
        $results.Add([PSCustomObject]@{
            DomainName         = $d.id
            AuthenticationType = $d.authenticationType
            IsVerified         = $d.isVerified
            IsDefault          = $d.isDefault
            IsInitial          = $d.isInitial
            IsRoot             = $d.isRoot
            IsAdminManaged     = $d.isAdminManaged
            Capabilities       = ($d.supportedServices -join ", ")
            AvailabilityStatus = $d.availabilityStatus
        })
    }

    Write-Host "$(Get-Date) INFO: Found $($results.Count) domain(s)"

    $output = @{
        ok      = $true
        domains = @($results | ForEach-Object { @{
            name               = $_.DomainName
            authenticationType = $_.AuthenticationType
            isVerified         = $_.IsVerified
            isDefault          = $_.IsDefault
            isInitial          = $_.IsInitial
            isRoot             = $_.IsRoot
            isAdminManaged     = $_.IsAdminManaged
            capabilities       = $_.Capabilities
        }})
        count   = $results.Count
    }
    $output | ConvertTo-Json -Depth 5 -Compress
}
catch {
    Write-Host "$(Get-Date) ERROR: $_"
    @{ ok = $false; error = "$_" } | ConvertTo-Json -Compress
}
