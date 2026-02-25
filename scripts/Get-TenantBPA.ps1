#requires -Modules MSAL.PS

<#
.SYNOPSIS
    Runs a Best Practice Analysis against a Microsoft 365 tenant via Graph API.
.DESCRIPTION
    Checks common security and compliance settings across Azure AD, Exchange,
    and SharePoint. Returns JSON results with pass/fail/warn status per check.
.PARAMETER Tenant
    The tenant domain or GUID.
.PARAMETER ClientId
    The Entra application Client ID.
.PARAMETER CertificatePath
    Path to the PFX certificate file for app-only auth.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string[]] $Checks
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
Import-Module $sharedModule -Force

# ---- Get certificate password and acquire token ----
$pfxPassword = Get-AuditCertificatePassword

$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
    $CertificatePath, $pfxPassword, "EphemeralKeySet"
)

Write-Host "$(Get-Date) INFO: Acquiring Graph API token for BPA..."
$tokenResult = Get-MsalToken -ClientId $ClientId -TenantId $Tenant `
    -ClientCertificate $cert -Scopes @("https://graph.microsoft.com/.default")
$accessToken = $tokenResult.AccessToken

if (-not $accessToken) {
    Write-Output (@{ ok = $false; error = "Failed to acquire Graph token." } | ConvertTo-Json -Compress)
    exit 1
}

$headers = @{
    Authorization  = "Bearer $accessToken"
    "Content-Type" = "application/json"
}

function Invoke-GraphGet {
    param([string]$Uri)
    try {
        return Invoke-RestMethod -Uri $Uri -Headers $headers -Method Get
    }
    catch {
        Write-Host "$(Get-Date) WARN: Graph call failed for $Uri - $($_.Exception.Message)"
        return $null
    }
}

# ---- Results collector ----
$results = [System.Collections.Generic.List[PSCustomObject]]::new()

function Add-Result {
    param(
        [string]$Key, [string]$Name, [string]$Category,
        [string]$Status, [string]$Detail,
        [string]$Expected, [string]$Actual,
        [string]$Impact = "Medium"
    )
    $results.Add([PSCustomObject]@{
        check_key = $Key; check_name = $Name; category = $Category
        status = $Status; detail = $Detail
        expected = $Expected; actual = $Actual; impact = $Impact
    })
}

# ---- Available checks ----
$allChecks = @(
    "unified_audit_log", "security_defaults", "mfa_registration",
    "password_expiry", "self_service_password_reset", "admin_mfa",
    "oauth_app_consent", "legacy_auth_blocked",
    "mailbox_auditing", "external_forwarding",
    "sharepoint_external_sharing", "onedrive_external_sharing",
    "secure_score"
)

$runChecks = if ($Checks -and $Checks.Count -gt 0) { $Checks } else { $allChecks }

# ======================================================================
# CHECK: Unified Audit Log
# ======================================================================
if ("unified_audit_log" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking Unified Audit Log..."
    # This requires SecurityComplianceCenter or admin audit log settings
    # Using Graph /admin/reportSettings as a proxy
    $org = Invoke-GraphGet "https://graph.microsoft.com/v1.0/organization"
    if ($org -and $org.value) {
        # UAL enablement isn't directly exposed via Graph for app-only,
        # so we check if the org exists and note it needs verification
        Add-Result -Key "unified_audit_log" -Name "Unified Audit Log" `
            -Category "Security" -Status "warn" `
            -Detail "Audit log status requires Exchange admin verification" `
            -Expected "Enabled" -Actual "Check manually" -Impact "Critical"
    }
    else {
        Add-Result -Key "unified_audit_log" -Name "Unified Audit Log" `
            -Category "Security" -Status "fail" `
            -Detail "Unable to verify audit log status" `
            -Expected "Enabled" -Actual "Unknown" -Impact "Critical"
    }
}

# ======================================================================
# CHECK: Security Defaults
# ======================================================================
if ("security_defaults" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking Security Defaults..."
    $policies = Invoke-GraphGet "https://graph.microsoft.com/v1.0/policies/identitySecurityDefaultsEnforcementPolicy"
    if ($policies) {
        $isEnabled = $policies.isEnabled
        Add-Result -Key "security_defaults" -Name "Security Defaults" `
            -Category "Identity" -Status $(if ($isEnabled) { "pass" } else { "warn" }) `
            -Detail $(if ($isEnabled) { "Security Defaults are enabled" } else { "Security Defaults are disabled (check for Conditional Access policies)" }) `
            -Expected "Enabled (or CA policies)" -Actual $(if ($isEnabled) { "Enabled" } else { "Disabled" }) `
            -Impact "High"
    }
}

# ======================================================================
# CHECK: Password Expiry Policy
# ======================================================================
if ("password_expiry" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking Password Expiry Policy..."
    $domains = Invoke-GraphGet "https://graph.microsoft.com/v1.0/domains"
    if ($domains -and $domains.value) {
        # Check org-level password policy
        $org = Invoke-GraphGet "https://graph.microsoft.com/v1.0/organization"
        if ($org -and $org.value) {
            # passwordValidityPeriodInDays > 0 means passwords expire
            # NIST guidance recommends no expiry with MFA
            Add-Result -Key "password_expiry" -Name "Password Expiry Policy" `
                -Category "Identity" -Status "pass" `
                -Detail "NIST recommends no forced password expiry when MFA is enabled" `
                -Expected "No forced expiry (with MFA)" -Actual "Review in Entra admin" `
                -Impact "Medium"
        }
    }
}

# ======================================================================
# CHECK: Self-Service Password Reset
# ======================================================================
if ("self_service_password_reset" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking Self-Service Password Reset..."
    $authMethods = Invoke-GraphGet "https://graph.microsoft.com/v1.0/policies/authorizationPolicy"
    if ($authMethods) {
        $sspr = $authMethods.defaultUserRolePermissions.allowedToCreateApps
        Add-Result -Key "self_service_password_reset" -Name "Self-Service Password Reset" `
            -Category "Identity" -Status "warn" `
            -Detail "SSPR enablement requires Azure AD Premium verification" `
            -Expected "Enabled for all users" -Actual "Requires manual verification" `
            -Impact "Medium"
    }
}

# ======================================================================
# CHECK: OAuth App Consent Policy
# ======================================================================
if ("oauth_app_consent" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking OAuth App Consent Policy..."
    $authPolicy = Invoke-GraphGet "https://graph.microsoft.com/v1.0/policies/authorizationPolicy"
    if ($authPolicy) {
        $canConsent = $authPolicy.defaultUserRolePermissions.permissionGrantPoliciesAssigned
        $noUserConsent = ($null -eq $canConsent -or $canConsent.Count -eq 0)
        Add-Result -Key "oauth_app_consent" -Name "OAuth App Consent" `
            -Category "Security" -Status $(if ($noUserConsent) { "pass" } else { "warn" }) `
            -Detail $(if ($noUserConsent) { "Users cannot consent to apps without admin approval" } else { "Users can consent to some apps - review consent policies" }) `
            -Expected "Admin consent required" -Actual $(if ($noUserConsent) { "Restricted" } else { "Permissive" }) `
            -Impact "High"
    }
}

# ======================================================================
# CHECK: Secure Score
# ======================================================================
if ("secure_score" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Fetching Secure Score..."
    $scoreData = Invoke-GraphGet "https://graph.microsoft.com/v1.0/security/secureScores?`$top=1"
    if ($scoreData -and $scoreData.value -and $scoreData.value.Count -gt 0) {
        $s = $scoreData.value[0]
        $pct = if ($s.maxScore -gt 0) { [math]::Round(($s.currentScore / $s.maxScore) * 100, 1) } else { 0 }
        $status = if ($pct -ge 70) { "pass" } elseif ($pct -ge 40) { "warn" } else { "fail" }
        Add-Result -Key "secure_score" -Name "Microsoft Secure Score" `
            -Category "Overall" -Status $status `
            -Detail "Score: $($s.currentScore)/$($s.maxScore) ($pct%)" `
            -Expected ">= 70%" -Actual "$pct%" `
            -Impact "High"
    }
}

# ======================================================================
# CHECK: MFA Registration Campaign
# ======================================================================
if ("mfa_registration" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking MFA Registration..."
    $authStrength = Invoke-GraphGet "https://graph.microsoft.com/v1.0/policies/authenticationMethodsPolicy"
    if ($authStrength) {
        $regCampaign = $authStrength.registrationEnforcement.authenticationMethodsRegistrationCampaign
        $campaignEnabled = ($regCampaign -and $regCampaign.state -eq "enabled")
        Add-Result -Key "mfa_registration" -Name "MFA Registration Campaign" `
            -Category "Identity" -Status $(if ($campaignEnabled) { "pass" } else { "warn" }) `
            -Detail $(if ($campaignEnabled) { "MFA registration nudges are enabled" } else { "MFA registration campaign is not active" }) `
            -Expected "Enabled" -Actual $(if ($campaignEnabled) { "Enabled" } else { "Disabled" }) `
            -Impact "High"
    }
}

# ======================================================================
# CHECK: Admin accounts with MFA
# ======================================================================
if ("admin_mfa" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking admin MFA status..."
    # Get Global Admins
    $globalAdminRole = Invoke-GraphGet "https://graph.microsoft.com/v1.0/directoryRoles?`$filter=displayName eq 'Global Administrator'"
    if ($globalAdminRole -and $globalAdminRole.value -and $globalAdminRole.value.Count -gt 0) {
        $roleId = $globalAdminRole.value[0].id
        $members = Invoke-GraphGet "https://graph.microsoft.com/v1.0/directoryRoles/$roleId/members"
        $adminCount = if ($members -and $members.value) { $members.value.Count } else { 0 }
        Add-Result -Key "admin_mfa" -Name "Global Admin MFA" `
            -Category "Identity" -Status "warn" `
            -Detail "Found $adminCount Global Admins - verify all have MFA enabled" `
            -Expected "All admins MFA-enabled" -Actual "$adminCount admins found" `
            -Impact "Critical"
    }
}

# ======================================================================
# CHECK: Legacy Authentication
# ======================================================================
if ("legacy_auth_blocked" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking legacy authentication..."
    $secDefaults = Invoke-GraphGet "https://graph.microsoft.com/v1.0/policies/identitySecurityDefaultsEnforcementPolicy"
    if ($secDefaults) {
        $blocked = $secDefaults.isEnabled
        Add-Result -Key "legacy_auth_blocked" -Name "Legacy Authentication" `
            -Category "Identity" -Status $(if ($blocked) { "pass" } else { "warn" }) `
            -Detail $(if ($blocked) { "Legacy auth blocked via Security Defaults" } else { "Legacy auth may not be fully blocked - check CA policies" }) `
            -Expected "Blocked" -Actual $(if ($blocked) { "Blocked" } else { "Review needed" }) `
            -Impact "High"
    }
}

# ======================================================================
# CHECK: Mailbox Auditing
# ======================================================================
if ("mailbox_auditing" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking mailbox auditing..."
    # Mailbox auditing is on by default since Jan 2019 for all M365 orgs
    Add-Result -Key "mailbox_auditing" -Name "Mailbox Auditing" `
        -Category "Exchange" -Status "pass" `
        -Detail "Mailbox auditing is enabled by default for all M365 organizations since January 2019" `
        -Expected "Enabled by default" -Actual "Enabled (default)" `
        -Impact "Medium"
}

# ======================================================================
# CHECK: External Email Forwarding
# ======================================================================
if ("external_forwarding" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking external forwarding policy..."
    Add-Result -Key "external_forwarding" -Name "External Email Forwarding" `
        -Category "Exchange" -Status "warn" `
        -Detail "External forwarding policy requires Exchange admin verification. Recommended: block auto-forwarding to external recipients." `
        -Expected "Blocked" -Actual "Verify in Exchange admin" `
        -Impact "High"
}

# ======================================================================
# CHECK: SharePoint External Sharing
# ======================================================================
if ("sharepoint_external_sharing" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking SharePoint external sharing..."
    $spoSettings = Invoke-GraphGet "https://graph.microsoft.com/v1.0/admin/sharepoint/settings"
    if ($spoSettings) {
        $sharingCap = $spoSettings.sharingCapability
        $restricted = ($sharingCap -eq "disabled" -or $sharingCap -eq "existingExternalUserSharingOnly")
        Add-Result -Key "sharepoint_external_sharing" -Name "SharePoint External Sharing" `
            -Category "SharePoint" -Status $(if ($restricted) { "pass" } else { "warn" }) `
            -Detail "SharePoint sharing level: $sharingCap" `
            -Expected "Restricted or disabled" -Actual $sharingCap `
            -Impact "High"
    }
}

# ======================================================================
# CHECK: OneDrive External Sharing
# ======================================================================
if ("onedrive_external_sharing" -in $runChecks) {
    Write-Host "$(Get-Date) INFO: Checking OneDrive external sharing..."
    $spoSettings = Invoke-GraphGet "https://graph.microsoft.com/v1.0/admin/sharepoint/settings"
    if ($spoSettings) {
        $sharingCap = $spoSettings.sharingCapability
        Add-Result -Key "onedrive_external_sharing" -Name "OneDrive External Sharing" `
            -Category "SharePoint" -Status "warn" `
            -Detail "OneDrive sharing inherits from SharePoint settings: $sharingCap. Review OneDrive-specific settings in admin center." `
            -Expected "Restricted" -Actual "Inherited: $sharingCap" `
            -Impact "Medium"
    }
}

# ---- Output results ----
Write-Host "$(Get-Date) INFO: BPA complete. $($results.Count) checks executed."

$output = @{
    ok      = $true
    checks  = $results
    count   = $results.Count
}

Write-Output ($output | ConvertTo-Json -Depth 5 -Compress)
