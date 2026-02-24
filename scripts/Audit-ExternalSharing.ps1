# Audit-ExternalSharing.ps1
# Reports external access on a SharePoint site:
#   - External users who have been granted access
#   - Sharing links (anonymous + organisation + specific-people) on document library items
# Exports to XLSX (two sheets: ExternalUsers + SharingLinks).
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

function Test-IsExternal {
    param([string]$LoginName)
    return ($LoginName -match '#ext#' -or
            $LoginName -match 'urn:spo:guest' -or
            $LoginName -match 'urn:spo:anon')
}

Write-Host "$(Get-Date) INFO: Connecting to $SiteUrl ..."
Connect-PnPOnline `
    -Url                 $SiteUrl `
    -ClientId            $ClientId `
    -Tenant              $Tenant `
    -CertificatePath     $CertificatePath `
    -CertificatePassword $CertificatePassword

# ---- External users ----
Write-Host "$(Get-Date) INFO: Retrieving site users..."

$allUsers     = Get-PnPUser
$externalRows = [System.Collections.Generic.List[PSCustomObject]]::new()

foreach ($user in $allUsers) {
    if (-not (Test-IsExternal -LoginName $user.LoginName)) { continue }

    $email = if ($user.Email) { $user.Email } else {
        if ($user.LoginName -match '#ext#') {
            # Extract email from UPN-style guest login
            ($user.LoginName -split '\|')[-1] -replace '#ext#.*', ''
        } else { "" }
    }

    $externalRows.Add([PSCustomObject]@{
        Name      = $user.Title
        Email     = $email
        LoginName = $user.LoginName
        UserId    = $user.Id
    })
}

Write-Host "$(Get-Date) INFO: Found $($externalRows.Count) external users."

# ---- Sharing links ----
Write-Host "$(Get-Date) INFO: Scanning document libraries for sharing links..."

$ctx  = Get-PnPContext
$web  = $ctx.Web
$ctx.Load($web)
$web.Context.Load($web.Lists)
$ctx.ExecuteQuery()

$linkRows     = [System.Collections.Generic.List[PSCustomObject]]::new()

$docLibraries = $web.Lists | Where-Object { $_.BaseTemplate -eq 101 -and -not $_.Hidden }
$total        = @($docLibraries).Count
$counter      = 0

foreach ($lib in $docLibraries) {
    $counter++
    Write-Progress -Activity "Scanning libraries for sharing links" `
                   -Status    "Library $counter of $total : $($lib.Title)" `
                   -PercentComplete ([math]::Round(($counter / $total) * 100))

    Write-Host "$(Get-Date) INFO:   Library: $($lib.Title)"

    try {
        # Get-PnPFileSharingLink works per file; iterate items in the library
        $query = [Microsoft.SharePoint.Client.CamlQuery]::CreateAllItemsQuery(500)
        $items = $lib.GetItems($query)
        $ctx.Load($items)
        $ctx.ExecuteQuery()

        foreach ($item in $items) {
            if ($item.FileSystemObjectType -ne "File") { continue }

            $itemName = $item["FileLeafRef"]
            $itemUrl  = $item["FileRef"]

            try {
                $sharingInfo = Get-PnPFileSharingLink -FileUrl $itemUrl -ErrorAction SilentlyContinue
                if (-not $sharingInfo) { continue }

                foreach ($link in $sharingInfo) {
                    $linkType = switch ($link.Link.Scope) {
                        "anonymous"      { "Anonymous (anyone)" }
                        "organization"   { "Organisation" }
                        "specificPeople" { "Specific people" }
                        default          { $link.Link.Scope }
                    }

                    $expiry    = if ($link.ExpirationDateTime) { $link.ExpirationDateTime.ToString("yyyy-MM-dd") } else { "No expiry" }
                    $createdBy = if ($link.CreatedBy) { $link.CreatedBy.DisplayName } else { "" }

                    $linkRows.Add([PSCustomObject]@{
                        Library    = $lib.Title
                        ItemName   = $itemName
                        ItemUrl    = $itemUrl
                        LinkType   = $linkType
                        Expiry     = $expiry
                        CreatedBy  = $createdBy
                        LinkUrl    = $link.Link.WebUrl
                    })
                }
            }
            catch {
                # Some items may not support sharing link retrieval — skip silently
            }
        }
    }
    catch {
        Write-Host "$(Get-Date) WARN: Could not scan library '$($lib.Title)': $_" -ForegroundColor Yellow
    }
}

Write-Progress -Activity "Scanning libraries for sharing links" -Completed
Disconnect-PnPOnline

Write-Host "$(Get-Date) INFO: Found $($linkRows.Count) sharing link records."

# ---- Export ----
$siteName = ($SiteUrl -replace 'https?://', '' -replace '[/\\:*?"<>|]', '_')
$stamp    = Get-Date -Format "yyyyMMdd-HHmm"
$xlsx     = Join-Path $OutputFolder "ExternalSharing_${siteName}_${stamp}.xlsx"

if (-not (Test-Path $OutputFolder)) { New-Item -ItemType Directory -Path $OutputFolder | Out-Null }

if (Get-Module -ListAvailable -Name ImportExcel) {
    Import-Module ImportExcel -ErrorAction Stop

    $externalRows.ToArray() | Export-Excel `
        -Path          $xlsx `
        -WorksheetName "ExternalUsers" `
        -AutoFilter `
        -BoldTopRow `
        -FreezeTopRow `
        -AutoSize `
        -ClearSheet

    if ($linkRows.Count -gt 0) {
        $linkRows.ToArray() | Export-Excel `
            -Path          $xlsx `
            -WorksheetName "SharingLinks" `
            -AutoFilter `
            -BoldTopRow `
            -FreezeTopRow `
            -AutoSize
    } else {
        # Write an empty sheet with a note so the workbook isn't misleading
        [PSCustomObject]@{ Note = "No sharing links found" } | Export-Excel `
            -Path          $xlsx `
            -WorksheetName "SharingLinks" `
            -ClearSheet
    }

    Write-Host "$(Get-Date) INFO: Report saved to $xlsx" -ForegroundColor Green
} else {
    $csv1 = Join-Path $OutputFolder "ExternalUsers_${siteName}_${stamp}.csv"
    $externalRows.ToArray() | Export-Csv -Path $csv1 -NoTypeInformation
    Write-Host "$(Get-Date) WARN: ImportExcel not found — exported CSV to $csv1" -ForegroundColor Yellow
    if ($linkRows.Count -gt 0) {
        $csv2 = Join-Path $OutputFolder "SharingLinks_${siteName}_${stamp}.csv"
        $linkRows.ToArray() | Export-Csv -Path $csv2 -NoTypeInformation
        Write-Host "  $csv2"
    }
}

Write-Host "$(Get-Date) INFO: Done. $($externalRows.Count) external user(s), $($linkRows.Count) sharing link(s)."
