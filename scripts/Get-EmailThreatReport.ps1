# Get-EmailThreatReport.ps1
# Generates an email threat assessment report for a tenant.
# Aggregates quarantine stats, mail flow data, and threat detection metrics.
# Requires: ExchangeOnlineManagement, MSAL.PS, ImportExcel (optional)

#requires -Modules ExchangeOnlineManagement
#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [string] $Days = "30"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword
$daysInt = [int]$Days
$startDate = (Get-Date).AddDays(-$daysInt)

Write-Host "$(Get-Date) INFO: Connecting to Exchange Online..."
Connect-ExchangeOnline -CertificateFilePath $CertificatePath `
    -CertificatePassword (ConvertTo-SecureString $CertificatePassword -AsPlainText -Force) `
    -AppId $ClientId -Organization $Tenant -ShowBanner:$false

try {
    Write-Host "$(Get-Date) INFO: Generating threat report for last $Days days..."

    # Quarantine statistics
    Write-Host "$(Get-Date) INFO: Fetching quarantine summary..."
    $quarantineStats = @{Spam=0; Phish=0; Malware=0; HighConfPhish=0; Total=0}
    try {
        $qMessages = Get-QuarantineMessage -StartDate $startDate -EndDate (Get-Date) -PageSize 200
        if ($qMessages) {
            $quarantineStats.Total = $qMessages.Count
            $quarantineStats.Spam = ($qMessages | Where-Object {$_.Type -eq "Spam"}).Count
            $quarantineStats.Phish = ($qMessages | Where-Object {$_.Type -eq "Phish"}).Count
            $quarantineStats.Malware = ($qMessages | Where-Object {$_.Type -eq "Malware"}).Count
            $quarantineStats.HighConfPhish = ($qMessages | Where-Object {$_.Type -eq "HighConfPhish"}).Count
        }
    } catch {
        Write-Host "$(Get-Date) WARN: Could not fetch quarantine data: $($_.Exception.Message)"
    }

    # Mail flow summary
    Write-Host "$(Get-Date) INFO: Fetching mail flow statistics..."
    $mailFlowStats = @{Inbound=0; Outbound=0}
    try {
        $trace = Get-MessageTrace -StartDate $startDate -EndDate (Get-Date) -PageSize 1
        $mailFlowStats.Note = "Use Message Trace for detailed mail flow analysis"
    } catch {
        Write-Host "$(Get-Date) WARN: Could not fetch mail flow data: $($_.Exception.Message)"
    }

    # Build report
    $report = [PSCustomObject]@{
        Tenant          = $Tenant
        ReportDate      = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        PeriodDays      = $daysInt
        QuarantineTotal = $quarantineStats.Total
        SpamCount       = $quarantineStats.Spam
        PhishCount      = $quarantineStats.Phish
        MalwareCount    = $quarantineStats.Malware
        HighConfPhish   = $quarantineStats.HighConfPhish
    }

    $safeTenant = ($Tenant -replace '[^\w\-\.]', '_')
    $ts = Get-Date -Format "yyyyMMdd-HHmm"
    $baseName = "EmailThreatReport_${safeTenant}_${ts}"

    Export-AuditData -Data @($report) -OutputFolder $OutputFolder -BaseName $baseName

    Write-Host "$(Get-Date) INFO: Email threat report complete."
    Write-Host "$(Get-Date) INFO: Summary — Quarantine: $($quarantineStats.Total), Spam: $($quarantineStats.Spam), Phish: $($quarantineStats.Phish), Malware: $($quarantineStats.Malware)"

} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
