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
    [Parameter(Mandatory = $false)][securestring] $CertificatePassword
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Import shared module
Import-Module (Join-Path $PSScriptRoot "Shared.psm1") -Force

# Resolve PFX password
$CertificatePassword = Get-AuditCertificatePassword -CertificatePassword $CertificatePassword

function Test-IsExternal {
    param([string]$LoginName)
    return ($LoginName -match '#ext#' -or
            $LoginName -match 'urn:spo:guest' -or
            $LoginName -match 'urn:spo:anon')
}

Write-Host "$(Get-Date) INFO: Connecting to $SiteUrl ..."
Connect-AuditSite -SiteUrl $SiteUrl -ClientId $ClientId -Tenant $Tenant `
    -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword

# ---- External users ----
Write-Host "$(Get-Date) INFO: Retrieving site users..."

$allUsers     = Get-PnPUser
$externalRows = [System.Collections.Generic.List[PSCustomObject]]::new()

foreach ($user in $allUsers) {
    if (-not (Test-IsExternal -LoginName $user.LoginName)) { continue }

    $email = if ($user.Email) { $user.Email } else {
        if ($user.LoginName -match '#ext#') {
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
                # Some items may not support sharing link retrieval
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

$sheets = @(
    @{ Name = "ExternalUsers"; Data = $externalRows.ToArray() }
)
if ($linkRows.Count -gt 0) {
    $sheets += @{ Name = "SharingLinks"; Data = $linkRows.ToArray() }
} else {
    $sheets += @{ Name = "SharingLinks"; Data = @([PSCustomObject]@{ Note = "No sharing links found" }) }
}

Export-AuditData -OutputFolder $OutputFolder -FilePrefix "ExternalSharing_${siteName}" -Sheets $sheets

Write-Host "$(Get-Date) INFO: Done. $($externalRows.Count) external user(s), $($linkRows.Count) sharing link(s)."
