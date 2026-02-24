# Audit-SitePermissions.ps1
# Full permission snapshot for a SharePoint site:
#   - Site-level role assignments (every principal and their permission level)
#   - Lists/libraries with unique permissions (broken inheritance)
# Exports to XLSX (two sheets: SitePermissions + ListPermissions).
# Requires: PnP.PowerShell, ImportExcel

#requires -Modules PnP.PowerShell

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)][string] $SiteUrl,
    [Parameter(Mandatory = $true)][string] $OutputFolder,
    [Parameter(Mandatory = $true)][string] $ClientId,
    [Parameter(Mandatory = $true)][string] $Tenant,
    [Parameter(Mandatory = $true)][string] $CertificatePath,
    [Parameter(Mandatory = $true)][securestring] $CertificatePassword
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

function Get-PrincipalType {
    param([string]$LoginName)
    if ($LoginName -match '^c:0t\.c\|tenant\|' -or $LoginName -match 'spo-grid-all-users') { return "Everyone" }
    if ($LoginName -match '#ext#|urn:spo:guest') { return "External" }
    if ($LoginName -match '^i:0#\.f\|membership\|') { return "User" }
    if ($LoginName -match '^c:0o\.c\|federateddirectoryclaimprovider\|') { return "M365 Group" }
    if ($LoginName -match 'sharepoint\|') { return "SP Group" }
    return "Other"
}

Write-Host "$(Get-Date) INFO: Connecting to $SiteUrl ..."
Connect-PnPOnline `
    -Url                 $SiteUrl `
    -ClientId            $ClientId `
    -Tenant              $Tenant `
    -CertificatePath     $CertificatePath `
    -CertificatePassword $CertificatePassword

# ---- Site-level permissions ----
Write-Host "$(Get-Date) INFO: Reading site-level role assignments..."

$ctx = Get-PnPContext
$web = $ctx.Web
$ctx.Load($web)
$web.Context.Load($web.RoleAssignments)
$ctx.ExecuteQuery()

$siteRows = [System.Collections.Generic.List[PSCustomObject]]::new()

foreach ($ra in $web.RoleAssignments) {
    $ctx.Load($ra.Member)
    $ctx.Load($ra.RoleDefinitionBindings)
    $ctx.ExecuteQuery()

    $levels = ($ra.RoleDefinitionBindings | ForEach-Object { $_.Name }) -join " | "
    if ([string]::IsNullOrWhiteSpace($levels)) { $levels = "No Permissions" }

    $siteRows.Add([PSCustomObject]@{
        PrincipalName   = $ra.Member.Title
        LoginName       = $ra.Member.LoginName
        PrincipalType   = Get-PrincipalType -LoginName $ra.Member.LoginName
        PermissionLevel = $levels
    })
}

Write-Host "$(Get-Date) INFO: Found $($siteRows.Count) site-level assignments."

# ---- List-level unique permissions ----
Write-Host "$(Get-Date) INFO: Reading lists with unique permissions..."

$lists = $web.Lists
$ctx.Load($lists)
$ctx.ExecuteQuery()

$listRows   = [System.Collections.Generic.List[PSCustomObject]]::new()
$listCount  = 0
$total      = $lists.Count
$counter    = 0

foreach ($list in $lists) {
    $counter++
    $ctx.Load($list)
    $ctx.ExecuteQuery()

    Write-Progress -Activity "Checking lists" `
                   -Status    "List $counter of $total : $($list.Title)" `
                   -PercentComplete ([math]::Round(($counter / $total) * 100))

    if ($excludedLists -contains $list.Title) { continue }

    $list.Retrieve("HasUniqueRoleAssignments")
    $ctx.ExecuteQuery()
    if (-not $list.HasUniqueRoleAssignments) { continue }

    $listCount++
    $assignments = $list.RoleAssignments
    $ctx.Load($assignments)
    $ctx.ExecuteQuery()

    foreach ($ra in $assignments) {
        $ctx.Load($ra.Member)
        $ctx.Load($ra.RoleDefinitionBindings)
        $ctx.ExecuteQuery()

        $levels = ($ra.RoleDefinitionBindings | ForEach-Object { $_.Name }) -join " | "
        if ([string]::IsNullOrWhiteSpace($levels)) { $levels = "No Permissions" }

        $listRows.Add([PSCustomObject]@{
            ListName        = $list.Title
            PrincipalName   = $ra.Member.Title
            LoginName       = $ra.Member.LoginName
            PrincipalType   = Get-PrincipalType -LoginName $ra.Member.LoginName
            PermissionLevel = $levels
        })
    }
}

Write-Progress -Activity "Checking lists" -Completed
Disconnect-PnPOnline

Write-Host "$(Get-Date) INFO: $listCount lists with unique permissions, $($listRows.Count) list-level assignment records."

# ---- Export ----
$siteName = ($SiteUrl -replace 'https?://', '' -replace '[/\\:*?"<>|]', '_')
$stamp    = Get-Date -Format "yyyyMMdd-HHmm"
$xlsx     = Join-Path $OutputFolder "SitePermissions_${siteName}_${stamp}.xlsx"

if (-not (Test-Path $OutputFolder)) { New-Item -ItemType Directory -Path $OutputFolder | Out-Null }

if (Get-Module -ListAvailable -Name ImportExcel) {
    Import-Module ImportExcel -ErrorAction Stop

    $siteRows.ToArray() | Export-Excel `
        -Path          $xlsx `
        -WorksheetName "SitePermissions" `
        -AutoFilter `
        -BoldTopRow `
        -FreezeTopRow `
        -AutoSize `
        -ClearSheet

    if ($listRows.Count -gt 0) {
        $listRows.ToArray() | Export-Excel `
            -Path          $xlsx `
            -WorksheetName "ListPermissions" `
            -AutoFilter `
            -BoldTopRow `
            -FreezeTopRow `
            -AutoSize
    }

    Write-Host "$(Get-Date) INFO: Report saved to $xlsx" -ForegroundColor Green
} else {
    $csv1 = Join-Path $OutputFolder "SitePermissions_${siteName}_${stamp}.csv"
    $siteRows.ToArray() | Export-Csv -Path $csv1 -NoTypeInformation
    Write-Host "$(Get-Date) WARN: ImportExcel not found — exported CSV to $csv1" -ForegroundColor Yellow
    if ($listRows.Count -gt 0) {
        $csv2 = Join-Path $OutputFolder "ListPermissions_${siteName}_${stamp}.csv"
        $listRows.ToArray() | Export-Csv -Path $csv2 -NoTypeInformation
        Write-Host "  $csv2"
    }
}

Write-Host "$(Get-Date) INFO: Done. $($siteRows.Count) site-level, $($listRows.Count) list-level records."
