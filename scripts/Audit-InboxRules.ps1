# Audit-InboxRules.ps1
# Audits inbox rules across all mailboxes (or a specific user) looking for
# suspicious forwarding rules — the #1 persistence mechanism in BEC attacks.
# Flags: external forwarding, redirect, delete-after-forward, keyword-based rules.
# Requires: ExchangeOnlineManagement, ImportExcel (optional)

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [string] $TargetUser = "",
    [string] $StartDate  = "",
    [string] $EndDate    = ""
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

# Get mailboxes to audit
if ($TargetUser) {
    $mailboxes = @(Get-EXOMailbox -Identity $TargetUser -Properties DisplayName, UserPrincipalName)
    Write-Host "$(Get-Date) INFO: Auditing inbox rules for $TargetUser"
} else {
    Write-Host "$(Get-Date) INFO: Retrieving all mailboxes..."
    $mailboxes = Get-EXOMailbox -ResultSize Unlimited -Properties DisplayName, UserPrincipalName
    Write-Host "$(Get-Date) INFO: Found $($mailboxes.Count) mailboxes."
}

$allRules      = [System.Collections.Generic.List[PSCustomObject]]::new()
$suspiciousAll = [System.Collections.Generic.List[PSCustomObject]]::new()
$counter       = 0
$total         = $mailboxes.Count

foreach ($mbx in $mailboxes) {
    $counter++
    Write-Progress -Activity "Auditing inbox rules" `
                   -Status "Mailbox $counter of $total : $($mbx.UserPrincipalName)" `
                   -PercentComplete ([math]::Round(($counter / $total) * 100))

    try {
        $rules = Get-InboxRule -Mailbox $mbx.UserPrincipalName -ErrorAction SilentlyContinue
    }
    catch {
        Write-Host "$(Get-Date) WARN: Could not read rules for $($mbx.UserPrincipalName): $_" -ForegroundColor Yellow
        continue
    }

    if (-not $rules) { continue }

    foreach ($rule in $rules) {
        $flags = [System.Collections.Generic.List[string]]::new()

        if ($rule.ForwardTo)                { $flags.Add("ForwardTo") }
        if ($rule.ForwardAsAttachmentTo)    { $flags.Add("ForwardAsAttachment") }
        if ($rule.RedirectTo)               { $flags.Add("Redirect") }
        if ($rule.DeleteMessage -eq $true)  { $flags.Add("DeleteMessage") }
        if ($rule.MoveToFolder -match 'RSS|Deleted|Junk') { $flags.Add("HideToFolder") }
        if ($rule.MarkAsRead -eq $true -and ($rule.ForwardTo -or $rule.RedirectTo)) {
            $flags.Add("MarkAsRead+Forward")
        }
        if ($rule.SubjectContainsWords -or $rule.BodyContainsWords) {
            $flags.Add("KeywordFilter")
        }

        $isSuspicious = $flags.Count -gt 0

        $row = [PSCustomObject]@{
            Mailbox         = $mbx.UserPrincipalName
            DisplayName     = $mbx.DisplayName
            RuleName        = $rule.Name
            RuleEnabled     = $rule.Enabled
            Priority        = $rule.Priority
            ForwardTo       = ($rule.ForwardTo -join "; ")
            ForwardAsAttach = ($rule.ForwardAsAttachmentTo -join "; ")
            RedirectTo      = ($rule.RedirectTo -join "; ")
            DeleteMessage   = $rule.DeleteMessage
            MoveToFolder    = $rule.MoveToFolder
            MarkAsRead      = $rule.MarkAsRead
            SubjectKeywords = ($rule.SubjectContainsWords -join "; ")
            BodyKeywords    = ($rule.BodyContainsWords -join "; ")
            Flags           = ($flags -join ", ")
            IsSuspicious    = $isSuspicious
        }

        $allRules.Add($row)
        if ($isSuspicious) { $suspiciousAll.Add($row) }
    }
}

Write-Progress -Activity "Auditing inbox rules" -Completed
Disconnect-ExchangeOnline -Confirm:$false

Write-Host "$(Get-Date) INFO: Total rules: $($allRules.Count), Suspicious: $($suspiciousAll.Count)"

$sheets = @(
    @{ Name = "SuspiciousRules"; Data = $suspiciousAll.ToArray() }
    @{ Name = "AllRules";        Data = $allRules.ToArray() }
)

$target = if ($TargetUser) { "_$($TargetUser -replace '@','_')" } else { "" }
Export-AuditData -OutputFolder $OutputFolder -FilePrefix "InboxRules${target}" -Sheets $sheets

Write-Host "$(Get-Date) INFO: Done. $($allRules.Count) rules audited, $($suspiciousAll.Count) flagged."
