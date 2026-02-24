# Setup-App.ps1
# Creates an Entra app registration with cert auth and all permissions needed
# by both SPO-Audit-Toolkit and OfficeSentry inspectors.
# Outputs a JSON result line that portal/app.py can parse.
# Called non-interactively by the Flask portal (Graph login is interactive via browser popup).

param(
    [Parameter(Mandatory)][string] $AppDisplayName,
    [Parameter(Mandatory)][string] $CerPath,
    [Parameter(Mandatory)][int]    $CertYearsValid,
    [Parameter(Mandatory)][string] $ModulesPath    # Path to local modules folder
)

$ErrorActionPreference = "Stop"

try {
    $env:PSModulePath = "$ModulesPath;$($env:PSModulePath)"

    Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
    Import-Module Microsoft.Graph.Applications    -ErrorAction Stop

    $scopes = @(
        "Application.ReadWrite.All",
        "Directory.Read.All",
        "AppRoleAssignment.ReadWrite.All",
        "RoleManagement.ReadWrite.Directory"
    )

    Connect-MgGraph -Scopes $scopes | Out-Null

    # Create app + service principal
    $app = New-MgApplication -DisplayName $AppDisplayName
    $sp  = New-MgServicePrincipal -AppId $app.AppId

    # Upload certificate
    $now      = Get-Date
    $cerBytes = [IO.File]::ReadAllBytes($CerPath)
    $keyCredential = @{
        Type          = "AsymmetricX509Cert"
        Usage         = "Verify"
        Key           = $cerBytes
        DisplayName   = "M365 Audit Portal Cert"
        StartDateTime = $now
        EndDateTime   = $now.AddYears($CertYearsValid)
    }
    Update-MgApplication -ApplicationId $app.Id -KeyCredentials @($keyCredential)

    # Helper to assign an app role
    function Grant-Role {
        param([string]$ResourceAppId, [string]$RoleValue)
        $resourceSp = Get-MgServicePrincipal -Filter "appId eq '$ResourceAppId'" -ConsistencyLevel eventual
        if (-not $resourceSp) { return }
        $role = $resourceSp.AppRoles |
            Where-Object { $_.Value -eq $RoleValue -and $_.AllowedMemberTypes -contains "Application" } |
            Select-Object -First 1
        if (-not $role) { return }
        New-MgServicePrincipalAppRoleAssignment `
            -ServicePrincipalId $sp.Id `
            -PrincipalId        $sp.Id `
            -ResourceId         $resourceSp.Id `
            -AppRoleId          $role.Id | Out-Null
    }

    $spo   = "00000003-0000-0ff1-ce00-000000000000"  # SharePoint Online
    $graph = "00000003-0000-0000-c000-000000000000"  # Microsoft Graph

    # SPO permissions (audit-only)
    Grant-Role -ResourceAppId $spo -RoleValue "Sites.Read.All"

    # Graph permissions (superset covering both SPO toolkit + all OfficeSentry inspectors)
    $graphRoles = @(
        "AuditLog.Read.All",
        "Directory.Read.All",
        "Policy.Read.All",
        "Organization.Read.All",
        "SecurityEvents.Read.All",
        "User.Read.All",
        "UserAuthenticationMethod.Read.All",
        "MailboxSettings.Read",
        "DeviceManagementManagedDevices.Read.All",
        "DeviceManagementApps.Read.All",
        "DeviceManagementServiceConfig.Read.All",
        "DeviceManagementConfiguration.Read.All",
        "SharePointTenantSettings.Read.All",
        "Reports.Read.All",
        "IdentityProvider.Read.All",
        "RoleManagement.Read.Directory"
    )
    foreach ($role in $graphRoles) {
        Grant-Role -ResourceAppId $graph -RoleValue $role
    }

    $consentUrl = "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationMenuBlade/~/Overview/appId/$($app.AppId)"

    @{
        ok           = $true
        clientId     = $app.AppId
        appId        = $app.Id
        displayName  = $app.DisplayName
        consentUrl   = $consentUrl
    } | ConvertTo-Json -Compress
}
catch {
    @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
