# Get-QuarantineMessages.ps1
# Retrieves quarantined messages from Exchange Online Protection.
# Shows spam, phishing, malware, and high confidence phishing quarantine.
# Requires: ExchangeOnlineManagement, ImportExcel (optional)

#requires -Modules ExchangeOnlineManagement

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OutputFolder = ".",
    [string] $QuarantineType = "all",
    [string] $StartDate = ((Get-Date).AddDays(-30).ToString("yyyy-MM-dd")),
    [string] $EndDate   = ((Get-Date).ToString("yyyy-MM-dd"))
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
    Write-Host "$(Get-Date) INFO: Fetching quarantine messages ($StartDate to $EndDate)..."

    $params = @{
        StartDate = [datetime]$StartDate
        EndDate   = [datetime]$EndDate
        PageSize  = 200
    }

    if ($QuarantineType -ne "all") {
        $params["Type"] = $QuarantineType
    }

    $messages = [System.Collections.Generic.List[PSCustomObject]]::new()
    $page = 1

    do {
        Write-Host "$(Get-Date) INFO: Fetching page $page..."
        $batch = Get-QuarantineMessage @params
        if ($batch) {
            $messages.AddRange($batch)
            if ($batch.Count -lt 200) { break }
            $page++
        }
    } while ($batch -and $batch.Count -eq 200 -and $page -le 50)

    Write-Host "$(Get-Date) INFO: Retrieved $($messages.Count) quarantined messages."

    if ($messages.Count -eq 0) {
        Write-Host "$(Get-Date) INFO: No quarantined messages found."
        return
    }

    $results = $messages | Select-Object ReceivedTime, SenderAddress, RecipientAddress,
        Subject, Type, ReleaseStatus, PolicyType, SystemTags, MessageId,
        @{N="QuarantineId"; E={$_.Identity}}

    $safeTenant = ($Tenant -replace '[^\w\-\.]', '_')
    $ts = Get-Date -Format "yyyyMMdd-HHmm"
    $baseName = "Quarantine_${safeTenant}_${ts}"

    Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName

    Write-Host "$(Get-Date) INFO: Quarantine report complete."

} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
