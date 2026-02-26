# Get-IntuneDevices.ps1
# Retrieves Intune managed device data via Microsoft Graph API.
# Supports inventory, compliance, autopilot, policies, and overview reports.
# Requires: MSAL.PS, ImportExcel (optional)

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OutputFolder = ".",
    [Parameter(Mandatory)]
    [ValidateSet("inventory", "compliance", "autopilot", "policies", "overview")]
    [string] $ReportType,
    [string] $Platform = "",
    [string] $ComplianceFilter = "",
    [string] $PolicyType = "all"
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
        Write-Host "$(Get-Date) INFO: Fetching managed device inventory..."
        $uri = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices"

        $filterParts = @()
        if ($Platform) {
            $platformMap = @{
                "windows" = "windows"
                "macOS"   = "macOS"
                "iOS"     = "iOS"
                "android" = "android"
            }
            if ($platformMap.ContainsKey($Platform)) {
                $filterParts += "operatingSystem eq '$($platformMap[$Platform])'"
            }
        }
        if ($ComplianceFilter -and $ComplianceFilter -ne "all") {
            $filterParts += "complianceState eq '$ComplianceFilter'"
        }
        if ($filterParts.Count -gt 0) {
            $uri += "?`$filter=" + ($filterParts -join " and ")
        }

        $devices = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 200
        Write-Host "$(Get-Date) INFO: Found $($devices.Count) managed device(s)."

        if ($devices.Count -gt 0) {
            $results = $devices | Select-Object id, deviceName, userDisplayName,
                userPrincipalName, operatingSystem, osVersion, complianceState,
                managedDeviceOwnerType, enrolledDateTime, lastSyncDateTime,
                manufacturer, model, serialNumber, totalStorageSpaceInBytes,
                freeStorageSpaceInBytes, managementAgent, isEncrypted
            $baseName = "DeviceInventory_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        } else {
            Write-Host "$(Get-Date) INFO: No managed devices found."
        }
    }

    "compliance" {
        Write-Host "$(Get-Date) INFO: Fetching device compliance policies..."
        $policyUri = "https://graph.microsoft.com/v1.0/deviceManagement/deviceCompliancePolicies"
        $policies = Invoke-GraphRequest -Uri $policyUri -AccessToken $accessToken -MaxPages 50
        Write-Host "$(Get-Date) INFO: Found $($policies.Count) compliance policies."

        Write-Host "$(Get-Date) INFO: Fetching device compliance summary..."
        $devUri = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices?`$select=id,deviceName,complianceState,userPrincipalName,operatingSystem"
        $devices = Invoke-GraphRequest -Uri $devUri -AccessToken $accessToken -MaxPages 200

        $compliant = ($devices | Where-Object {$_.complianceState -eq "compliant"}).Count
        $nonCompliant = ($devices | Where-Object {$_.complianceState -eq "noncompliant"}).Count
        $unknown = ($devices | Where-Object {$_.complianceState -notin @("compliant", "noncompliant")}).Count

        Write-Host "$(Get-Date) INFO: Compliance Summary — Compliant: $compliant, Non-compliant: $nonCompliant, Other: $unknown"

        $summary = [PSCustomObject]@{
            Tenant          = $Tenant
            TotalDevices    = $devices.Count
            Compliant       = $compliant
            NonCompliant    = $nonCompliant
            Unknown         = $unknown
            TotalPolicies   = $policies.Count
            ReportDate      = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        }

        $baseName = "DeviceCompliance_${safeTenant}_${ts}"
        Export-AuditData -Data @($summary) -OutputFolder $OutputFolder -BaseName $baseName
    }

    "autopilot" {
        Write-Host "$(Get-Date) INFO: Fetching Autopilot devices..."
        $uri = "https://graph.microsoft.com/v1.0/deviceManagement/windowsAutopilotDeviceIdentities"
        $autopilotDevices = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 200
        Write-Host "$(Get-Date) INFO: Found $($autopilotDevices.Count) Autopilot device(s)."

        Write-Host "$(Get-Date) INFO: Fetching Autopilot deployment profiles..."
        $profileUri = "https://graph.microsoft.com/v1.0/deviceManagement/windowsAutopilotDeploymentProfiles"
        $profiles = Invoke-GraphRequest -Uri $profileUri -AccessToken $accessToken -MaxPages 50
        Write-Host "$(Get-Date) INFO: Found $($profiles.Count) deployment profile(s)."

        if ($autopilotDevices.Count -gt 0) {
            $results = $autopilotDevices | Select-Object id, serialNumber, manufacturer,
                model, groupTag, purchaseOrderIdentifier, enrollmentState,
                lastContactedDateTime, deploymentProfileAssignmentStatus
            $baseName = "AutopilotDevices_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        } else {
            Write-Host "$(Get-Date) INFO: No Autopilot devices found."
        }
    }

    "policies" {
        Write-Host "$(Get-Date) INFO: Fetching Intune policies (type: $PolicyType)..."
        $allPolicies = @()

        if ($PolicyType -in @("configuration", "all")) {
            Write-Host "$(Get-Date) INFO: Fetching configuration profiles..."
            $configUri = "https://graph.microsoft.com/v1.0/deviceManagement/deviceConfigurations"
            $configs = Invoke-GraphRequest -Uri $configUri -AccessToken $accessToken -MaxPages 100
            $allPolicies += $configs | Select-Object id, displayName,
                @{N="PolicyCategory";E={"Configuration"}}, createdDateTime, lastModifiedDateTime
        }

        if ($PolicyType -in @("compliance", "all")) {
            Write-Host "$(Get-Date) INFO: Fetching compliance policies..."
            $compUri = "https://graph.microsoft.com/v1.0/deviceManagement/deviceCompliancePolicies"
            $comps = Invoke-GraphRequest -Uri $compUri -AccessToken $accessToken -MaxPages 100
            $allPolicies += $comps | Select-Object id, displayName,
                @{N="PolicyCategory";E={"Compliance"}}, createdDateTime, lastModifiedDateTime
        }

        Write-Host "$(Get-Date) INFO: Found $($allPolicies.Count) total policies."
        $allPolicies | ConvertTo-Json -Depth 5
    }

    "overview" {
        Write-Host "$(Get-Date) INFO: Generating device overview..."
        $devUri = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices?`$select=id,operatingSystem,complianceState,managedDeviceOwnerType"
        $devices = Invoke-GraphRequest -Uri $devUri -AccessToken $accessToken -MaxPages 200

        $overview = [PSCustomObject]@{
            Tenant          = $Tenant
            ReportDate      = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
            TotalDevices    = $devices.Count
            Windows         = ($devices | Where-Object {$_.operatingSystem -eq "Windows"}).Count
            MacOS           = ($devices | Where-Object {$_.operatingSystem -eq "macOS"}).Count
            iOS             = ($devices | Where-Object {$_.operatingSystem -eq "iOS"}).Count
            Android         = ($devices | Where-Object {$_.operatingSystem -eq "Android"}).Count
            Compliant       = ($devices | Where-Object {$_.complianceState -eq "compliant"}).Count
            NonCompliant    = ($devices | Where-Object {$_.complianceState -eq "noncompliant"}).Count
            Corporate       = ($devices | Where-Object {$_.managedDeviceOwnerType -eq "company"}).Count
            Personal        = ($devices | Where-Object {$_.managedDeviceOwnerType -eq "personal"}).Count
        }

        $baseName = "DeviceOverview_${safeTenant}_${ts}"
        Export-AuditData -Data @($overview) -OutputFolder $OutputFolder -BaseName $baseName
        Write-Host "$(Get-Date) INFO: Device overview — Total: $($overview.TotalDevices), Compliant: $($overview.Compliant), Non-compliant: $($overview.NonCompliant)"
    }
}

Write-Host "$(Get-Date) INFO: Intune device report complete."
