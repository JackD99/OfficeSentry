# Invoke-QuarantineAction.ps1
# Performs actions on quarantined messages (release, delete, deny).
# Requires: ExchangeOnlineManagement

#requires -Modules ExchangeOnlineManagement

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][ValidateSet("release", "delete", "deny")][string] $Action,
    [Parameter(Mandatory)][string] $MessageIds
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
    $ids = $MessageIds -split ","
    Write-Host "$(Get-Date) INFO: Performing '$Action' on $($ids.Count) message(s)..."

    $successCount = 0
    $failCount = 0

    foreach ($id in $ids) {
        $id = $id.Trim()
        try {
            switch ($Action) {
                "release" {
                    Release-QuarantineMessage -Identity $id -ReleaseToAll -Force
                }
                "delete" {
                    Delete-QuarantineMessage -Identity $id -Confirm:$false
                }
                "deny" {
                    Release-QuarantineMessage -Identity $id -ActionType Deny -Force
                }
            }
            $successCount++
            Write-Host "$(Get-Date) INFO: $Action succeeded for message: $id"
        } catch {
            $failCount++
            Write-Host "$(Get-Date) WARN: $Action failed for message $id — $($_.Exception.Message)"
        }
    }

    Write-Host "$(Get-Date) INFO: Action complete. Success: $successCount, Failed: $failCount"

} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
