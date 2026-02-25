#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Retrieves app registrations and enterprise applications from an M365 tenant.
.DESCRIPTION
    Queries Microsoft Graph for application registrations and service principals,
    returning details about permissions, credentials, and consent status.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tenant,
    [Parameter(Mandatory)][string]$ClientId,
    [Parameter(Mandatory)][string]$CertificatePath,
    [ValidateSet("appRegistrations", "enterpriseApps", "both")]
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

    # App Registrations
    if ($DataType -in "appRegistrations", "both") {
        Write-Host "$(Get-Date) INFO: Fetching app registrations for $Tenant"

        $apps = Invoke-GraphRequest -Uri "https://graph.microsoft.com/v1.0/applications" `
                                    -Headers $headers

        $appList = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($a in $apps) {
            $credExpiry = $null
            if ($a.passwordCredentials) {
                $credExpiry = ($a.passwordCredentials | Sort-Object endDateTime -Descending | Select-Object -First 1).endDateTime
            }
            if ($a.keyCredentials -and !$credExpiry) {
                $credExpiry = ($a.keyCredentials | Sort-Object endDateTime -Descending | Select-Object -First 1).endDateTime
            }

            $appList.Add([PSCustomObject]@{
                Id              = $a.id
                AppId           = $a.appId
                DisplayName     = $a.displayName
                SignInAudience  = $a.signInAudience
                CreatedDate     = $a.createdDateTime
                HasSecrets      = ($a.passwordCredentials.Count -gt 0)
                HasCerts        = ($a.keyCredentials.Count -gt 0)
                CredentialExpiry = $credExpiry
                RequiredPermissions = ($a.requiredResourceAccess | ConvertTo-Json -Depth 3 -Compress)
            })
        }

        Write-Host "$(Get-Date) INFO: Found $($appList.Count) app registration(s)"

        $output.appRegistrations = @($appList | ForEach-Object { @{
            id              = $_.Id
            appId           = $_.AppId
            displayName     = $_.DisplayName
            signInAudience  = $_.SignInAudience
            createdDate     = $_.CreatedDate
            hasSecrets      = $_.HasSecrets
            hasCerts        = $_.HasCerts
            credentialExpiry = $_.CredentialExpiry
            requiredPermissions = $_.RequiredPermissions
        }})
        $output.appRegistrationCount = $appList.Count
    }

    # Enterprise Apps (Service Principals)
    if ($DataType -in "enterpriseApps", "both") {
        Write-Host "$(Get-Date) INFO: Fetching enterprise applications for $Tenant"

        $sps = Invoke-GraphRequest `
            -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$top=999&`$select=id,appId,displayName,servicePrincipalType,accountEnabled,appOwnerOrganizationId,createdDateTime,loginUrl,tags" `
            -Headers $headers

        $spList = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($sp in $sps) {
            $spList.Add([PSCustomObject]@{
                Id              = $sp.id
                AppId           = $sp.appId
                DisplayName     = $sp.displayName
                Type            = $sp.servicePrincipalType
                Enabled         = $sp.accountEnabled
                OwnerOrg        = $sp.appOwnerOrganizationId
                CreatedDate     = $sp.createdDateTime
                LoginUrl        = $sp.loginUrl
                Tags            = ($sp.tags -join ", ")
            })
        }

        Write-Host "$(Get-Date) INFO: Found $($spList.Count) enterprise application(s)"

        $output.enterpriseApps = @($spList | ForEach-Object { @{
            id          = $_.Id
            appId       = $_.AppId
            displayName = $_.DisplayName
            type        = $_.Type
            enabled     = $_.Enabled
            ownerOrg    = $_.OwnerOrg
            createdDate = $_.CreatedDate
            loginUrl    = $_.LoginUrl
            tags        = $_.Tags
        }})
        $output.enterpriseAppCount = $spList.Count
    }

    $output | ConvertTo-Json -Depth 5 -Compress
}
catch {
    Write-Host "$(Get-Date) ERROR: $_"
    @{ ok = $false; error = "$_" } | ConvertTo-Json -Compress
}
