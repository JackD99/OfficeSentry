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
    [Parameter(Mandatory = $false)][securestring] $CertificatePassword
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Import shared module
Import-Module (Join-Path $PSScriptRoot "Shared.psm1") -Force

# Resolve PFX password (explicit param -> env var -> interactive prompt)
$CertificatePassword = Get-AuditCertificatePassword -CertificatePassword $CertificatePassword

Write-Host "$(Get-Date) INFO: Connecting to $SiteUrl ..."
Connect-AuditSite -SiteUrl $SiteUrl -ClientId $ClientId -Tenant $Tenant `
    -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword

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
        $type      = Get-PrincipalType -LoginName $loginName

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

# Build output filename
$siteName = ($SiteUrl -replace 'https?://', '' -replace '[/\\:*?"<>|]', '_')

Export-AuditData -OutputFolder $OutputFolder -FilePrefix "Groups_${siteName}" -Sheets @(
    @{ Name = "Groups";  Data = $groupSummary.ToArray() },
    @{ Name = "Members"; Data = $groupMembers.ToArray() }
)

Write-Host "$(Get-Date) INFO: Done. $($groupSummary.Count) groups, $($groupMembers.Count) member records."
