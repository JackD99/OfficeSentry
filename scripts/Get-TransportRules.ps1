# Get-TransportRules.ps1
# Retrieves Exchange transport (mail flow) rules for a tenant.
# Useful for auditing email routing, DLP, and security configurations.
# Requires: ExchangeOnlineManagement, ImportExcel (optional)

#requires -Modules ExchangeOnlineManagement

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Connecting to Exchange Online..."
Connect-ExchangeOnline -CertificateFilePath $CertificatePath `
    -CertificatePassword (ConvertTo-SecureString $CertificatePassword -AsPlainText -Force) `
    -AppId $ClientId -Organization $Tenant -ShowBanner:$false

try {
    Write-Host "$(Get-Date) INFO: Fetching transport rules..."
    $rules = Get-TransportRule

    if (-not $rules -or $rules.Count -eq 0) {
        Write-Host "$(Get-Date) INFO: No transport rules found."
        $result = @{rules = @(); count = 0}
    } else {
        Write-Host "$(Get-Date) INFO: Found $($rules.Count) transport rule(s)."

        $result = @{
            count = $rules.Count
            rules = $rules | ForEach-Object {
                @{
                    Name           = $_.Name
                    Priority       = $_.Priority
                    State          = $_.State
                    Mode           = $_.Mode
                    Description    = $_.Description
                    FromScope      = $_.FromScope
                    SentToScope    = $_.SentToScope
                    Conditions     = ($_.Conditions | Out-String).Trim()
                    Actions        = ($_.Actions | Out-String).Trim()
                    WhenChanged    = $_.WhenChangedUTC
                }
            }
        }
    }

    Write-Host "$(Get-Date) INFO: Transport rules report complete."
    $result | ConvertTo-Json -Depth 10

} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
