# Setup-App.ps1
# Creates an Entra app registration with cert auth and all permissions needed
# by both SPO-Audit-Toolkit and OfficeSentry inspectors.
# Outputs a JSON result line that portal/app.py can parse.
# Called by the Flask portal as a background job. Uses device code flow so any tenant admin
# can authenticate from any browser without adding their account to this device.

param(
    [Parameter(Mandatory)][string] $AppDisplayName,
    [Parameter(Mandatory)][string] $CerPath,
    [Parameter(Mandatory)][int]    $CertYearsValid,
    [Parameter(Mandatory)][string] $ModulesPath,      # Path to local modules folder
    [Parameter(Mandatory)][string] $AdminClientId,    # ClientId of public-client app in your management tenant
    [string] $TenantId = ""                           # Azure tenant GUID or domain (e.g. contoso.onmicrosoft.com)
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

    # Clear any cached Graph session so WAM cannot reuse a previous token.
    Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null

    # DisableLoginByWAM only takes effect when a custom ClientId is supplied —
    # the default Microsoft Graph PowerShell ClientId forces WAM regardless.
    Set-MgGraphOption -DisableLoginByWAM $true

    $connectParams = @{
        ClientId      = $AdminClientId
        Scopes        = $scopes
        UseDeviceCode = $true
        NoWelcome     = $true
    }
    if ($TenantId) { $connectParams["TenantId"] = $TenantId }

    Write-Output "ACTION REQUIRED: open https://microsoft.com/devicelogin in any browser and enter the code below."
    Connect-MgGraph @connectParams

    $ctx = Get-MgContext
    Write-Output "Authenticated as $($ctx.Account) in tenant $($ctx.TenantId)"

    # Create app registration
    $app = New-MgApplication -DisplayName $AppDisplayName
    Write-Output "App registration created: $($app.AppId)"

    # Allow replication before creating service principal
    Start-Sleep -Seconds 5

    $sp = New-MgServicePrincipal -AppId $app.AppId
    Write-Output "Service principal created: $($sp.Id)"

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
        $spCount = 0
        $resourceSp = Get-MgServicePrincipal -Filter "appId eq '$ResourceAppId'" `
            -ConsistencyLevel eventual -CountVariable spCount | Select-Object -First 1
        Write-Debug "Service principal query returned $spCount result(s) for $ResourceAppId"
        if (-not $resourceSp) {
            Write-Output "WARNING: service principal not found for $ResourceAppId — skipping $RoleValue"
            return
        }
        $role = $resourceSp.AppRoles |
            Where-Object { $_.Value -eq $RoleValue -and $_.AllowedMemberTypes -contains "Application" } |
            Select-Object -First 1
        if (-not $role) {
            Write-Output "WARNING: role '$RoleValue' not found on $ResourceAppId — skipping"
            return
        }
        try {
            New-MgServicePrincipalAppRoleAssignment `
                -ServicePrincipalId $sp.Id `
                -PrincipalId        $sp.Id `
                -ResourceId         $resourceSp.Id `
                -AppRoleId          $role.Id | Out-Null
            Write-Output "Granted: $RoleValue"
        } catch {
            Write-Output "WARNING: could not assign $RoleValue — $($_.Exception.Message)"
        }
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
