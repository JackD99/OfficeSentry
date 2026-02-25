# Invoke-SPOAdmin.ps1
# SharePoint Online site administration actions via PnP.PowerShell.
# Supports: list sites, storage report, site admin management,
#           sharing settings, and site lock/unlock.
# Designed for MSP helpdesk staff managing SharePoint across client tenants.
#
# Required API Permissions (Application):
#   - Sites.FullControl.All (PnP.PowerShell)
#   - SharePoint Administrator role (for tenant-level operations)

#requires -Modules PnP.PowerShell

[CmdletBinding()]
param (
    [Parameter(Mandatory)][ValidateSet(
        "list_sites","site_storage",
        "add_site_admin","remove_site_admin",
        "set_site_sharing",
        "lock_site","unlock_site"
    )]
    [string] $Action,
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OutputFolder    = "",
    [string] $SiteUrl         = "",
    [string] $TargetUser      = "",
    [ValidateSet("Disabled","ExistingExternalUserSharingOnly","ExternalUserSharingOnly","ExternalUserAndGuestSharing","")]
    [string] $SharingLevel    = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

# Derive the admin site URL from tenant domain
$tenantDomain = $Tenant -replace '\.onmicrosoft\.com$', ''
$adminUrl = "https://${tenantDomain}-admin.sharepoint.com"

Write-Host "$(Get-Date) INFO: Connecting to SharePoint admin ($adminUrl)..."
Connect-AuditSite -SiteUrl $adminUrl -ClientId $ClientId -Tenant $Tenant `
    -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword

Write-Host "$(Get-Date) INFO: Performing SharePoint action '$Action'..."

try {
    switch ($Action) {
        "list_sites" {
            Write-Host "$(Get-Date) INFO: Retrieving all site collections..."
            $sites = Get-PnPTenantSite -Detailed

            $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
            foreach ($site in $sites) {
                $storageMB = [math]::Round($site.StorageUsageCurrent, 2)
                $quotaMB = $site.StorageMaximumLevel
                $pctUsed = if ($quotaMB -gt 0) { [math]::Round(($storageMB / $quotaMB) * 100, 1) } else { 0 }

                $rows.Add([PSCustomObject]@{
                    Title           = $site.Title
                    Url             = $site.Url
                    Template        = $site.Template
                    StorageUsedMB   = $storageMB
                    StorageQuotaMB  = $quotaMB
                    PercentUsed     = $pctUsed
                    Owner           = $site.Owner
                    SharingCapability = $site.SharingCapability
                    LockState       = $site.LockState
                    LastModified    = $site.LastContentModifiedDate
                    SiteCount       = $site.WebsCount
                })
            }

            Write-Host "$(Get-Date) INFO: Found $($rows.Count) site collections."
            if ($OutputFolder) {
                $sheets = @(@{ Name = "Sites"; Data = $rows.ToArray() })
                Export-AuditData -OutputFolder $OutputFolder -FilePrefix "SPO_Sites" -Sheets $sheets
            }
        }

        "site_storage" {
            Write-Host "$(Get-Date) INFO: Generating site storage report..."
            $sites = Get-PnPTenantSite -Detailed

            $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
            foreach ($site in $sites) {
                $storageMB = [math]::Round($site.StorageUsageCurrent, 2)
                $storageGB = [math]::Round($storageMB / 1024, 2)
                $quotaMB = $site.StorageMaximumLevel
                $quotaGB = [math]::Round($quotaMB / 1024, 2)
                $pctUsed = if ($quotaMB -gt 0) { [math]::Round(($storageMB / $quotaMB) * 100, 1) } else { 0 }

                $rows.Add([PSCustomObject]@{
                    Title          = $site.Title
                    Url            = $site.Url
                    StorageUsedGB  = $storageGB
                    StorageQuotaGB = $quotaGB
                    PercentUsed    = $pctUsed
                    Status         = if ($pctUsed -ge 90) { "Critical" } elseif ($pctUsed -ge 75) { "Warning" } else { "OK" }
                })
            }

            # Sort by usage descending
            $sorted = $rows | Sort-Object -Property { [double]$_.StorageUsedGB } -Descending

            $totalUsedGB = [math]::Round(($rows | Measure-Object -Property StorageUsedGB -Sum).Sum, 2)
            Write-Host "$(Get-Date) INFO: $($rows.Count) sites, ${totalUsedGB} GB total storage used."

            if ($OutputFolder) {
                $sheets = @(@{ Name = "StorageReport"; Data = @($sorted) })
                Export-AuditData -OutputFolder $OutputFolder -FilePrefix "SPO_Storage" -Sheets $sheets
            }
        }

        "add_site_admin" {
            if (-not $SiteUrl) { throw "SiteUrl is required." }
            if (-not $TargetUser) { throw "TargetUser is required." }

            # Connect to the specific site
            Disconnect-PnPOnline 2>$null
            Connect-AuditSite -SiteUrl $SiteUrl -ClientId $ClientId -Tenant $Tenant `
                -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword

            Set-PnPTenantSite -Url $SiteUrl -Owners $TargetUser
            Write-Host "$(Get-Date) INFO: SUCCESS - Added '$TargetUser' as site collection admin on '$SiteUrl'."
        }

        "remove_site_admin" {
            if (-not $SiteUrl) { throw "SiteUrl is required." }
            if (-not $TargetUser) { throw "TargetUser is required." }

            # Connect to the specific site
            Disconnect-PnPOnline 2>$null
            Connect-AuditSite -SiteUrl $SiteUrl -ClientId $ClientId -Tenant $Tenant `
                -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword

            Remove-PnPSiteCollectionAdmin -Owners $TargetUser
            Write-Host "$(Get-Date) INFO: SUCCESS - Removed '$TargetUser' as site collection admin on '$SiteUrl'."
        }

        "set_site_sharing" {
            if (-not $SiteUrl) { throw "SiteUrl is required." }
            if (-not $SharingLevel) { throw "SharingLevel is required." }

            Set-PnPTenantSite -Url $SiteUrl -SharingCapability $SharingLevel
            $labels = @{
                Disabled                       = "Disabled (no external sharing)"
                ExistingExternalUserSharingOnly = "Existing external users only"
                ExternalUserSharingOnly        = "New and existing external users"
                ExternalUserAndGuestSharing    = "Anyone (including anonymous)"
            }
            $label = $labels[$SharingLevel]
            Write-Host "$(Get-Date) INFO: SUCCESS - Sharing set to '$label' on '$SiteUrl'."
        }

        "lock_site" {
            if (-not $SiteUrl) { throw "SiteUrl is required." }
            Set-PnPTenantSite -Url $SiteUrl -LockState "ReadOnly"
            Write-Host "$(Get-Date) INFO: SUCCESS - Site '$SiteUrl' locked (read-only)."
        }

        "unlock_site" {
            if (-not $SiteUrl) { throw "SiteUrl is required." }
            Set-PnPTenantSite -Url $SiteUrl -LockState "Unlock"
            Write-Host "$(Get-Date) INFO: SUCCESS - Site '$SiteUrl' unlocked."
        }
    }
}
catch {
    Write-Host "$(Get-Date) ERROR: Action '$Action' failed: $_" -ForegroundColor Red
}
finally {
    Disconnect-PnPOnline 2>$null
}

Write-Host "$(Get-Date) INFO: SharePoint action '$Action' completed."
