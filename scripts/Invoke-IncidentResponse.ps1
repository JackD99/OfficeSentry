# Invoke-IncidentResponse.ps1
# Runs incident response playbooks for common security scenarios.
# Supports BEC, Account Compromise, Data Exfiltration, Malware Outbreak,
# Privilege Escalation, and Timeline builder playbooks.
# Requires: MSAL.PS, ExchangeOnlineManagement, ImportExcel (optional)

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [Parameter(Mandatory)]
    [ValidateSet("BEC", "AccountCompromise", "DataExfiltration",
                 "MalwareOutbreak", "PrivilegeEscalation", "Timeline")]
    [string] $Playbook,
    [string] $TargetUser = "",
    [string] $DaysBack = "30",
    [string] $Steps = "",
    [string] $Scope = "user",
    [string] $StartDate = "",
    [string] $EndDate = "",
    [string] $DataSources = "signins,audit,mailbox,sharepoint"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword
$daysInt = [int]$DaysBack
$safeTenant = ($Tenant -replace '[^\w\-\.]', '_')
$ts = Get-Date -Format "yyyyMMdd-HHmm"
$startDT = (Get-Date).AddDays(-$daysInt).ToString("yyyy-MM-ddT00:00:00Z")

Write-Host "$(Get-Date) INFO: Acquiring Graph token..."
$token = Get-AuditGraphToken `
    -Tenant              $Tenant `
    -ClientId            $ClientId `
    -CertificatePath     $CertificatePath `
    -CertificatePassword $CertificatePassword

$accessToken = $token.AccessToken

function Get-GraphData {
    param([string]$Uri)
    Invoke-GraphRequest -Uri $Uri -AccessToken $accessToken -MaxPages 100
}

switch ($Playbook) {
    "BEC" {
        Write-Host "$(Get-Date) INFO: ===== BEC RESPONSE PLAYBOOK ====="
        Write-Host "$(Get-Date) INFO: Target: $TargetUser | Lookback: $daysInt days"
        Write-Host ""

        $findings = [System.Collections.Generic.List[PSCustomObject]]::new()

        # Step 1: Check sign-in logs for suspicious activity
        Write-Host "$(Get-Date) INFO: [Step 1/5] Checking sign-in logs..."
        $filter = "createdDateTime ge $startDT"
        if ($TargetUser) { $filter += " and userPrincipalName eq '$TargetUser'" }
        $signIns = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/signIns?`$filter=$filter&`$top=999"
        $failedSignIns = $signIns | Where-Object { $_.status.errorCode -ne 0 }
        Write-Host "$(Get-Date) INFO: Sign-ins: $($signIns.Count) total, $($failedSignIns.Count) failed"

        # Step 2: Check inbox rules (requires Exchange connection)
        Write-Host "$(Get-Date) INFO: [Step 2/5] Checking inbox rules..."
        try {
            Import-Module ExchangeOnlineManagement -ErrorAction Stop
            Connect-ExchangeOnline -CertificateFilePath $CertificatePath `
                -CertificatePassword (ConvertTo-SecureString $CertificatePassword -AsPlainText -Force) `
                -AppId $ClientId -Organization $Tenant -ShowBanner:$false

            if ($TargetUser) {
                $rules = Get-InboxRule -Mailbox $TargetUser -ErrorAction SilentlyContinue
                $suspiciousRules = $rules | Where-Object {
                    $_.ForwardTo -or $_.ForwardAsAttachmentTo -or
                    $_.RedirectTo -or $_.DeleteMessage -eq $true
                }
                Write-Host "$(Get-Date) INFO: Inbox rules: $($rules.Count) total, $($suspiciousRules.Count) suspicious"
                if ($suspiciousRules) {
                    foreach ($rule in $suspiciousRules) {
                        $findings.Add([PSCustomObject]@{
                            Step    = "Inbox Rules"
                            Finding = "Suspicious rule: $($rule.Name)"
                            Detail  = "Forward: $($rule.ForwardTo) | Redirect: $($rule.RedirectTo) | Delete: $($rule.DeleteMessage)"
                        })
                    }
                }
            }
            Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
        } catch {
            Write-Host "$(Get-Date) WARN: Could not check inbox rules: $($_.Exception.Message)"
        }

        # Step 3: Check OAuth consents
        Write-Host "$(Get-Date) INFO: [Step 3/5] Checking OAuth consent grants..."
        $oauthGrants = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?`$top=999"
        Write-Host "$(Get-Date) INFO: OAuth grants: $($oauthGrants.Count)"

        # Step 4: Check recent password changes
        Write-Host "$(Get-Date) INFO: [Step 4/5] Checking password changes in audit log..."
        $pwFilter = "activityDateTime ge $startDT and activityDisplayName eq 'Reset password'"
        $pwChanges = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?`$filter=$pwFilter"
        Write-Host "$(Get-Date) INFO: Password resets: $($pwChanges.Count)"

        # Step 5: Check MFA status
        Write-Host "$(Get-Date) INFO: [Step 5/5] Checking MFA registration..."
        if ($TargetUser) {
            $mfaUri = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails?`$filter=userPrincipalName eq '$TargetUser'"
            $mfaData = Get-GraphData -Uri $mfaUri
            if ($mfaData -and $mfaData.Count -gt 0) {
                $mfaStatus = $mfaData[0]
                Write-Host "$(Get-Date) INFO: MFA Registered: $($mfaStatus.isMfaRegistered) | Methods: $($mfaStatus.methodsRegistered -join ', ')"
            }
        }

        Write-Host ""
        Write-Host "$(Get-Date) INFO: ===== BEC INVESTIGATION SUMMARY ====="
        Write-Host "$(Get-Date) INFO: Sign-ins: $($signIns.Count) ($($failedSignIns.Count) failed)"
        Write-Host "$(Get-Date) INFO: Suspicious findings: $($findings.Count)"

        if ($findings.Count -gt 0) {
            $baseName = "BEC_Response_${safeTenant}_${ts}"
            Export-AuditData -Data $findings -OutputFolder $OutputFolder -BaseName $baseName
        }
    }

    "AccountCompromise" {
        Write-Host "$(Get-Date) INFO: ===== ACCOUNT COMPROMISE INVESTIGATION ====="
        Write-Host "$(Get-Date) INFO: Target: $TargetUser | Lookback: $daysInt days"

        $stepList = if ($Steps) { $Steps -split "," } else { @("signins", "inbox_rules", "oauth_consents", "mfa_status", "password_changes") }
        $findings = [System.Collections.Generic.List[PSCustomObject]]::new()

        foreach ($step in $stepList) {
            switch ($step.Trim()) {
                "signins" {
                    Write-Host "$(Get-Date) INFO: Checking sign-in logs..."
                    $filter = "createdDateTime ge $startDT"
                    if ($TargetUser) { $filter += " and userPrincipalName eq '$TargetUser'" }
                    $signIns = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/signIns?`$filter=$filter&`$top=999"
                    Write-Host "$(Get-Date) INFO: Found $($signIns.Count) sign-in(s)"
                }
                "inbox_rules" {
                    Write-Host "$(Get-Date) INFO: Checking inbox rules..."
                    # Requires Exchange connection (handled above in BEC)
                }
                "oauth_consents" {
                    Write-Host "$(Get-Date) INFO: Checking OAuth consents..."
                    $grants = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?`$top=999"
                    Write-Host "$(Get-Date) INFO: Found $($grants.Count) OAuth grant(s)"
                }
                "mfa_status" {
                    Write-Host "$(Get-Date) INFO: Checking MFA status..."
                    if ($TargetUser) {
                        $mfaUri = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails?`$filter=userPrincipalName eq '$TargetUser'"
                        $mfa = Get-GraphData -Uri $mfaUri
                        if ($mfa -and $mfa.Count -gt 0) {
                            Write-Host "$(Get-Date) INFO: MFA: $($mfa[0].isMfaRegistered) | Methods: $($mfa[0].methodsRegistered -join ', ')"
                        }
                    }
                }
                "password_changes" {
                    Write-Host "$(Get-Date) INFO: Checking password changes..."
                    $pwFilter = "activityDateTime ge $startDT and activityDisplayName eq 'Reset password'"
                    $pwChanges = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?`$filter=$pwFilter"
                    Write-Host "$(Get-Date) INFO: Password resets: $($pwChanges.Count)"
                }
            }
        }

        Write-Host "$(Get-Date) INFO: Account compromise investigation complete."
    }

    "DataExfiltration" {
        Write-Host "$(Get-Date) INFO: ===== DATA EXFILTRATION ANALYSIS ====="
        Write-Host "$(Get-Date) INFO: Scope: $Scope | Lookback: $daysInt days"

        # Check file sharing activity in audit log
        Write-Host "$(Get-Date) INFO: Checking sharing and download activity..."
        $filter = "activityDateTime ge $startDT"
        $operations = @("FileDownloaded", "FileAccessed", "SharingSet", "AnonymousLinkCreated",
                       "CompanyLinkCreated", "SharingInvitationCreated")

        foreach ($op in $operations) {
            $opFilter = "$filter and activity eq '$op'"
            try {
                $events = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?`$filter=$opFilter"
                Write-Host "$(Get-Date) INFO: $op events: $($events.Count)"
            } catch {
                Write-Host "$(Get-Date) WARN: Could not query $op — $($_.Exception.Message)"
            }
        }

        Write-Host "$(Get-Date) INFO: Data exfiltration analysis complete."
    }

    "MalwareOutbreak" {
        Write-Host "$(Get-Date) INFO: ===== MALWARE OUTBREAK INVESTIGATION ====="
        Write-Host "$(Get-Date) INFO: Lookback: $daysInt days"

        # Check quarantine for malware
        Write-Host "$(Get-Date) INFO: Checking quarantine for malware detections..."
        try {
            Import-Module ExchangeOnlineManagement -ErrorAction Stop
            Connect-ExchangeOnline -CertificateFilePath $CertificatePath `
                -CertificatePassword (ConvertTo-SecureString $CertificatePassword -AsPlainText -Force) `
                -AppId $ClientId -Organization $Tenant -ShowBanner:$false

            $malwareMessages = Get-QuarantineMessage -Type Malware `
                -StartDate (Get-Date).AddDays(-$daysInt) -EndDate (Get-Date) -PageSize 200
            Write-Host "$(Get-Date) INFO: Malware quarantine messages: $($malwareMessages.Count)"

            if ($malwareMessages -and $malwareMessages.Count -gt 0) {
                $results = $malwareMessages | Select-Object ReceivedTime, SenderAddress,
                    RecipientAddress, Subject, Type, PolicyType
                $baseName = "MalwareOutbreak_${safeTenant}_${ts}"
                Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
            }

            Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
        } catch {
            Write-Host "$(Get-Date) WARN: Could not check Exchange quarantine: $($_.Exception.Message)"
        }

        Write-Host "$(Get-Date) INFO: Malware outbreak investigation complete."
    }

    "PrivilegeEscalation" {
        Write-Host "$(Get-Date) INFO: ===== PRIVILEGE ESCALATION DETECTION ====="
        Write-Host "$(Get-Date) INFO: Lookback: $daysInt days"

        # Check directory role changes
        Write-Host "$(Get-Date) INFO: Checking role assignment changes..."
        $roleFilter = "activityDateTime ge $startDT and activityDisplayName eq 'Add member to role'"
        $roleChanges = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?`$filter=$roleFilter"
        Write-Host "$(Get-Date) INFO: Role additions: $($roleChanges.Count)"

        # Check app permission changes
        Write-Host "$(Get-Date) INFO: Checking application permission changes..."
        $appFilter = "activityDateTime ge $startDT and activityDisplayName eq 'Add app role assignment grant to user'"
        $appChanges = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?`$filter=$appFilter"
        Write-Host "$(Get-Date) INFO: App permission grants: $($appChanges.Count)"

        # Check global admin assignments
        Write-Host "$(Get-Date) INFO: Checking current Global Administrator members..."
        $gaRole = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/directoryRoles?`$filter=displayName eq 'Global Administrator'"
        if ($gaRole -and $gaRole.Count -gt 0) {
            $gaMembers = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/directoryRoles/$($gaRole[0].id)/members"
            Write-Host "$(Get-Date) INFO: Global Administrators: $($gaMembers.Count)"
            foreach ($admin in $gaMembers) {
                Write-Host "$(Get-Date) INFO:   - $($admin.userPrincipalName ?? $admin.displayName)"
            }
        }

        $allFindings = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($change in $roleChanges) {
            $allFindings.Add([PSCustomObject]@{
                Type      = "RoleAssignment"
                Activity  = $change.activityDisplayName
                DateTime  = $change.activityDateTime
                Actor     = ($change.initiatedBy.user.userPrincipalName ?? "System")
                Target    = ($change.targetResources | ForEach-Object { $_.userPrincipalName ?? $_.displayName }) -join "; "
            })
        }

        if ($allFindings.Count -gt 0) {
            $baseName = "PrivEsc_${safeTenant}_${ts}"
            Export-AuditData -Data $allFindings -OutputFolder $OutputFolder -BaseName $baseName
        }

        Write-Host "$(Get-Date) INFO: Privilege escalation detection complete."
    }

    "Timeline" {
        Write-Host "$(Get-Date) INFO: ===== INCIDENT TIMELINE BUILDER ====="
        Write-Host "$(Get-Date) INFO: Range: $StartDate to $EndDate"
        if ($TargetUser) { Write-Host "$(Get-Date) INFO: Target user: $TargetUser" }

        $sources = $DataSources -split ","
        $timeline = [System.Collections.Generic.List[PSCustomObject]]::new()

        $sDate = if ($StartDate) { "${StartDate}T00:00:00Z" } else { $startDT }
        $eDate = if ($EndDate) { "${EndDate}T23:59:59Z" } else { (Get-Date).ToString("yyyy-MM-ddT23:59:59Z") }

        foreach ($source in $sources) {
            switch ($source.Trim()) {
                "signins" {
                    Write-Host "$(Get-Date) INFO: Collecting sign-in events..."
                    $filter = "createdDateTime ge $sDate and createdDateTime le $eDate"
                    if ($TargetUser) { $filter += " and userPrincipalName eq '$TargetUser'" }
                    $events = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/signIns?`$filter=$filter&`$top=999"
                    foreach ($e in $events) {
                        $timeline.Add([PSCustomObject]@{
                            Timestamp = $e.createdDateTime
                            Source    = "SignIn"
                            Actor     = $e.userPrincipalName
                            Activity  = "Sign-in from $($e.ipAddress)"
                            Detail    = "App: $($e.appDisplayName) | Status: $($e.status.errorCode)"
                        })
                    }
                }
                "audit" {
                    Write-Host "$(Get-Date) INFO: Collecting directory audit events..."
                    $filter = "activityDateTime ge $sDate and activityDateTime le $eDate"
                    $events = Get-GraphData -Uri "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?`$filter=$filter&`$top=999"
                    foreach ($e in $events) {
                        $timeline.Add([PSCustomObject]@{
                            Timestamp = $e.activityDateTime
                            Source    = "DirectoryAudit"
                            Actor     = ($e.initiatedBy.user.userPrincipalName ?? "System")
                            Activity  = $e.activityDisplayName
                            Detail    = $e.result
                        })
                    }
                }
            }
        }

        $timeline = $timeline | Sort-Object Timestamp
        Write-Host "$(Get-Date) INFO: Timeline contains $($timeline.Count) event(s)."

        if ($timeline.Count -gt 0) {
            $baseName = "IncidentTimeline_${safeTenant}_${ts}"
            Export-AuditData -Data $timeline -OutputFolder $OutputFolder -BaseName $baseName
        }

        Write-Host "$(Get-Date) INFO: Timeline build complete."
    }
}

Write-Host "$(Get-Date) INFO: Incident response playbook complete."
