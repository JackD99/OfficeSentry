# Get-MessageTrace.ps1
# Pulls Exchange Online message trace data for a date range and optional
# sender/recipient.  Useful for investigating email flow during an incident.
# Note: Message trace is limited to the last 10 days via cmdlet;
# for older data, use Start-HistoricalSearch (up to 90 days).
# Requires: ExchangeOnlineManagement, ImportExcel (optional)

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [string] $StartDate  = ((Get-Date).AddDays(-7).ToString("yyyy-MM-dd")),
    [string] $EndDate    = ((Get-Date).ToString("yyyy-MM-dd")),
    [string] $TargetUser = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path (Split-Path $PSScriptRoot) "scripts\Shared.psm1"
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
$end   = [datetime]::Parse($EndDate).AddDays(1)

# Validate 10-day limit for real-time trace
$daySpan = ($end - $start).Days
if ($daySpan -gt 10) {
    Write-Host "$(Get-Date) WARN: Get-MessageTrace is limited to 10 days. Clamping start date." -ForegroundColor Yellow
    $start = $end.AddDays(-10)
}

Write-Host "$(Get-Date) INFO: Running message trace ($($start.ToString('yyyy-MM-dd')) to $($end.ToString('yyyy-MM-dd')))..."
if ($TargetUser) { Write-Host "$(Get-Date) INFO: Filtering for: $TargetUser" }

$allMessages = [System.Collections.Generic.List[PSCustomObject]]::new()
$page = 1
$pageSize = 5000

do {
    $params = @{
        StartDate = $start
        EndDate   = $end
        PageSize  = $pageSize
        Page      = $page
    }
    if ($TargetUser) {
        # Search as both sender and recipient
        $sentParams = $params.Clone()
        $sentParams.SenderAddress = $TargetUser
        $recvParams = $params.Clone()
        $recvParams.RecipientAddress = $TargetUser

        $sent = Get-MessageTrace @sentParams -ErrorAction SilentlyContinue
        $recv = Get-MessageTrace @recvParams -ErrorAction SilentlyContinue

        $results = @()
        if ($sent) { $results += $sent }
        if ($recv) { $results += $recv }
        # Deduplicate by MessageTraceId
        $results = $results | Sort-Object MessageTraceId -Unique
    } else {
        $results = Get-MessageTrace @params -ErrorAction SilentlyContinue
    }

    if ($null -eq $results -or $results.Count -eq 0) { break }

    foreach ($msg in $results) {
        $allMessages.Add([PSCustomObject]@{
            Received        = $msg.Received
            SenderAddress   = $msg.SenderAddress
            RecipientAddress = $msg.RecipientAddress
            Subject         = $msg.Subject
            Status          = $msg.Status
            FromIP          = $msg.FromIP
            ToIP            = $msg.ToIP
            Size            = $msg.Size
            MessageId       = $msg.MessageId
            MessageTraceId  = $msg.MessageTraceId
        })
    }

    Write-Host "$(Get-Date) INFO: Page $page — $($allMessages.Count) messages so far..."
    $page++

    # Get-MessageTrace returns max 5000 per page, paginate until empty
} while ($results.Count -eq $pageSize -and $page -le 100)

Write-Host "$(Get-Date) INFO: Disconnecting Exchange Online..."
Disconnect-ExchangeOnline -Confirm:$false

Write-Host "$(Get-Date) INFO: Total messages: $($allMessages.Count)"

if ($allMessages.Count -eq 0) {
    Write-Host "$(Get-Date) INFO: No messages found for the specified criteria."
    return
}

# Summaries
$senderSummary = $allMessages | Group-Object SenderAddress |
    Select-Object @{N='Sender';E={$_.Name}}, Count |
    Sort-Object Count -Descending | Select-Object -First 50

$statusSummary = $allMessages | Group-Object Status |
    Select-Object @{N='Status';E={$_.Name}}, Count |
    Sort-Object Count -Descending

$sheets = @(
    @{ Name = "Messages";      Data = $allMessages.ToArray() }
    @{ Name = "TopSenders";    Data = $senderSummary }
    @{ Name = "StatusSummary"; Data = @($statusSummary) }
)

$target = if ($TargetUser) { "_$($TargetUser -replace '@','_')" } else { "" }
Export-AuditData -OutputFolder $OutputFolder -FilePrefix "MessageTrace${target}" -Sheets $sheets

Write-Host "$(Get-Date) INFO: Done. $($allMessages.Count) messages traced."
