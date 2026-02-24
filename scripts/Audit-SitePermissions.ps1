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
    [Parameter(Mandatory = $false)][securestring] $CertificatePassword
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Import shared module
Import-Module (Join-Path $PSScriptRoot "Shared.psm1") -Force

# Resolve PFX password
$CertificatePassword = Get-AuditCertificatePassword -CertificatePassword $CertificatePassword
$excludedLists = Get-ExcludedSystemLists

Write-Host "$(Get-Date) INFO: Connecting to $SiteUrl ..."
Connect-AuditSite -SiteUrl $SiteUrl -ClientId $ClientId -Tenant $Tenant `
    -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword

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

Export-AuditData -OutputFolder $OutputFolder -FilePrefix "SitePermissions_${siteName}" -Sheets @(
    @{ Name = "SitePermissions"; Data = $siteRows.ToArray() },
    @{ Name = "ListPermissions"; Data = $listRows.ToArray() }
)

Write-Host "$(Get-Date) INFO: Done. $($siteRows.Count) site-level, $($listRows.Count) list-level records."
