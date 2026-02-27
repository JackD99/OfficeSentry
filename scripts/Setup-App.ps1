# Setup-App.ps1
# Creates an Entra app registration with cert auth and all permissions needed
# by both Office Sentry audits and security inspectors.
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

    Import-Module MSAL.PS                       -ErrorAction Stop
    Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
    Import-Module Microsoft.Graph.Applications   -ErrorAction Stop

    # Fully-qualified scopes required by MSAL.PS
    $scopes = @(
        "https://graph.microsoft.com/Application.ReadWrite.All",
        "https://graph.microsoft.com/Directory.Read.All",
        "https://graph.microsoft.com/AppRoleAssignment.ReadWrite.All",
        "https://graph.microsoft.com/RoleManagement.ReadWrite.Directory"
    )

    # Clear any cached Graph session.
    Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null

    # Acquire token via MSAL.PS device code flow.
    # This bypasses the MS Graph SDK's DeviceCodeCredential which has a known
    # null-reference bug when used with a custom ClientId.
    $msalParams = @{
        ClientId   = $AdminClientId
        Scopes     = $scopes
        DeviceCode = $true
    }
    if ($TenantId) { $msalParams["TenantId"] = $TenantId }

    Write-Output "ACTION REQUIRED: open https://microsoft.com/devicelogin in any browser and enter the code below."
    $tokenResult = Get-MsalToken @msalParams

    Write-Output "Authenticated as $($tokenResult.Account.Username) in tenant $($tokenResult.TenantId)"

    # Connect to Microsoft Graph using the access token (avoids DeviceCodeCredential issues)
    $secureToken = ConvertTo-SecureString $tokenResult.AccessToken -AsPlainText -Force
    Connect-MgGraph -AccessToken $secureToken -NoWelcome

    # Create app registration
    $app = New-MgApplication -DisplayName $AppDisplayName
    Write-Output "App registration created: $($app.AppId)"

    # Create service principal with retry (Entra ID replication delay)
    $sp = $null
    $maxAttempts = 6
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        try {
            $sp = New-MgServicePrincipal -AppId $app.AppId
            break
        } catch {
            if ($attempt -eq $maxAttempts) { throw }
            $wait = $attempt * 5
            Write-Output "Waiting for app to replicate... (attempt $attempt/$maxAttempts, retrying in ${wait}s)"
            Start-Sleep -Seconds $wait
        }
    }
    Write-Output "Service principal created: $($sp.Id)"

    # Upload certificate — read actual validity dates from the cert so they match exactly
    $cerBytes = [IO.File]::ReadAllBytes($CerPath)
    $cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($CerPath)
    $keyCredential = @{
        Type          = "AsymmetricX509Cert"
        Usage         = "Verify"
        Key           = $cerBytes
        DisplayName   = "Office Sentry Cert"
        StartDateTime = $cert.NotBefore.ToUniversalTime()
        EndDateTime   = $cert.NotAfter.ToUniversalTime()
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

    # Graph permissions (superset covering all Office Sentry audits + security inspectors)
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
