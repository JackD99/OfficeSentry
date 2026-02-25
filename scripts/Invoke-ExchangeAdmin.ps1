# Invoke-ExchangeAdmin.ps1
# Exchange Online administrative operations for MSP helpdesk staff.
# Supports: mailbox size reports, distribution group management,
#           mailbox permissions, and quota management.
#
# Required API Permissions (Application):
#   - Exchange.ManageAsApp (Exchange Online application permission)
#   - Exchange Administrator role assignment

#requires -Modules ExchangeOnlineManagement

[CmdletBinding()]
param (
    [Parameter(Mandatory)][ValidateSet(
        "mailbox_sizes","distro_members",
        "add_distro_member","remove_distro_member",
        "set_mailbox_perms","remove_mailbox_perms",
        "set_mailbox_quota"
    )]
    [string] $Action,
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OutputFolder    = "",
    [string] $TargetUser      = "",
    [string] $GroupName       = "",
    [string] $DelegateUser    = "",
    [ValidateSet("FullAccess","SendAs","SendOnBehalf","")]
    [string] $PermissionType  = "",
    [string] $QuotaWarningGB  = "",
    [string] $QuotaLimitGB    = ""
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

Write-Host "$(Get-Date) INFO: Performing Exchange action '$Action'..."

try {
    switch ($Action) {
        "mailbox_sizes" {
            Write-Host "$(Get-Date) INFO: Retrieving mailbox statistics..."
            if ($TargetUser) {
                $mailboxes = @(Get-EXOMailbox -Identity $TargetUser -Properties DisplayName, UserPrincipalName, RecipientTypeDetails)
            } else {
                $mailboxes = Get-EXOMailbox -ResultSize Unlimited -Properties DisplayName, UserPrincipalName, RecipientTypeDetails
            }

            $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
            $counter = 0
            $total = $mailboxes.Count
            Write-Host "$(Get-Date) INFO: Processing $total mailboxes..."

            foreach ($mbx in $mailboxes) {
                $counter++
                Write-Progress -Activity "Getting mailbox statistics" `
                    -Status "$counter of $total : $($mbx.UserPrincipalName)" `
                    -PercentComplete ([math]::Round(($counter / $total) * 100))

                try {
                    $stats = Get-EXOMailboxStatistics -Identity $mbx.UserPrincipalName -ErrorAction SilentlyContinue
                    $sizeStr = if ($stats.TotalItemSize) { $stats.TotalItemSize.ToString() } else { "0 B" }
                    # Parse size to MB for sorting
                    $sizeBytes = 0
                    if ($stats.TotalItemSize -and $stats.TotalItemSize.Value) {
                        $sizeBytes = $stats.TotalItemSize.Value.ToBytes()
                    }
                    $sizeMB = [math]::Round($sizeBytes / 1MB, 2)

                    $rows.Add([PSCustomObject]@{
                        DisplayName       = $mbx.DisplayName
                        UserPrincipalName = $mbx.UserPrincipalName
                        MailboxType       = $mbx.RecipientTypeDetails
                        TotalSize         = $sizeStr
                        SizeMB            = $sizeMB
                        ItemCount         = $stats.ItemCount
                        DeletedItemCount  = $stats.DeletedItemCount
                        LastLogonTime     = $stats.LastLogonTime
                    })
                }
                catch {
                    Write-Host "$(Get-Date) WARN: Could not get stats for $($mbx.UserPrincipalName): $_" -ForegroundColor Yellow
                }
            }
            Write-Progress -Activity "Getting mailbox statistics" -Completed

            # Sort by size descending
            $sorted = $rows | Sort-Object -Property SizeMB -Descending

            Write-Host "$(Get-Date) INFO: Processed $($rows.Count) mailboxes."
            if ($OutputFolder) {
                $sheets = @(@{ Name = "MailboxSizes"; Data = @($sorted) })
                Export-AuditData -OutputFolder $OutputFolder -FilePrefix "MailboxSizes" -Sheets $sheets
            }
        }

        "distro_members" {
            if (-not $GroupName) { throw "GroupName is required for distro_members action." }
            Write-Host "$(Get-Date) INFO: Retrieving members of distribution group '$GroupName'..."

            $group = Get-DistributionGroup -Identity $GroupName -ErrorAction Stop
            $members = Get-DistributionGroupMember -Identity $GroupName -ResultSize Unlimited

            $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
            foreach ($m in $members) {
                $rows.Add([PSCustomObject]@{
                    DisplayName       = $m.DisplayName
                    PrimarySmtpAddress = $m.PrimarySmtpAddress
                    RecipientType     = $m.RecipientTypeDetails
                })
            }

            Write-Host "$(Get-Date) INFO: Found $($rows.Count) members in '$($group.DisplayName)'."
            if ($OutputFolder) {
                $safeName = $group.DisplayName -replace '[^\w]', '_'
                $sheets = @(@{ Name = "Members"; Data = $rows.ToArray() })
                Export-AuditData -OutputFolder $OutputFolder -FilePrefix "DistroGroup_${safeName}" -Sheets $sheets
            }
        }

        "add_distro_member" {
            if (-not $GroupName) { throw "GroupName is required." }
            if (-not $TargetUser) { throw "TargetUser is required." }
            Add-DistributionGroupMember -Identity $GroupName -Member $TargetUser
            Write-Host "$(Get-Date) INFO: SUCCESS - Added '$TargetUser' to distribution group '$GroupName'."
        }

        "remove_distro_member" {
            if (-not $GroupName) { throw "GroupName is required." }
            if (-not $TargetUser) { throw "TargetUser is required." }
            Remove-DistributionGroupMember -Identity $GroupName -Member $TargetUser -Confirm:$false
            Write-Host "$(Get-Date) INFO: SUCCESS - Removed '$TargetUser' from distribution group '$GroupName'."
        }

        "set_mailbox_perms" {
            if (-not $TargetUser) { throw "TargetUser (mailbox) is required." }
            if (-not $DelegateUser) { throw "DelegateUser is required." }
            if (-not $PermissionType) { throw "PermissionType is required." }

            switch ($PermissionType) {
                "FullAccess" {
                    Add-MailboxPermission -Identity $TargetUser -User $DelegateUser `
                        -AccessRights FullAccess -InheritanceType All -AutoMapping $true
                    Write-Host "$(Get-Date) INFO: SUCCESS - Granted Full Access on '$TargetUser' to '$DelegateUser'."
                }
                "SendAs" {
                    Add-RecipientPermission -Identity $TargetUser -Trustee $DelegateUser `
                        -AccessRights SendAs -Confirm:$false
                    Write-Host "$(Get-Date) INFO: SUCCESS - Granted Send As on '$TargetUser' to '$DelegateUser'."
                }
                "SendOnBehalf" {
                    Set-Mailbox -Identity $TargetUser -GrantSendOnBehalfTo @{Add = $DelegateUser}
                    Write-Host "$(Get-Date) INFO: SUCCESS - Granted Send on Behalf on '$TargetUser' to '$DelegateUser'."
                }
            }
        }

        "remove_mailbox_perms" {
            if (-not $TargetUser) { throw "TargetUser (mailbox) is required." }
            if (-not $DelegateUser) { throw "DelegateUser is required." }
            if (-not $PermissionType) { throw "PermissionType is required." }

            switch ($PermissionType) {
                "FullAccess" {
                    Remove-MailboxPermission -Identity $TargetUser -User $DelegateUser `
                        -AccessRights FullAccess -InheritanceType All -Confirm:$false
                    Write-Host "$(Get-Date) INFO: SUCCESS - Removed Full Access on '$TargetUser' from '$DelegateUser'."
                }
                "SendAs" {
                    Remove-RecipientPermission -Identity $TargetUser -Trustee $DelegateUser `
                        -AccessRights SendAs -Confirm:$false
                    Write-Host "$(Get-Date) INFO: SUCCESS - Removed Send As on '$TargetUser' from '$DelegateUser'."
                }
                "SendOnBehalf" {
                    Set-Mailbox -Identity $TargetUser -GrantSendOnBehalfTo @{Remove = $DelegateUser}
                    Write-Host "$(Get-Date) INFO: SUCCESS - Removed Send on Behalf on '$TargetUser' from '$DelegateUser'."
                }
            }
        }

        "set_mailbox_quota" {
            if (-not $TargetUser) { throw "TargetUser is required." }
            $params = @{ Identity = $TargetUser }
            if ($QuotaWarningGB) {
                $params.IssueWarningQuota = "${QuotaWarningGB}GB"
            }
            if ($QuotaLimitGB) {
                $params.ProhibitSendQuota = "${QuotaLimitGB}GB"
                $params.ProhibitSendReceiveQuota = "${QuotaLimitGB}GB"
            }
            Set-Mailbox @params
            Write-Host "$(Get-Date) INFO: SUCCESS - Mailbox quota updated for '$TargetUser'."
            if ($QuotaWarningGB) { Write-Host "  Warning at: ${QuotaWarningGB} GB" }
            if ($QuotaLimitGB) { Write-Host "  Send limit: ${QuotaLimitGB} GB" }
        }
    }
}
catch {
    Write-Host "$(Get-Date) ERROR: Action '$Action' failed: $_" -ForegroundColor Red
}
finally {
    Disconnect-ExchangeOnline -Confirm:$false 2>$null
}

Write-Host "$(Get-Date) INFO: Exchange action '$Action' completed."
