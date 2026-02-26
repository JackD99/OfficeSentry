# Invoke-DeviceAction.ps1
# Performs remote actions on Intune managed devices via Microsoft Graph.
# Supports sync, restart, retire, wipe, resetPasscode, remoteLock, etc.
# Requires: MSAL.PS

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $DeviceId,
    [Parameter(Mandatory)]
    [ValidateSet("sync", "restart", "retire", "wipe", "resetPasscode",
                 "remoteLock", "enableLostMode", "disableLostMode")]
    [string] $Action
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

# Map action to Graph API endpoint
$actionMap = @{
    "sync"             = "syncDevice"
    "restart"          = "rebootNow"
    "retire"           = "retire"
    "wipe"             = "wipe"
    "resetPasscode"    = "resetPasscode"
    "remoteLock"       = "remoteLock"
    "enableLostMode"   = "enableLostMode"
    "disableLostMode"  = "disableLostMode"
}

$graphAction = $actionMap[$Action]
$uri = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices/$DeviceId/$graphAction"

Write-Host "$(Get-Date) INFO: Performing '$Action' on device $DeviceId..."

$headers = @{
    "Authorization" = "Bearer $accessToken"
    "Content-Type"  = "application/json"
}

try {
    $body = "{}"
    if ($Action -eq "enableLostMode") {
        $body = @{
            message   = "This device has been reported lost."
            phoneNumber = ""
            footer    = "Contact your IT administrator."
        } | ConvertTo-Json
    }

    Invoke-RestMethod -Uri $uri -Method POST -Headers $headers -Body $body
    Write-Host "$(Get-Date) INFO: Action '$Action' successfully sent to device $DeviceId."
} catch {
    Write-Host "$(Get-Date) ERROR: Failed to perform '$Action' on device: $($_.Exception.Message)"
    throw
}
