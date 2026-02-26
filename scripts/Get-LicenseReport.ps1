# Get-LicenseReport.ps1
# Retrieves license data from Microsoft Graph API.
# Supports inventory, usage, optimization, overview, and user detail reports.
# Requires: MSAL.PS, ImportExcel (optional)

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [Parameter(Mandatory)]
    [ValidateSet("inventory", "usage", "optimization", "overview", "userDetail")]
    [string] $ReportType,
    [string] $InactiveDays = "90"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Acquiring Graph token..."
$token = Get-AuditGraphToken `
    -Tenant              $Tenant `
    -ClientId            $ClientId `
    -CertificatePath     $CertificatePath `
    -CertificatePassword $CertificatePassword

$accessToken = $token.AccessToken
$safeTenant = ($Tenant -replace '[^\w\-\.]', '_')
$ts = Get-Date -Format "yyyyMMdd-HHmm"

switch ($ReportType) {
    "inventory" {
        Write-Host "$(Get-Date) INFO: Fetching subscribed SKUs..."
        $uri = "https://graph.microsoft.com/v1.0/subscribedSkus"
        $skus = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 10
        Write-Host "$(Get-Date) INFO: Found $($skus.Count) subscription(s)."

        if ($skus.Count -gt 0) {
            $results = $skus | Select-Object skuPartNumber, skuId,
                @{N="TotalLicenses";E={$_.prepaidUnits.enabled}},
                @{N="AssignedLicenses";E={$_.consumedUnits}},
                @{N="AvailableLicenses";E={$_.prepaidUnits.enabled - $_.consumedUnits}},
                @{N="SuspendedUnits";E={$_.prepaidUnits.suspended}},
                @{N="WarningUnits";E={$_.prepaidUnits.warning}},
                appliesTo, capabilityStatus

            foreach ($sku in $results) {
                $pct = if ($sku.TotalLicenses -gt 0) {
                    [math]::Round(($sku.AssignedLicenses / $sku.TotalLicenses) * 100, 1)
                } else { 0 }
                Write-Host "$(Get-Date) INFO: $($sku.skuPartNumber) — $($sku.AssignedLicenses)/$($sku.TotalLicenses) ($pct% used)"
            }

            $baseName = "LicenseInventory_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        }
    }

    "usage" {
        Write-Host "$(Get-Date) INFO: Fetching license usage analysis..."
        $inactiveDaysInt = [int]$InactiveDays
        $cutoffDate = (Get-Date).AddDays(-$inactiveDaysInt).ToString("yyyy-MM-ddT00:00:00Z")

        # Get users with licenses
        $userUri = "https://graph.microsoft.com/v1.0/users?`$select=id,userPrincipalName,displayName,signInActivity,assignedLicenses&`$top=999"
        $users = Invoke-GraphRequest -Uri $userUri -AccessToken $accessToken -MaxPages 200
        Write-Host "$(Get-Date) INFO: Retrieved $($users.Count) user(s)."

        $inactiveUsers = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($user in $users) {
            if ($user.assignedLicenses.Count -eq 0) { continue }
            $lastSignIn = $user.signInActivity.lastSignInDateTime
            if (-not $lastSignIn -or [datetime]$lastSignIn -lt [datetime]$cutoffDate) {
                $inactiveUsers.Add([PSCustomObject]@{
                    UserPrincipalName = $user.userPrincipalName
                    DisplayName       = $user.displayName
                    LastSignIn        = $lastSignIn
                    LicenseCount      = $user.assignedLicenses.Count
                    DaysInactive      = if ($lastSignIn) {
                        ((Get-Date) - [datetime]$lastSignIn).Days
                    } else { "Never signed in" }
                })
            }
        }

        Write-Host "$(Get-Date) INFO: Found $($inactiveUsers.Count) inactive licensed user(s) (>$inactiveDaysInt days)."

        if ($inactiveUsers.Count -gt 0) {
            $baseName = "LicenseUsage_Inactive_${safeTenant}_${ts}"
            Export-AuditData -Data $inactiveUsers -OutputFolder $OutputFolder -BaseName $baseName
        }
    }

    "optimization" {
        Write-Host "$(Get-Date) INFO: Generating license optimization recommendations..."

        $skuUri = "https://graph.microsoft.com/v1.0/subscribedSkus"
        $skus = Invoke-GraphRequest -Uri $skuUri -AccessToken $accessToken -MaxPages 10

        $recommendations = [System.Collections.Generic.List[PSCustomObject]]::new()

        foreach ($sku in $skus) {
            $total = $sku.prepaidUnits.enabled
            $used = $sku.consumedUnits
            $available = $total - $used
            $usagePct = if ($total -gt 0) { [math]::Round(($used / $total) * 100, 1) } else { 0 }

            # Flag underutilized SKUs
            if ($total -gt 5 -and $usagePct -lt 50) {
                $recommendations.Add([PSCustomObject]@{
                    SKU           = $sku.skuPartNumber
                    Type          = "Underutilized"
                    Detail        = "$available of $total licenses unused ($usagePct% utilization)"
                    Recommendation = "Consider reducing license count or reassigning unused licenses"
                    PotentialSavings = $available
                })
            }

            # Flag suspended/warning units
            if ($sku.prepaidUnits.suspended -gt 0) {
                $recommendations.Add([PSCustomObject]@{
                    SKU           = $sku.skuPartNumber
                    Type          = "Suspended"
                    Detail        = "$($sku.prepaidUnits.suspended) suspended license(s)"
                    Recommendation = "Review subscription status — may need renewal"
                    PotentialSavings = 0
                })
            }
        }

        Write-Host "$(Get-Date) INFO: Generated $($recommendations.Count) recommendation(s)."

        if ($recommendations.Count -gt 0) {
            foreach ($rec in $recommendations) {
                Write-Host "$(Get-Date) INFO: [$($rec.Type)] $($rec.SKU) — $($rec.Detail)"
            }
            $baseName = "LicenseOptimization_${safeTenant}_${ts}"
            Export-AuditData -Data $recommendations -OutputFolder $OutputFolder -BaseName $baseName
        } else {
            Write-Host "$(Get-Date) INFO: No optimization recommendations — license utilization looks healthy."
        }
    }

    "overview" {
        Write-Host "$(Get-Date) INFO: Generating license overview..."
        $skuUri = "https://graph.microsoft.com/v1.0/subscribedSkus"
        $skus = Invoke-GraphRequest -Uri $skuUri -AccessToken $accessToken -MaxPages 10

        $totalLicenses = ($skus | Measure-Object -Property @{E={$_.prepaidUnits.enabled}} -Sum).Sum
        $totalAssigned = ($skus | Measure-Object -Property consumedUnits -Sum).Sum
        $totalAvailable = $totalLicenses - $totalAssigned

        $overview = [PSCustomObject]@{
            Tenant          = $Tenant
            ReportDate      = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
            TotalSKUs       = $skus.Count
            TotalLicenses   = $totalLicenses
            AssignedLicenses = $totalAssigned
            AvailableLicenses = $totalAvailable
            UtilizationPercent = if ($totalLicenses -gt 0) {
                [math]::Round(($totalAssigned / $totalLicenses) * 100, 1)
            } else { 0 }
        }

        $baseName = "LicenseOverview_${safeTenant}_${ts}"
        Export-AuditData -Data @($overview) -OutputFolder $OutputFolder -BaseName $baseName
        Write-Host "$(Get-Date) INFO: License Overview — Total: $totalLicenses, Assigned: $totalAssigned, Available: $totalAvailable ($($overview.UtilizationPercent)% utilization)"
    }

    "userDetail" {
        Write-Host "$(Get-Date) INFO: Fetching per-user license assignments..."
        $userUri = "https://graph.microsoft.com/v1.0/users?`$select=id,userPrincipalName,displayName,assignedLicenses,accountEnabled&`$top=999"
        $users = Invoke-GraphRequest -Uri $userUri -AccessToken $accessToken -MaxPages 200

        # Build SKU lookup
        $skuUri = "https://graph.microsoft.com/v1.0/subscribedSkus?`$select=skuId,skuPartNumber"
        $skuLookup = @{}
        $skus = Invoke-GraphRequest -Uri $skuUri -AccessToken $accessToken -MaxPages 10
        foreach ($sku in $skus) { $skuLookup[$sku.skuId] = $sku.skuPartNumber }

        $results = $users | Where-Object { $_.assignedLicenses.Count -gt 0 } | ForEach-Object {
            $licenseNames = ($_.assignedLicenses | ForEach-Object {
                if ($skuLookup.ContainsKey($_.skuId)) { $skuLookup[$_.skuId] }
                else { $_.skuId }
            }) -join "; "

            [PSCustomObject]@{
                UserPrincipalName = $_.userPrincipalName
                DisplayName       = $_.displayName
                AccountEnabled    = $_.accountEnabled
                LicenseCount      = $_.assignedLicenses.Count
                Licenses          = $licenseNames
            }
        }

        Write-Host "$(Get-Date) INFO: Found $(@($results).Count) licensed user(s)."

        if (@($results).Count -gt 0) {
            $baseName = "UserLicenseDetail_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        }
    }
}

Write-Host "$(Get-Date) INFO: License report complete."
