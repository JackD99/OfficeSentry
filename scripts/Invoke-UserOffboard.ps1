# Invoke-UserOffboard.ps1
# Performs a complete user offboarding workflow for M365 tenants.
# Executes multiple steps in sequence: block sign-in, revoke sessions,
# convert mailbox to shared, set out-of-office, hide from GAL, remove licenses.
# Designed for MSP 3rd-line engineers handling user departures.
#
# Required API Permissions (Application):
#   - User.ReadWrite.All (Graph: block sign-in, revoke sessions, remove licenses)
#   - Directory.ReadWrite.All (Graph: license management)
#   - Exchange.ManageAsApp (Exchange: mailbox conversion, OOO, GAL)
#
# Usage:
#   .\Invoke-UserOffboard.ps1 -TargetUser leaver@contoso.com -Tenant contoso.onmicrosoft.com ...
#   .\Invoke-UserOffboard.ps1 -TargetUser leaver@contoso.com -OooMessage "No longer with company" ...

#requires -Modules MSAL.PS, ExchangeOnlineManagement

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $TargetUser,
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OooMessage      = "",
    [string] $ForwardTo       = "",
    [switch] $SkipConvertMailbox,
    [switch] $SkipRemoveLicenses,
    [switch] $SkipHideFromGal,
    [string] $OutputFolder    = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

# Track results for each step
$steps = [System.Collections.Generic.List[PSCustomObject]]::new()

function Add-StepResult {
    param([string]$Step, [bool]$Success, [string]$Message)
    $icon = if ($Success) { "OK" } else { "FAIL" }
    Write-Host "$(Get-Date) INFO: [$icon] $Step - $Message"
    $steps.Add([PSCustomObject]@{
        Step    = $Step
        Success = $Success
        Message = $Message
    })
}

Write-Host "$(Get-Date) INFO: ============================================"
Write-Host "$(Get-Date) INFO: USER OFFBOARDING: $TargetUser"
Write-Host "$(Get-Date) INFO: Tenant: $Tenant"
Write-Host "$(Get-Date) INFO: ============================================"

# ---- Step 1: Block sign-in (Graph API) ----
Write-Host ""
Write-Host "$(Get-Date) INFO: Step 1/6 - Blocking sign-in..."
try {
    $token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId `
        -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword
    $headers = @{
        Authorization  = "Bearer $($token.AccessToken)"
        "Content-Type" = "application/json"
    }

    $body = @{ accountEnabled = $false } | ConvertTo-Json
    Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/users/$TargetUser" `
        -Method PATCH -Headers $headers -Body $body
    Add-StepResult "Block Sign-In" $true "User sign-in disabled."
}
catch {
    Add-StepResult "Block Sign-In" $false "Failed: $($_.Exception.Message)"
}

# ---- Step 2: Revoke sessions (Graph API) ----
Write-Host ""
Write-Host "$(Get-Date) INFO: Step 2/6 - Revoking all sessions..."
try {
    Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/users/$TargetUser/revokeSignInSessions" `
        -Method POST -Headers $headers
    Add-StepResult "Revoke Sessions" $true "All active sessions invalidated."
}
catch {
    Add-StepResult "Revoke Sessions" $false "Failed: $($_.Exception.Message)"
}

# ---- Step 3: Remove licenses (Graph API) ----
if (-not $SkipRemoveLicenses) {
    Write-Host ""
    Write-Host "$(Get-Date) INFO: Step 3/6 - Removing licenses..."
    try {
        $licUri = "https://graph.microsoft.com/v1.0/users/$TargetUser/licenseDetails"
        $licenses = Invoke-GraphRequest -Uri $licUri -AccessToken $token.AccessToken
        $skuIds = @($licenses | ForEach-Object { $_.skuId })

        if ($skuIds.Count -gt 0) {
            $licBody = @{
                addLicenses    = @()
                removeLicenses = $skuIds
            } | ConvertTo-Json -Depth 3
            Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/users/$TargetUser/assignLicense" `
                -Method POST -Headers $headers -Body $licBody
            Add-StepResult "Remove Licenses" $true "Removed $($skuIds.Count) license(s)."
        }
        else {
            Add-StepResult "Remove Licenses" $true "No licenses to remove."
        }
    }
    catch {
        Add-StepResult "Remove Licenses" $false "Failed: $($_.Exception.Message)"
    }
}
else {
    Add-StepResult "Remove Licenses" $true "Skipped (as requested)."
}

# ---- Connect to Exchange Online for steps 4-6 ----
Write-Host ""
Write-Host "$(Get-Date) INFO: Connecting to Exchange Online..."
try {
    Connect-ExchangeOnline `
        -CertificateFilePath $CertificatePath `
        -CertificatePassword $CertificatePassword `
        -AppId               $ClientId `
        -Organization        $Tenant `
        -ShowBanner:$false
    $exchangeConnected = $true
}
catch {
    $exchangeConnected = $false
    Write-Host "$(Get-Date) ERROR: Failed to connect to Exchange Online: $_" -ForegroundColor Red
}

# ---- Step 4: Convert mailbox to shared (Exchange) ----
if (-not $SkipConvertMailbox -and $exchangeConnected) {
    Write-Host ""
    Write-Host "$(Get-Date) INFO: Step 4/6 - Converting mailbox to shared..."
    try {
        Set-Mailbox -Identity $TargetUser -Type Shared
        Add-StepResult "Convert to Shared" $true "Mailbox converted to shared."
    }
    catch {
        Add-StepResult "Convert to Shared" $false "Failed: $($_.Exception.Message)"
    }
}
else {
    $msg = if ($SkipConvertMailbox) { "Skipped (as requested)." } else { "Skipped (Exchange connection failed)." }
    Add-StepResult "Convert to Shared" (-not $SkipConvertMailbox -eq $false) $msg
}

# ---- Step 5: Set out-of-office (Exchange) ----
if ($exchangeConnected) {
    Write-Host ""
    Write-Host "$(Get-Date) INFO: Step 5/6 - Setting out-of-office reply..."
    if ($OooMessage) {
        try {
            Set-MailboxAutoReplyConfiguration -Identity $TargetUser `
                -AutoReplyState Enabled `
                -InternalMessage $OooMessage `
                -ExternalMessage $OooMessage
            Add-StepResult "Set Out-of-Office" $true "Auto-reply enabled."
        }
        catch {
            Add-StepResult "Set Out-of-Office" $false "Failed: $($_.Exception.Message)"
        }
    }
    else {
        Add-StepResult "Set Out-of-Office" $true "Skipped (no message provided)."
    }

    # Set forwarding if specified
    if ($ForwardTo) {
        try {
            Set-Mailbox -Identity $TargetUser `
                -ForwardingSmtpAddress "smtp:$ForwardTo" `
                -DeliverToMailboxAndForward $true
            Add-StepResult "Set Forwarding" $true "Mail forwarded to $ForwardTo."
        }
        catch {
            Add-StepResult "Set Forwarding" $false "Failed: $($_.Exception.Message)"
        }
    }
}

# ---- Step 6: Hide from GAL (Exchange) ----
if (-not $SkipHideFromGal -and $exchangeConnected) {
    Write-Host ""
    Write-Host "$(Get-Date) INFO: Step 6/6 - Hiding from Global Address List..."
    try {
        Set-Mailbox -Identity $TargetUser -HiddenFromAddressListsEnabled $true
        Add-StepResult "Hide from GAL" $true "User hidden from address list."
    }
    catch {
        Add-StepResult "Hide from GAL" $false "Failed: $($_.Exception.Message)"
    }
}
else {
    $msg = if ($SkipHideFromGal) { "Skipped (as requested)." } else { "Skipped (Exchange connection failed)." }
    Add-StepResult "Hide from GAL" (-not $SkipHideFromGal -eq $false) $msg
}

# Disconnect Exchange
if ($exchangeConnected) {
    Disconnect-ExchangeOnline -Confirm:$false 2>$null
}

# ---- Summary ----
Write-Host ""
Write-Host "$(Get-Date) INFO: ============================================"
Write-Host "$(Get-Date) INFO: OFFBOARDING SUMMARY FOR $TargetUser"
Write-Host "$(Get-Date) INFO: ============================================"

$successCount = ($steps | Where-Object { $_.Success }).Count
$failCount    = ($steps | Where-Object { -not $_.Success }).Count

foreach ($s in $steps) {
    $icon = if ($s.Success) { "[OK]  " } else { "[FAIL]" }
    Write-Host "  $icon $($s.Step): $($s.Message)"
}

Write-Host ""
Write-Host "$(Get-Date) INFO: Result: $successCount succeeded, $failCount failed out of $($steps.Count) steps."

if ($OutputFolder) {
    $sheets = @(@{ Name = "OffboardSummary"; Data = $steps.ToArray() })
    $target = $TargetUser -replace '@','_'
    Export-AuditData -OutputFolder $OutputFolder -FilePrefix "Offboard_${target}" -Sheets $sheets
}

Write-Host "$(Get-Date) INFO: Offboarding complete for $TargetUser."
