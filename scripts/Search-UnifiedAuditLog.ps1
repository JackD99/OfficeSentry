# Search-UnifiedAuditLog.ps1
# Searches the Microsoft 365 Unified Audit Log for a given date range and
# optional target user.  Essential for breach investigation and incident response.
# Exports results to XLSX/CSV with sheets for each record type.
# Requires: ExchangeOnlineManagement, ImportExcel (optional)

#requires -Modules ExchangeOnlineManagement

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [string] $StartDate = ((Get-Date).AddDays(-7).ToString("yyyy-MM-dd")),
    [string] $EndDate   = ((Get-Date).ToString("yyyy-MM-dd")),
    [string] $TargetUser = "",
    [string[]] $Operations = @(),
    [int] $MaxResults = 50000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Connecting to Exchange Online..."
Connect-ExchangeOnline `
    -CertificateFilePath $CertificatePath `
    -CertificatePassword $CertificatePassword `
    -AppId               $ClientId `
    -Organization        $Tenant `
    -ShowBanner:$false

$start = [datetime]::Parse($StartDate)
$end   = [datetime]::Parse($EndDate).AddDays(1)  # include the end date

Write-Host "$(Get-Date) INFO: Searching UAL from $StartDate to $EndDate..."
if ($TargetUser) { Write-Host "$(Get-Date) INFO: Filtering for user: $TargetUser" }

$allRecords = [System.Collections.Generic.List[PSCustomObject]]::new()
$sessionId  = "UALSearch_$(Get-Date -Format 'yyyyMMddHHmmss')"
$batchSize  = 5000
$totalFound = 0
$retries    = 0

do {
    $params = @{
        StartDate   = $start
        EndDate     = $end
        SessionId   = $sessionId
        SessionCommand = "ReturnLargeSet"
        ResultSize  = $batchSize
    }
    if ($TargetUser) { $params.UserIds = $TargetUser }
    if ($Operations.Count -gt 0) { $params.Operations = $Operations }

    try {
        $results = Search-UnifiedAuditLog @params
    }
    catch {
        $retries++
        if ($retries -gt 3) { throw }
        Write-Host "$(Get-Date) WARN: Throttled/error, retrying in $($retries * 5)s..."
        Start-Sleep -Seconds ($retries * 5)
        continue
    }

    if ($null -eq $results -or $results.Count -eq 0) { break }

    $totalFound += $results.Count
    Write-Host "$(Get-Date) INFO: Retrieved $totalFound records so far..."

    foreach ($rec in $results) {
        $auditData = $null
        try { $auditData = $rec.AuditData | ConvertFrom-Json } catch {}

        $allRecords.Add([PSCustomObject]@{
            CreationDate = $rec.CreationDate
            UserIds      = $rec.UserIds
            Operations   = $rec.Operations
            RecordType   = $rec.RecordType
            ResultIndex  = $rec.ResultIndex
            ResultCount  = $rec.ResultCount
            ClientIP     = if ($auditData) { $auditData.ClientIP } else { "" }
            ObjectId     = if ($auditData) { $auditData.ObjectId } else { "" }
            ItemType     = if ($auditData) { $auditData.ItemType } else { "" }
            Workload     = if ($auditData) { $auditData.Workload } else { "" }
            AuditData    = $rec.AuditData
        })
    }

    if ($totalFound -ge $MaxResults) {
        Write-Host "$(Get-Date) WARN: Reached max result limit ($MaxResults)."
        break
    }

} while ($results.Count -eq $batchSize)

Write-Host "$(Get-Date) INFO: Disconnecting Exchange Online..."
Disconnect-ExchangeOnline -Confirm:$false

Write-Host "$(Get-Date) INFO: Total records: $($allRecords.Count)"

if ($allRecords.Count -eq 0) {
    Write-Host "$(Get-Date) INFO: No audit log records found for the specified criteria."
    return
}

# Group by operation for summary
$summary = $allRecords | Group-Object Operations | Sort-Object Count -Descending |
    Select-Object @{N='Operation';E={$_.Name}}, Count

# Export
$sheets = @(
    @{ Name = "AllRecords"; Data = $allRecords.ToArray() }
    @{ Name = "Summary";    Data = $summary }
)

$target = if ($TargetUser) { "_$($TargetUser -replace '@','_')" } else { "" }
$prefix = "UAL${target}"

Export-AuditData -OutputFolder $OutputFolder -FilePrefix $prefix -Sheets $sheets

Write-Host "$(Get-Date) INFO: Done. $($allRecords.Count) audit log records exported."
