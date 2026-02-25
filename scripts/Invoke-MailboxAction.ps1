# Invoke-MailboxAction.ps1
# Performs mailbox management actions via Exchange Online Management.
# Supports: convert to shared/regular, set/clear out-of-office, set/remove forwarding.
# Designed for MSP helpdesk staff managing multiple M365 tenants.
#
# Required API Permissions (Application):
#   - Exchange.ManageAsApp (Exchange Online application permission)
#   - Exchange Administrator role assignment
#
# Usage:
#   .\Invoke-MailboxAction.ps1 -Action convert_shared -TargetUser user@contoso.com ...
#   .\Invoke-MailboxAction.ps1 -Action set_ooo -TargetUser user@contoso.com -OooMessage "On leave" ...
#   .\Invoke-MailboxAction.ps1 -Action set_forwarding -TargetUser user@contoso.com -ForwardTo admin@contoso.com ...

#requires -Modules ExchangeOnlineManagement

[CmdletBinding()]
param (
    [Parameter(Mandatory)][ValidateSet(
        "convert_shared","convert_regular",
        "set_ooo","clear_ooo",
        "set_forwarding","remove_forwarding",
        "hide_from_gal","unhide_from_gal"
    )]
    [string] $Action,
    [Parameter(Mandatory)][string] $TargetUser,
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OooMessage       = "",
    [string] $OooExternalMsg   = "",
    [string] $OooStartDate     = "",
    [string] $OooEndDate       = "",
    [string] $ForwardTo        = "",
    [switch] $KeepCopy
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Connecting to Exchange Online for tenant $Tenant..."
Connect-ExchangeOnline `
    -CertificateFilePath $CertificatePath `
    -CertificatePassword $CertificatePassword `
    -AppId               $ClientId `
    -Organization        $Tenant `
    -ShowBanner:$false

Write-Host "$(Get-Date) INFO: Performing action '$Action' on mailbox '$TargetUser'..."

$result = @{ ok = $false; action = $Action; user = $TargetUser }

try {
    switch ($Action) {
        "convert_shared" {
            Set-Mailbox -Identity $TargetUser -Type Shared
            Write-Host "$(Get-Date) INFO: SUCCESS - Mailbox '$TargetUser' converted to Shared."
            $result.ok = $true
            $result.message = "Mailbox converted to Shared successfully."
        }

        "convert_regular" {
            Set-Mailbox -Identity $TargetUser -Type Regular
            Write-Host "$(Get-Date) INFO: SUCCESS - Mailbox '$TargetUser' converted to Regular."
            $result.ok = $true
            $result.message = "Mailbox converted to Regular (user) mailbox successfully."
        }

        "set_ooo" {
            $params = @{
                Identity       = $TargetUser
                AutoReplyState = "Enabled"
            }
            if ($OooMessage) {
                $params.InternalMessage = $OooMessage
                $params.ExternalMessage = if ($OooExternalMsg) { $OooExternalMsg } else { $OooMessage }
            }
            if ($OooStartDate -and $OooEndDate) {
                $params.AutoReplyState = "Scheduled"
                $params.StartTime      = [datetime]::Parse($OooStartDate)
                $params.EndTime        = [datetime]::Parse($OooEndDate)
            }
            Set-MailboxAutoReplyConfiguration @params
            Write-Host "$(Get-Date) INFO: SUCCESS - Out-of-office set for '$TargetUser'."
            $result.ok = $true
            $result.message = "Out-of-office auto-reply enabled."
        }

        "clear_ooo" {
            Set-MailboxAutoReplyConfiguration -Identity $TargetUser -AutoReplyState Disabled
            Write-Host "$(Get-Date) INFO: SUCCESS - Out-of-office cleared for '$TargetUser'."
            $result.ok = $true
            $result.message = "Out-of-office auto-reply disabled."
        }

        "set_forwarding" {
            if (-not $ForwardTo) {
                throw "ForwardTo address is required for set_forwarding action."
            }
            $params = @{
                Identity               = $TargetUser
                ForwardingSmtpAddress  = "smtp:$ForwardTo"
                DeliverToMailboxAndForward = [bool]$KeepCopy
            }
            Set-Mailbox @params
            $keepMsg = if ($KeepCopy) { " (keeping copy in mailbox)" } else { "" }
            Write-Host "$(Get-Date) INFO: SUCCESS - Forwarding set for '$TargetUser' -> '$ForwardTo'$keepMsg."
            $result.ok = $true
            $result.message = "Mail forwarding set to $ForwardTo$keepMsg."
        }

        "remove_forwarding" {
            Set-Mailbox -Identity $TargetUser `
                -ForwardingSmtpAddress $null `
                -ForwardingAddress $null `
                -DeliverToMailboxAndForward $false
            Write-Host "$(Get-Date) INFO: SUCCESS - Forwarding removed for '$TargetUser'."
            $result.ok = $true
            $result.message = "Mail forwarding removed."
        }

        "hide_from_gal" {
            Set-Mailbox -Identity $TargetUser -HiddenFromAddressListsEnabled $true
            Write-Host "$(Get-Date) INFO: SUCCESS - '$TargetUser' hidden from Global Address List."
            $result.ok = $true
            $result.message = "User hidden from Global Address List."
        }

        "unhide_from_gal" {
            Set-Mailbox -Identity $TargetUser -HiddenFromAddressListsEnabled $false
            Write-Host "$(Get-Date) INFO: SUCCESS - '$TargetUser' visible in Global Address List."
            $result.ok = $true
            $result.message = "User is now visible in Global Address List."
        }
    }
}
catch {
    Write-Host "$(Get-Date) ERROR: Action '$Action' failed: $_" -ForegroundColor Red
    $result.error = $_.Exception.Message
}
finally {
    Disconnect-ExchangeOnline -Confirm:$false 2>$null
}

# Output JSON result on last line for portal parsing
$result | ConvertTo-Json -Compress
Write-Host "$(Get-Date) INFO: Action '$Action' completed for '$TargetUser'."
