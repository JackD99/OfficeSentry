# Get-SharePointTenantPermissions.ps1
# Description: Get permissions for a given user across all SharePoint Online site collections and export to CSV.
# Requires: PnP.PowerShell, MSAL.PS

#requires -Modules PnP.PowerShell, MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [string] $TenantName,  # e.g. contoso  (used as contoso-admin.sharepoint.com and contoso.onmicrosoft.com)

    [Parameter(Mandatory = $true)]
    [string] $UserEmail,

    [Parameter(Mandatory = $true)]
    [string] $CSVPath,

    [Parameter(Mandatory = $true)]
    [string] $ClientId,

    [Parameter(Mandatory = $true)]
    [string] $CertificatePath, # PFX path

    [Parameter(Mandatory = $true)]
    [securestring] $CertificatePassword, # PFX password

    [Parameter(Mandatory = $false)]
    [switch] $Append = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Connect-TenantSite {
    <#
    .SYNOPSIS
    Connects to a SharePoint Online site using certificate-based authentication via PnP PowerShell.
    #>
    param (
        [Parameter(Mandatory = $true)]
        [string] $SiteUrl
    )

    $connectionAttempts = 3
    for ($i = 0; $i -lt $connectionAttempts; $i++) {
        try {
            Connect-PnPOnline `
                -Url $SiteUrl `
                -ClientId $ClientId `
                -Tenant "$TenantName.onmicrosoft.com" `
                -CertificatePath $CertificatePath `
                -CertificatePassword $CertificatePassword

            return
        }
        catch {
            if ($i -eq $connectionAttempts - 1) {
                throw
            }
            Start-Sleep -Seconds 2
        }
    }
}

function Get-GraphToken {
    <#
    .SYNOPSIS
    Gets a bearer token for Microsoft Graph using MSAL.PS + certificate auth.
    #>
    try {
        # Load PFX into X509Certificate2 (required by MSAL.PS Get-MsalToken -ClientCertificate)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($CertificatePassword)
        )

        $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet `
               -bor [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable

        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CertificatePath, $plain, $flags)

        # Use .default so Graph uses the app's *application permissions*
        return Get-MsalToken -TenantId "$TenantName.onmicrosoft.com" -ClientId $ClientId -ClientCertificate $cert -Scopes "https://graph.microsoft.com/.default"
    }
    catch {
        throw
    }
}

function Get-UserGroupMembership {
    <#
    .SYNOPSIS
    Gets Entra group membership for a given user (memberOf).
    Returns objects with GroupName and GroupId.
    #>
    param (
        [Parameter(Mandatory = $true)]
        [string] $UserEmail
    )

    $accessToken = (Get-GraphToken).AccessToken
    $encodedUserEmail = [System.Web.HttpUtility]::UrlEncode($UserEmail)

    $url = "https://graph.microsoft.com/v1.0/users/$encodedUserEmail/memberOf?$select=id,displayName"

    $all = @()
    while ($url) {
        $resp = Invoke-RestMethod -Uri $url -Method GET -Headers @{ Authorization = "Bearer $accessToken" }
        $all += @($resp.value | ForEach-Object {
            [PSCustomObject]@{
                GroupName = $_.displayName
                GroupId   = $_.id
            }
        })
        $url = $resp.'@odata.nextLink'
    }

    return $all
}

function New-CsvFile {
    param ([Parameter(Mandatory = $true)][string] $Path)

    if (Test-Path $Path) { Remove-Item $Path -Force }

    # write headers only
    [PSCustomObject]@{
        UserPrincipalName = $null
        SiteUrl           = $null
        SiteAdmin         = $null
        GroupName         = $null
        PermissionLevel   = $null
        ListName          = $null
        ListPermission    = $null
    } | Export-Csv -Path $Path -NoTypeInformation

    # Remove the empty data row, keep header
    $lines = Get-Content $Path
    Set-Content -Path $Path -Value @($lines[0])
}

function Test-UserIsSiteCollectionAdmin {
    param (
        [Parameter(Mandatory = $true)][string] $UserEmail,
        [Parameter(Mandatory = $false)][array]  $GraphGroups
    )

    $siteAdmins = Get-PnPSiteCollectionAdmin
    foreach ($siteAdmin in $siteAdmins) {
        $login = $siteAdmin.LoginName
        $idPart = ($login -split '\|')[-1]

        if ($UserEmail -eq $idPart) { return $true }

        if ($GraphGroups) {
            if ($GraphGroups.GroupId -contains $idPart) { return $true }
        }
    }
    return $false
}

function Get-UserSharePointGroups {
    param (
        [Parameter(Mandatory = $true)][string] $UserEmail,
        [Parameter(Mandatory = $false)][array]  $GraphGroups
    )

    $siteGroups = Get-PnPGroup
    $membership = @()

    foreach ($siteGroup in $siteGroups) {
        $groupMembers = Get-PnPGroupMember -Identity $siteGroup.Title

        foreach ($groupMember in $groupMembers) {
            $idPart = (($groupMember.LoginName -split '\|')[-1])

            $isDirect = ($UserEmail -eq $idPart)
            $isViaEntraGroup = ($GraphGroups -and ($GraphGroups.GroupId -contains $idPart))

            if ($isDirect -or $isViaEntraGroup) {
                $levels = Get-PnPGroupPermissions -Identity $siteGroup
                $levelString = ($levels | ForEach-Object { $_.Name }) -join " | "
                if ([string]::IsNullOrWhiteSpace($levelString)) { $levelString = "No Permissions" }

                $membership += [PSCustomObject]@{
                    GroupName       = $siteGroup.Title
                    PermissionLevel = $levelString
                }
                break
            }
        }
    }

    return $membership
}

function Get-UniqueListPermissions {
    param (
        [Parameter(Mandatory = $true)][string] $UserEmail,
        [Parameter(Mandatory = $false)][array]  $GraphGroups
    )

    $ctx = Get-PnPContext
    $web = $ctx.Web
    $ctx.Load($web)
    $ctx.ExecuteQuery()

    $lists = $web.Lists
    $ctx.Load($lists)
    $ctx.ExecuteQuery()

    # Exclude built-in lists (fixed syntax)
    $excludedLists = @(
        "App Packages","appdata","appfiles","Apps in Testing","Cache Profiles","Composed Looks",
        "Content and Structure Reports","Content type publishing error log","Converted Forms",
        "Device Channels","Form Templates","fpdatasources","Get started with Apps for Office and SharePoint",
        "List Template Gallery","Long Running Operation Status","Maintenance Log Library","Style Library",
        "Master Docs","Master Page Gallery","MicroFeed","NintexFormXml","Quick Deploy Items",
        "Relationships List","Reusable Content","Search Config List","Solution Gallery",
        "Site Collection Images","Suggested Content Browser Locations","TaxonomyHiddenList",
        "User Information List","Web Part Gallery","wfpub","wfsvc","Workflow History","Workflow Tasks",
        "Preservation Hold Library","SharePointHomeCacheList"
    )

    $siteListPermissions = @()

    foreach ($list in $lists) {
        $ctx.Load($list)
        $ctx.ExecuteQuery()

        if ($excludedLists -contains $list.Title) { continue }

        $list.Retrieve("HasUniqueRoleAssignments")
        $ctx.ExecuteQuery()

        if (-not $list.HasUniqueRoleAssignments) { continue }

        $assignments = $list.RoleAssignments
        $ctx.Load($assignments)
        $ctx.ExecuteQuery()

        foreach ($ra in $assignments) {
            $ctx.Load($ra.Member)
            $ctx.Load($ra.RoleDefinitionBindings)
            $ctx.ExecuteQuery()

            $idPart = (($ra.Member.LoginName -split '\|')[-1])

            $isDirect = ($UserEmail -eq $idPart)
            $isViaEntraGroup = ($GraphGroups -and ($GraphGroups.GroupId -contains $idPart))

            if ($isDirect -or $isViaEntraGroup) {
                $siteListPermissions += [PSCustomObject]@{
                    Name            = $list.Title
                    PermissionLevel = ($ra.RoleDefinitionBindings | ForEach-Object { $_.Name }) -join "; "
                }
            }
        }
    }

    return $siteListPermissions
}

Set-Location $PSScriptRoot

Write-Host "$(Get-Date) INFO: Connecting to tenant admin site..."
Connect-TenantSite -SiteUrl "https://$TenantName-admin.sharepoint.com"

Write-Host "$(Get-Date) INFO: Getting all site collections..."
$siteCollections = Get-PnPTenantSite
Write-Host "$(Get-Date) INFO: `tFound $($siteCollections.Count) site collections."
Disconnect-PnPOnline

Write-Host "$(Get-Date) INFO: Getting Entra group membership for $UserEmail..."
$userGroupMembership = Get-UserGroupMembership -UserEmail $UserEmail
Write-Host "$(Get-Date) INFO: `tFound $($userGroupMembership.Count) groups."

if (-not $Append) {
    New-CsvFile -Path $CSVPath
}

$siteCounter = 1
foreach ($siteCollection in $siteCollections) {
    Write-Host "$(Get-Date) INFO: Connecting to $($siteCollection.Url)`t ($siteCounter of $($siteCollections.Count))..."
    $siteCounter++

    Connect-TenantSite -SiteUrl $siteCollection.Url

    if (Test-UserIsSiteCollectionAdmin -UserEmail $UserEmail -GraphGroups $userGroupMembership) {
        Write-Host "$(Get-Date) INFO: `t$UserEmail is a site collection admin for $($siteCollection.Url)."
        [PSCustomObject]@{
            UserPrincipalName = $UserEmail
            SiteUrl           = $siteCollection.Url
            SiteAdmin         = $true
            GroupName         = $null
            PermissionLevel   = $null
            ListName          = $null
            ListPermission    = $null
        } | Export-Csv -Path $CSVPath -Append -NoTypeInformation

        Disconnect-PnPOnline
        continue
    }

    $spGroups = Get-UserSharePointGroups -UserEmail $UserEmail -GraphGroups $userGroupMembership
    foreach ($group in $spGroups) {
        Write-Host "$(Get-Date) INFO: `t$UserEmail is in $($group.GroupName) with $($group.PermissionLevel)."
        [PSCustomObject]@{
            UserPrincipalName = $UserEmail
            SiteUrl           = $siteCollection.Url
            SiteAdmin         = $false
            GroupName         = $group.GroupName
            PermissionLevel   = $group.PermissionLevel
            ListName          = $null
            ListPermission    = $null
        } | Export-Csv -Path $CSVPath -Append -NoTypeInformation
    }

    $listPermissions = Get-UniqueListPermissions -UserEmail $UserEmail -GraphGroups $userGroupMembership
    foreach ($lp in $listPermissions) {
        Write-Host "$(Get-Date) INFO: `t$UserEmail has $($lp.PermissionLevel) on list '$($lp.Name)'."
        [PSCustomObject]@{
            UserPrincipalName = $UserEmail
            SiteUrl           = $siteCollection.Url
            SiteAdmin         = $false
            GroupName         = $null
            PermissionLevel   = $null
            ListName          = $lp.Name
            ListPermission    = $lp.PermissionLevel
        } | Export-Csv -Path $CSVPath -Append -NoTypeInformation
    }

    Disconnect-PnPOnline
}
