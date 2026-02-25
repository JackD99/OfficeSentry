#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Retrieves authentication method policies and registration details for an M365 tenant.
.DESCRIPTION
    Queries Microsoft Graph for authentication method policies including MFA status,
    passwordless methods, FIDO2, Microsoft Authenticator, SMS, and email OTP.
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

    Write-Host "$(Get-Date) INFO: Fetching authentication method policies for $Tenant"

    # Get auth method policy
    $policyUri = "https://graph.microsoft.com/v1.0/policies/authenticationMethodsPolicy"
    $policy = Invoke-RestMethod -Uri $policyUri -Headers $headers -Method Get

    $methods = [System.Collections.Generic.List[PSCustomObject]]::new()

    foreach ($config in $policy.authenticationMethodConfigurations) {
        $methods.Add([PSCustomObject]@{
            Method  = $config.id
            State   = $config.state
            Details = ($config | ConvertTo-Json -Depth 3 -Compress)
        })
    }

    Write-Host "$(Get-Date) INFO: Found $($methods.Count) authentication method configuration(s)"

    # Get registration details summary
    $regUri = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/usersRegisteredByMethod"
    $regDetails = $null
    try {
        $regDetails = Invoke-RestMethod -Uri $regUri -Headers $headers -Method Get
    }
    catch {
        Write-Host "$(Get-Date) WARN: Could not fetch registration details: $_"
    }

    $output = @{
        ok                  = $true
        policyDescription   = $policy.description
        registrationEnforcement = $policy.registrationEnforcement
        methods             = @($methods | ForEach-Object { @{
            method  = $_.Method
            state   = $_.State
            details = $_.Details
        }})
        registrationDetails = if ($regDetails) { $regDetails } else { $null }
    }
    $output | ConvertTo-Json -Depth 5 -Compress
}
catch {
    Write-Host "$(Get-Date) ERROR: $_"
    @{ ok = $false; error = "$_" } | ConvertTo-Json -Compress
}
