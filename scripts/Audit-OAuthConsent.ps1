# Audit-OAuthConsent.ps1
# Reviews OAuth app consent grants across the tenant.  Detects illicit consent
# grants — a major attack vector where attackers trick users into granting
# permissions to malicious apps.
# Requires: MSAL.PS, ImportExcel (optional)

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [string] $StartDate = "",
    [string] $EndDate   = ""
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
$headers     = @{ Authorization = "Bearer $accessToken" }

# ---- 1. OAuth2 permission grants (delegated permissions) ----
Write-Host "$(Get-Date) INFO: Fetching OAuth2 permission grants..."
$grants = Invoke-GraphRequest `
    -Uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?`$top=999" `
    -AccessToken $accessToken

$grantRows = [System.Collections.Generic.List[PSCustomObject]]::new()

foreach ($g in $grants) {
    # Resolve service principal name
    $spName = ""
    try {
        $sp = Invoke-RestMethod `
            -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($g.clientId)?`$select=displayName,appId" `
            -Headers $headers -Method GET
        $spName = $sp.displayName
    } catch {}

    # Resolve user
    $userName = "(All users)"
    if ($g.consentType -eq "Principal" -and $g.principalId) {
        try {
            $user = Invoke-RestMethod `
                -Uri "https://graph.microsoft.com/v1.0/users/$($g.principalId)?`$select=userPrincipalName" `
                -Headers $headers -Method GET
            $userName = $user.userPrincipalName
        } catch { $userName = $g.principalId }
    }

    # High-risk scopes
    $scopes = $g.scope -split ' '
    $riskyScopes = $scopes | Where-Object {
        $_ -match 'Mail\.(Read|Send|ReadWrite)|Files\.ReadWrite|User\.ReadWrite|Directory\.ReadWrite|full_access'
    }

    $grantRows.Add([PSCustomObject]@{
        AppName      = $spName
        AppClientId  = $g.clientId
        ConsentType  = $g.consentType
        ConsentedBy  = $userName
        Scopes       = $g.scope
        RiskyScopes  = ($riskyScopes -join " ")
        ResourceId   = $g.resourceId
        IsRisky      = ($riskyScopes.Count -gt 0)
    })
}

Write-Host "$(Get-Date) INFO: Found $($grantRows.Count) OAuth permission grants."

# ---- 2. Service principals with application permissions (app roles) ----
Write-Host "$(Get-Date) INFO: Fetching service principals with app role assignments..."

$spList = Invoke-GraphRequest `
    -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=servicePrincipalType eq 'Application'&`$select=id,displayName,appId,createdDateTime&`$top=999" `
    -AccessToken $accessToken

$appRoleRows = [System.Collections.Generic.List[PSCustomObject]]::new()
$counter     = 0
$total       = $spList.Count

foreach ($sp in $spList) {
    $counter++
    if ($counter % 50 -eq 0) {
        Write-Progress -Activity "Checking app role assignments" `
                       -Status "$counter of $total" `
                       -PercentComplete ([math]::Round(($counter / $total) * 100))
    }

    try {
        $assignments = Invoke-GraphRequest `
            -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($sp.id)/appRoleAssignments?`$top=999" `
            -AccessToken $accessToken
    } catch { continue }

    foreach ($a in $assignments) {
        $appRoleRows.Add([PSCustomObject]@{
            AppName          = $sp.displayName
            AppId            = $sp.appId
            AppCreated       = $sp.createdDateTime
            ResourceName     = $a.resourceDisplayName
            AppRoleId        = $a.appRoleId
            AssignedDateTime = $a.createdDateTime
        })
    }
}

Write-Progress -Activity "Checking app role assignments" -Completed

Write-Host "$(Get-Date) INFO: Found $($appRoleRows.Count) app role assignments."

# ---- 3. Recently created service principals (last 30 days) ----
$recentDate = (Get-Date).AddDays(-30).ToString("yyyy-MM-ddTHH:mm:ssZ")
$recentSPs  = $spList | Where-Object { $_.createdDateTime -gt $recentDate }

$recentRows = $recentSPs | Select-Object `
    @{N='AppName';E={$_.displayName}},
    @{N='AppId';E={$_.appId}},
    @{N='Created';E={$_.createdDateTime}}

# Export
$sheets = @(
    @{ Name = "DelegatedGrants";  Data = $grantRows.ToArray() }
    @{ Name = "AppPermissions";   Data = $appRoleRows.ToArray() }
    @{ Name = "RecentApps";       Data = @($recentRows) }
)

Export-AuditData -OutputFolder $OutputFolder -FilePrefix "OAuthConsent" -Sheets $sheets

$riskyCount = ($grantRows | Where-Object { $_.IsRisky }).Count
Write-Host "$(Get-Date) INFO: Done. $($grantRows.Count) delegated grants ($riskyCount risky), $($appRoleRows.Count) app permissions, $(@($recentRows).Count) recent apps."
