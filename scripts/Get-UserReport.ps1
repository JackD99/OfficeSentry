# Get-UserReport.ps1
# Generates user reports via Microsoft Graph API.
# Supports: license assignments, MFA status, user info, group membership.
# Designed for MSP helpdesk staff needing quick visibility across tenants.
#
# Required API Permissions (Application):
#   - User.Read.All (user info, license details)
#   - Directory.Read.All (group membership)
#   - UserAuthenticationMethod.Read.All (MFA status)
#   - Reports.Read.All (authentication methods report)
#
# Usage:
#   .\Get-UserReport.ps1 -ReportType licenses -Tenant contoso.onmicrosoft.com ...
#   .\Get-UserReport.ps1 -ReportType mfa_status -Tenant contoso.onmicrosoft.com ...
#   .\Get-UserReport.ps1 -ReportType user_info -TargetUser user@contoso.com ...

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][ValidateSet("licenses","mfa_status","user_info","group_membership")]
    [string] $ReportType,
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [string] $TargetUser = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Acquiring Graph API token for tenant $Tenant..."
$token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId `
    -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword
$accessToken = $token.AccessToken

Write-Host "$(Get-Date) INFO: Generating report: $ReportType..."

switch ($ReportType) {
    "licenses" {
        # Get all users with license details
        $select = "displayName,userPrincipalName,accountEnabled,assignedLicenses,assignedPlans"
        if ($TargetUser) {
            $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser`?`$select=$select"
            $users = @(Invoke-GraphRequest -Uri $uri -AccessToken $accessToken)
        }
        else {
            $uri = "https://graph.microsoft.com/v1.0/users?`$select=$select&`$top=999"
            $users = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken
        }

        # Get SKU mapping for friendly names
        $skuUri = "https://graph.microsoft.com/v1.0/subscribedSkus"
        $skus = Invoke-GraphRequest -Uri $skuUri -AccessToken $accessToken
        $skuMap = @{}
        foreach ($sku in $skus) {
            $friendly = $sku.skuPartNumber
            $skuMap[$sku.skuId] = $friendly
        }

        $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
        $counter = 0
        foreach ($user in $users) {
            $counter++
            Write-Progress -Activity "Processing license data" -Status "$counter users" -PercentComplete (($counter / [math]::Max($users.Count, 1)) * 100)

            $licenses = @()
            if ($user.assignedLicenses) {
                $licenses = $user.assignedLicenses | ForEach-Object {
                    if ($skuMap.ContainsKey($_.skuId)) { $skuMap[$_.skuId] } else { $_.skuId }
                }
            }

            $rows.Add([PSCustomObject]@{
                DisplayName       = $user.displayName
                UserPrincipalName = $user.userPrincipalName
                AccountEnabled    = $user.accountEnabled
                LicenseCount      = $licenses.Count
                Licenses          = ($licenses -join "; ")
            })
        }
        Write-Progress -Activity "Processing license data" -Completed

        Write-Host "$(Get-Date) INFO: Processed $($rows.Count) users with license data."
        $sheets = @(@{ Name = "Licenses"; Data = $rows.ToArray() })
        $target = if ($TargetUser) { "_$($TargetUser -replace '@','_')" } else { "" }
        Export-AuditData -OutputFolder $OutputFolder -FilePrefix "LicenseReport${target}" -Sheets $sheets
    }

    "mfa_status" {
        # Use authentication methods registration details
        $uri = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails?`$top=999"
        if ($TargetUser) {
            $uri = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails?`$filter=userPrincipalName eq '$TargetUser'"
        }

        $results = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken

        $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($item in $results) {
            $rows.Add([PSCustomObject]@{
                DisplayName          = $item.userDisplayName
                UserPrincipalName    = $item.userPrincipalName
                IsMfaRegistered      = $item.isMfaRegistered
                IsMfaCapable         = $item.isMfaCapable
                IsPasswordlessCapable = $item.isPasswordlessCapable
                IsSsprRegistered     = $item.isSsprRegistered
                IsSsprEnabled        = $item.isSsprEnabled
                IsSsprCapable        = $item.isSsprCapable
                MethodsRegistered    = ($item.methodsRegistered -join "; ")
                DefaultMfaMethod     = $item.defaultMfaMethod
            })
        }

        Write-Host "$(Get-Date) INFO: Processed $($rows.Count) users with MFA data."
        $sheets = @(@{ Name = "MFA_Status"; Data = $rows.ToArray() })
        $target = if ($TargetUser) { "_$($TargetUser -replace '@','_')" } else { "" }
        Export-AuditData -OutputFolder $OutputFolder -FilePrefix "MFA_Report${target}" -Sheets $sheets
    }

    "user_info" {
        if (-not $TargetUser) {
            throw "TargetUser is required for user_info report."
        }

        $select = "id,displayName,userPrincipalName,mail,accountEnabled,createdDateTime," +
                  "jobTitle,department,companyName,officeLocation,mobilePhone,businessPhones," +
                  "city,state,country,usageLocation,lastPasswordChangeDateTime," +
                  "onPremisesSyncEnabled,userType,signInActivity"
        $uri = "https://graph.microsoft.com/v1.0/users/${TargetUser}?`$select=$select"

        $headers = @{ Authorization = "Bearer $accessToken" }
        $user = Invoke-RestMethod -Uri $uri -Method GET -Headers $headers

        # Get license details
        $licUri = "https://graph.microsoft.com/v1.0/users/$TargetUser/licenseDetails"
        $licData = Invoke-GraphRequest -Uri $licUri -AccessToken $accessToken
        $licenseNames = ($licData | ForEach-Object { $_.skuPartNumber }) -join "; "

        # Get group memberships
        $grpUri = "https://graph.microsoft.com/v1.0/users/$TargetUser/memberOf?`$top=999"
        $groups = Invoke-GraphRequest -Uri $grpUri -AccessToken $accessToken
        $groupNames = ($groups | Where-Object { $_.'@odata.type' -eq '#microsoft.graph.group' } |
                       ForEach-Object { $_.displayName }) -join "; "

        $lastSignIn = ""
        if ($user.signInActivity -and $user.signInActivity.lastSignInDateTime) {
            $lastSignIn = $user.signInActivity.lastSignInDateTime
        }

        $row = [PSCustomObject]@{
            DisplayName        = $user.displayName
            UserPrincipalName  = $user.userPrincipalName
            Email              = $user.mail
            AccountEnabled     = $user.accountEnabled
            UserType           = $user.userType
            JobTitle           = $user.jobTitle
            Department         = $user.department
            Company            = $user.companyName
            Office             = $user.officeLocation
            MobilePhone        = $user.mobilePhone
            BusinessPhones     = ($user.businessPhones -join "; ")
            City               = $user.city
            State              = $user.state
            Country            = $user.country
            UsageLocation      = $user.usageLocation
            Created            = $user.createdDateTime
            LastPasswordChange = $user.lastPasswordChangeDateTime
            LastSignIn         = $lastSignIn
            OnPremSync         = $user.onPremisesSyncEnabled
            Licenses           = $licenseNames
            GroupMemberships   = $groupNames
        }

        Write-Host "$(Get-Date) INFO: User details retrieved for $TargetUser."
        Write-Host "  Display Name: $($row.DisplayName)"
        Write-Host "  Account Enabled: $($row.AccountEnabled)"
        Write-Host "  Licenses: $($row.Licenses)"
        Write-Host "  Last Sign-In: $($row.LastSignIn)"

        $sheets = @(@{ Name = "UserInfo"; Data = @($row) })
        $target = $TargetUser -replace '@','_'
        Export-AuditData -OutputFolder $OutputFolder -FilePrefix "UserInfo_${target}" -Sheets $sheets
    }

    "group_membership" {
        if (-not $TargetUser) {
            throw "TargetUser is required for group_membership report."
        }

        $grpUri = "https://graph.microsoft.com/v1.0/users/$TargetUser/memberOf?`$top=999"
        $memberships = Invoke-GraphRequest -Uri $grpUri -AccessToken $accessToken

        $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($item in $memberships) {
            $type = switch ($item.'@odata.type') {
                '#microsoft.graph.group' { "Group" }
                '#microsoft.graph.directoryRole' { "Directory Role" }
                '#microsoft.graph.administrativeUnit' { "Administrative Unit" }
                default { $item.'@odata.type' -replace '#microsoft.graph.', '' }
            }

            $rows.Add([PSCustomObject]@{
                DisplayName  = $item.displayName
                Type         = $type
                Description  = $item.description
                Mail         = $item.mail
                MailEnabled  = $item.mailEnabled
                GroupTypes   = ($item.groupTypes -join "; ")
            })
        }

        Write-Host "$(Get-Date) INFO: Found $($rows.Count) group memberships for $TargetUser."
        $sheets = @(@{ Name = "GroupMembership"; Data = $rows.ToArray() })
        $target = $TargetUser -replace '@','_'
        Export-AuditData -OutputFolder $OutputFolder -FilePrefix "Groups_${target}" -Sheets $sheets
    }
}

Write-Host "$(Get-Date) INFO: Report '$ReportType' complete."
