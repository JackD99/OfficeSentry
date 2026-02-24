# Audit-Groups.ps1
# Lists all SharePoint groups at a site, their permission levels, and member details.
# Exports to XLSX (two sheets: Groups summary + Members).
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

Write-Host "$(Get-Date) INFO: Connecting to $SiteUrl ..."
Connect-PnPOnline `
    -Url                 $SiteUrl `
    -ClientId            $ClientId `
    -Tenant              $Tenant `
    -CertificatePath     $CertificatePath `
    -CertificatePassword $CertificatePassword

Write-Host "$(Get-Date) INFO: Retrieving SharePoint groups..."
$groups = Get-PnPGroup

$groupSummary = [System.Collections.Generic.List[PSCustomObject]]::new()
$groupMembers = [System.Collections.Generic.List[PSCustomObject]]::new()

$total   = $groups.Count
$counter = 0

foreach ($group in $groups) {
    $counter++
    Write-Progress -Activity "Auditing groups" `
                   -Status    "Group $counter of $total : $($group.Title)" `
                   -PercentComplete ([math]::Round(($counter / $total) * 100))

    # Get permission levels
    $permissions  = Get-PnPGroupPermissions -Identity $group
    $permString   = ($permissions | ForEach-Object { $_.Name }) -join " | "
    if ([string]::IsNullOrWhiteSpace($permString)) { $permString = "No Permissions" }

    # Get members
    $members    = Get-PnPGroupMember -Identity $group.Title
    $memberList = [System.Collections.Generic.List[PSCustomObject]]::new()

    foreach ($m in $members) {
        $loginName = $m.LoginName
        $email     = if ($m.Email) { $m.Email } else { ($loginName -split '\|')[-1] }
        $type      = if ($m.PrincipalType -eq 'SecurityGroup' -or $m.PrincipalType -eq 'SharePointGroup') { "Group" } else { "User" }

        $memberRow = [PSCustomObject]@{
            GroupName  = $group.Title
            MemberName = $m.Title
            Email      = $email
            LoginName  = $loginName
            MemberType = $type
        }
        $memberList.Add($memberRow)
        $groupMembers.Add($memberRow)
    }

    $groupSummary.Add([PSCustomObject]@{
        GroupName       = $group.Title
        Description     = $group.Description
        PermissionLevel = $permString
        MemberCount     = $memberList.Count
        Owner           = $group.OwnerTitle
    })
}

Write-Progress -Activity "Auditing groups" -Completed
Disconnect-PnPOnline

# Build output paths
$siteName = ($SiteUrl -replace 'https?://', '' -replace '[/\\:*?"<>|]', '_')
$stamp    = Get-Date -Format "yyyyMMdd-HHmm"
$xlsx     = Join-Path $OutputFolder "Groups_${siteName}_${stamp}.xlsx"

if (-not (Test-Path $OutputFolder)) { New-Item -ItemType Directory -Path $OutputFolder | Out-Null }

if (Get-Module -ListAvailable -Name ImportExcel) {
    Import-Module ImportExcel -ErrorAction Stop

    $groupSummary.ToArray() | Export-Excel `
        -Path          $xlsx `
        -WorksheetName "Groups" `
        -AutoFilter `
        -BoldTopRow `
        -FreezeTopRow `
        -AutoSize `
        -ClearSheet

    $groupMembers.ToArray() | Export-Excel `
        -Path          $xlsx `
        -WorksheetName "Members" `
        -AutoFilter `
        -BoldTopRow `
        -FreezeTopRow `
        -AutoSize

    Write-Host "$(Get-Date) INFO: Report saved to $xlsx" -ForegroundColor Green
} else {
    # Fallback to CSV
    $csv1 = Join-Path $OutputFolder "Groups_${siteName}_${stamp}.csv"
    $csv2 = Join-Path $OutputFolder "GroupMembers_${siteName}_${stamp}.csv"
    $groupSummary.ToArray() | Export-Csv -Path $csv1 -NoTypeInformation
    $groupMembers.ToArray() | Export-Csv -Path $csv2 -NoTypeInformation
    Write-Host "$(Get-Date) WARN: ImportExcel not found. Exported CSV instead:" -ForegroundColor Yellow
    Write-Host "  $csv1"
    Write-Host "  $csv2"
}

Write-Host "$(Get-Date) INFO: Done. $($groupSummary.Count) groups, $($groupMembers.Count) member records."
