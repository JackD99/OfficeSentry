# Get-EmailProtectionPolicies.ps1
# Retrieves email protection policies from Exchange Online and Defender for Office 365.
# Supports anti-phishing, safe links, safe attachments, anti-malware, anti-spam,
# and spoof intelligence queries.
# Requires: ExchangeOnlineManagement, ImportExcel (optional)

#requires -Modules ExchangeOnlineManagement

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)]
    [ValidateSet("antiphishing", "safelinks", "safeattachments", "antimalware",
                 "antispam", "spoofintelligence", "all")]
    [string] $PolicyType
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
    $results = @{}

    if ($PolicyType -in @("antiphishing", "all")) {
        Write-Host "$(Get-Date) INFO: Fetching anti-phishing policies..."
        try {
            $results["AntiPhishing"] = Get-AntiPhishPolicy | Select-Object Name, Enabled,
                PhishThresholdLevel, EnableMailboxIntelligence,
                EnableMailboxIntelligenceProtection, EnableSpoofIntelligence,
                EnableFirstContactSafetyTips, EnableUnauthenticatedSender
        } catch {
            Write-Host "$(Get-Date) WARN: Could not fetch anti-phishing policies: $($_.Exception.Message)"
            $results["AntiPhishing"] = @()
        }
    }

    if ($PolicyType -in @("safelinks", "all")) {
        Write-Host "$(Get-Date) INFO: Fetching Safe Links policies..."
        try {
            $results["SafeLinks"] = Get-SafeLinksPolicy | Select-Object Name, IsEnabled,
                ScanUrls, EnableForInternalSenders, DeliverMessageAfterScan,
                DisableUrlRewrite, EnableOrganizationBranding
        } catch {
            Write-Host "$(Get-Date) WARN: Could not fetch Safe Links policies (may require Defender P1/P2): $($_.Exception.Message)"
            $results["SafeLinks"] = @()
        }
    }

    if ($PolicyType -in @("safeattachments", "all")) {
        Write-Host "$(Get-Date) INFO: Fetching Safe Attachments policies..."
        try {
            $results["SafeAttachments"] = Get-SafeAttachmentPolicy | Select-Object Name,
                Enable, Action, ActionOnError, Redirect, RedirectAddress
        } catch {
            Write-Host "$(Get-Date) WARN: Could not fetch Safe Attachments policies (may require Defender P1/P2): $($_.Exception.Message)"
            $results["SafeAttachments"] = @()
        }
    }

    if ($PolicyType -in @("antimalware", "all")) {
        Write-Host "$(Get-Date) INFO: Fetching anti-malware policies..."
        try {
            $results["AntiMalware"] = Get-MalwareFilterPolicy | Select-Object Name,
                EnableFileFilter, FileTypes, ZapEnabled,
                EnableInternalSenderAdminNotifications, EnableExternalSenderAdminNotifications
        } catch {
            Write-Host "$(Get-Date) WARN: Could not fetch anti-malware policies: $($_.Exception.Message)"
            $results["AntiMalware"] = @()
        }
    }

    if ($PolicyType -in @("antispam", "all")) {
        Write-Host "$(Get-Date) INFO: Fetching anti-spam policies..."
        try {
            $results["AntiSpam"] = Get-HostedContentFilterPolicy | Select-Object Name,
                SpamAction, HighConfidenceSpamAction, PhishSpamAction,
                HighConfidencePhishAction, BulkSpamAction, BulkThreshold,
                QuarantineRetentionPeriod, EnableEndUserSpamNotifications
        } catch {
            Write-Host "$(Get-Date) WARN: Could not fetch anti-spam policies: $($_.Exception.Message)"
            $results["AntiSpam"] = @()
        }
    }

    if ($PolicyType -in @("spoofintelligence", "all")) {
        Write-Host "$(Get-Date) INFO: Fetching spoof intelligence..."
        try {
            $results["SpoofIntelligence"] = Get-SpoofIntelligenceInsight | Select-Object SpoofedUser,
                SendingInfrastructure, MessageCount, LastSeen, SpoofType, Action
        } catch {
            Write-Host "$(Get-Date) WARN: Could not fetch spoof intelligence: $($_.Exception.Message)"
            $results["SpoofIntelligence"] = @()
        }
    }

    Write-Host "$(Get-Date) INFO: Email protection policy report complete."
    $results | ConvertTo-Json -Depth 10

} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
