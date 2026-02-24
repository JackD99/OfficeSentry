# Get-SharePointTenantPermissions.ps1
# Audits a single user's permissions across all SharePoint Online site collections.
# Exports results to CSV and (optionally) XLSX.
# Requires: PnP.PowerShell, MSAL.PS, ImportExcel (optional, for XLSX)

#requires -Modules PnP.PowerShell, MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [string] $Tenant,           # TenantId (GUID) or domain (e.g. contoso.onmicrosoft.com)

    [Parameter(Mandatory = $true)]
    [string] $UserEmail,        # UPN of the user to audit

    [Parameter(Mandatory = $true)]
    [string] $CSVPath,          # Full path for CSV output

    [Parameter(Mandatory = $false)]
    [string] $XlsxPath,         # Full path for XLSX output (optional)

    [Parameter(Mandatory = $false)]
    [string] $AdminUrl,         # Override admin URL (default: derived from TenantDomain)

    [Parameter(Mandatory = $true)]
    [string] $ClientId,

    [Parameter(Mandatory = $true)]
    [string] $CertificatePath,  # PFX path

    [Parameter(Mandatory = $false)]
    [securestring] $CertificatePassword,

    [Parameter(Mandatory = $false)]
    [switch] $Append = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Import shared module
Import-Module (Join-Path $PSScriptRoot "Shared.psm1") -Force

# Resolve PFX password
$CertificatePassword = Get-AuditCertificatePassword -CertificatePassword $CertificatePassword
$excludedLists = Get-ExcludedSystemLists

# Derive admin URL from Tenant value if not explicitly provided
function Get-AdminUrl {
    param([string]$Tenant, [string]$Override)
    if (-not [string]::IsNullOrWhiteSpace($Override)) { return $Override }

    if ($Tenant -match '^[0-9a-f-]{36}$') {
        throw "Tenant appears to be a GUID — please provide -AdminUrl explicitly (e.g. https://contoso-admin.sharepoint.com)"
    }

    $prefix = ($Tenant -split '\.')[0]
    return "https://$prefix-admin.sharepoint.com"
}

function Get-UserGroupMembership {
    param ([Parameter(Mandatory = $true)][string] $UserEmail)

    $token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId `
        -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword
    $encodedUserEmail = [System.Web.HttpUtility]::UrlEncode($UserEmail)
    $uri = "https://graph.microsoft.com/v1.0/users/$encodedUserEmail/memberOf?`$select=id,displayName"

    $results = Invoke-GraphRequest -Uri $uri -AccessToken $token.AccessToken
    $groups = [System.Collections.Generic.List[PSCustomObject]]::new()
    foreach ($item in $results) {
        $groups.Add([PSCustomObject]@{
            GroupName = $item.displayName
            GroupId   = $item.id
        })
    }
    return $groups.ToArray()
}

function New-CsvFile {
    param ([Parameter(Mandatory = $true)][string] $Path)

    $dir = Split-Path $Path -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    if (Test-Path $Path) { Remove-Item $Path -Force }

    [PSCustomObject]@{
        UserPrincipalName = $null; SiteUrl = $null; SiteAdmin = $null
        GroupName = $null; PermissionLevel = $null; ListName = $null; ListPermission = $null
    } | Export-Csv -Path $Path -NoTypeInformation

    # Remove empty data row, keep header only
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
        $idPart = ($siteAdmin.LoginName -split '\|')[-1]
        if ($UserEmail -eq $idPart) { return $true }
        if ($GraphGroups -and $GraphGroups.Count -gt 0 -and ($GraphGroups.GroupId -contains $idPart)) { return $true }
    }
    return $false
}

function Get-UserSharePointGroups {
    param (
        [Parameter(Mandatory = $true)][string] $UserEmail,
        [Parameter(Mandatory = $false)][array]  $GraphGroups
    )

    $membership = [System.Collections.Generic.List[PSCustomObject]]::new()

    foreach ($siteGroup in (Get-PnPGroup)) {
        $matched = $false
        foreach ($member in (Get-PnPGroupMember -Identity $siteGroup.Title)) {
            $idPart         = ($member.LoginName -split '\|')[-1]
            $isDirect       = ($UserEmail -eq $idPart)
            $isViaEntra     = ($GraphGroups -and $GraphGroups.Count -gt 0 -and ($GraphGroups.GroupId -contains $idPart))

            if ($isDirect -or $isViaEntra) {
                $levels      = Get-PnPGroupPermissions -Identity $siteGroup
                $levelString = ($levels | ForEach-Object { $_.Name }) -join " | "
                if ([string]::IsNullOrWhiteSpace($levelString)) { $levelString = "No Permissions" }

                $membership.Add([PSCustomObject]@{
                    GroupName       = $siteGroup.Title
                    PermissionLevel = $levelString
                })
                $matched = $true
                break
            }
        }
        if ($matched) { continue }
    }

    return $membership.ToArray()
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

    $results = [System.Collections.Generic.List[PSCustomObject]]::new()

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

            $idPart     = ($ra.Member.LoginName -split '\|')[-1]
            $isDirect   = ($UserEmail -eq $idPart)
            $isViaEntra = ($GraphGroups -and $GraphGroups.Count -gt 0 -and ($GraphGroups.GroupId -contains $idPart))

            if ($isDirect -or $isViaEntra) {
                $results.Add([PSCustomObject]@{
                    Name            = $list.Title
                    PermissionLevel = ($ra.RoleDefinitionBindings | ForEach-Object { $_.Name }) -join "; "
                })
            }
        }
    }

    return $results.ToArray()
}

# ------------------- Main -------------------

$adminUrl = Get-AdminUrl -Tenant $Tenant -Override $AdminUrl

Write-Host "$(Get-Date) INFO: Connecting to tenant admin..."
Connect-AuditSite -SiteUrl $adminUrl -ClientId $ClientId -Tenant $Tenant `
    -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword

Write-Host "$(Get-Date) INFO: Retrieving site collections..."
$siteCollections = Get-PnPTenantSite
Write-Host "$(Get-Date) INFO: Found $($siteCollections.Count) site collections."
Disconnect-PnPOnline

Write-Host "$(Get-Date) INFO: Getting Entra group membership for $UserEmail..."
$userGroupMembership = Get-UserGroupMembership -UserEmail $UserEmail
Write-Host "$(Get-Date) INFO: Member of $($userGroupMembership.Count) groups."

if (-not $Append) { New-CsvFile -Path $CSVPath }

$allRows   = [System.Collections.Generic.List[PSCustomObject]]::new()
$total     = $siteCollections.Count
$counter   = 0

foreach ($site in $siteCollections) {
    $counter++
    Write-Progress -Activity "Auditing user permissions" `
                   -Status    "Site $counter of $total : $($site.Url)" `
                   -PercentComplete ([math]::Round(($counter / $total) * 100))

    Write-Host "$(Get-Date) INFO: [$counter/$total] $($site.Url)"

    try {
        Connect-AuditSite -SiteUrl $site.Url -ClientId $ClientId -Tenant $Tenant `
            -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword
    }
    catch {
        Write-Host "$(Get-Date) WARN: Could not connect to $($site.Url) — skipping. $_" -ForegroundColor Yellow
        continue
    }

    try {
        if (Test-UserIsSiteCollectionAdmin -UserEmail $UserEmail -GraphGroups $userGroupMembership) {
            $row = [PSCustomObject]@{
                UserPrincipalName = $UserEmail; SiteUrl = $site.Url; SiteAdmin = $true
                GroupName = $null; PermissionLevel = $null; ListName = $null; ListPermission = $null
            }
            $row | Export-Csv -Path $CSVPath -Append -NoTypeInformation
            $allRows.Add($row)
            Write-Host "$(Get-Date) INFO:   Site Collection Admin"
        } else {
            $spGroups = Get-UserSharePointGroups -UserEmail $UserEmail -GraphGroups $userGroupMembership
            foreach ($g in $spGroups) {
                $row = [PSCustomObject]@{
                    UserPrincipalName = $UserEmail; SiteUrl = $site.Url; SiteAdmin = $false
                    GroupName = $g.GroupName; PermissionLevel = $g.PermissionLevel; ListName = $null; ListPermission = $null
                }
                $row | Export-Csv -Path $CSVPath -Append -NoTypeInformation
                $allRows.Add($row)
                Write-Host "$(Get-Date) INFO:   Group: $($g.GroupName) [$($g.PermissionLevel)]"
            }

            $listPerms = Get-UniqueListPermissions -UserEmail $UserEmail -GraphGroups $userGroupMembership
            foreach ($lp in $listPerms) {
                $row = [PSCustomObject]@{
                    UserPrincipalName = $UserEmail; SiteUrl = $site.Url; SiteAdmin = $false
                    GroupName = $null; PermissionLevel = $null; ListName = $lp.Name; ListPermission = $lp.PermissionLevel
                }
                $row | Export-Csv -Path $CSVPath -Append -NoTypeInformation
                $allRows.Add($row)
                Write-Host "$(Get-Date) INFO:   List: '$($lp.Name)' [$($lp.PermissionLevel)]"
            }
        }
    }
    finally {
        Disconnect-PnPOnline
    }
}

Write-Progress -Activity "Auditing user permissions" -Completed

Write-Host ""
Write-Host "$(Get-Date) INFO: CSV saved to $CSVPath"

# Export XLSX if requested and ImportExcel is available
if (-not [string]::IsNullOrWhiteSpace($XlsxPath)) {
    if (Get-Module -ListAvailable -Name ImportExcel) {
        Import-Module ImportExcel -ErrorAction Stop
        $allRows.ToArray() | Export-Excel `
            -Path          $XlsxPath `
            -WorksheetName "UserPermissions" `
            -AutoFilter `
            -BoldTopRow `
            -FreezeTopRow `
            -AutoSize
        Write-Host "$(Get-Date) INFO: XLSX saved to $XlsxPath"
    } else {
        Write-Host "$(Get-Date) WARN: ImportExcel module not found — XLSX not exported." -ForegroundColor Yellow
    }
}

Write-Host "$(Get-Date) INFO: Done. $($allRows.Count) permission record(s) found for $UserEmail."
