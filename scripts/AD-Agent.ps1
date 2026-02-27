<#
.SYNOPSIS
    Office Sentry AD Agent — On-premises Active Directory agent for the M365 Audit Portal.

.DESCRIPTION
    Polls the portal for commands, executes AD operations locally, and reports results.
    Uses HMAC-SHA256 signed requests for authentication with replay protection.

    On first run, reads enrollment.json to bootstrap credentials.
    Credentials are stored locally using Windows DPAPI encryption.

.NOTES
    Requires: PowerShell 5.1+, ActiveDirectory module, network access to portal.
#>

#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ConfigDir = $PSScriptRoot,
    [int]$PollIntervalSeconds = 60,
    [switch]$RunOnce
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$script:EnrollmentFile = Join-Path $ConfigDir "enrollment.json"
$script:CredentialFile = Join-Path $ConfigDir "agent_credentials.xml"
$script:LogFile        = Join-Path $ConfigDir "agent.log"
$script:AgentVersion   = "1.0.0"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
function Write-AgentLog {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts $Level`: $Message"
    Write-Host $line
    try { $line | Out-File -Append -FilePath $script:LogFile -Encoding UTF8 } catch {}
}

# ---------------------------------------------------------------------------
# DPAPI credential storage
# ---------------------------------------------------------------------------
function Save-AgentCredentials {
    param([string]$AgentId, [string]$AgentSecret, [string]$PortalUrl, [string]$TenantId)
    $cred = @{
        AgentId     = $AgentId
        AgentSecret = $AgentSecret | ConvertTo-SecureString -AsPlainText -Force | ConvertFrom-SecureString
        PortalUrl   = $PortalUrl
        TenantId    = $TenantId
    }
    $cred | Export-Clixml -Path $script:CredentialFile -Force
    Write-AgentLog "Credentials saved (DPAPI encrypted)"
}

function Load-AgentCredentials {
    if (-not (Test-Path $script:CredentialFile)) { return $null }
    try {
        $cred = Import-Clixml -Path $script:CredentialFile
        $ss = $cred.AgentSecret | ConvertTo-SecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss)
        $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        return @{
            AgentId     = $cred.AgentId
            AgentSecret = $plain
            PortalUrl   = $cred.PortalUrl
            TenantId    = $cred.TenantId
        }
    } catch {
        Write-AgentLog "Failed to load credentials: $_" "ERROR"
        return $null
    }
}

# ---------------------------------------------------------------------------
# HMAC-SHA256 request signing
# ---------------------------------------------------------------------------
function Invoke-SignedRequest {
    param(
        [string]$Url,
        [string]$Method = "POST",
        [string]$Body = "",
        [string]$AgentId,
        [string]$AgentSecret
    )

    $timestamp = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $nonce = -join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })

    # Compute HMAC-SHA256
    $message = "$AgentId`n$timestamp`n$nonce`n$Body"
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($AgentSecret)
    $messageBytes = [System.Text.Encoding]::UTF8.GetBytes($message)
    $hashBytes = $hmac.ComputeHash($messageBytes)
    $signature = ($hashBytes | ForEach-Object { $_.ToString("x2") }) -join ""

    $headers = @{
        "X-Agent-Id"        = $AgentId
        "X-Agent-Timestamp" = $timestamp
        "X-Agent-Nonce"     = $nonce
        "X-Agent-Signature" = $signature
        "Content-Type"      = "application/json"
    }

    $params = @{
        Uri         = $Url
        Method      = $Method
        Headers     = $headers
        ContentType = "application/json"
    }
    if ($Body -and $Body -ne "") {
        $params["Body"] = $Body
    }

    try {
        $response = Invoke-RestMethod @params -ErrorAction Stop
        return $response
    } catch {
        $statusCode = $null
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        Write-AgentLog "Request failed: $_ (HTTP $statusCode)" "ERROR"
        return @{ ok = $false; error = $_.Exception.Message }
    }
}

# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------
function Invoke-Enrollment {
    if (-not (Test-Path $script:EnrollmentFile)) {
        Write-AgentLog "No enrollment.json found — cannot enroll" "ERROR"
        return $null
    }

    $enrollment = Get-Content $script:EnrollmentFile -Raw | ConvertFrom-Json
    Write-AgentLog "Starting enrollment for tenant: $($enrollment.tenant_name)"

    # Detect AD server names
    $serverNames = @()
    try {
        Import-Module ActiveDirectory -ErrorAction Stop
        $dc = Get-ADDomainController -Discover -ErrorAction Stop
        $serverNames = @($dc.HostName)
        Write-AgentLog "Detected AD server(s): $($serverNames -join ', ')"
    } catch {
        Write-AgentLog "Could not detect AD servers: $_" "WARN"
    }

    $body = @{
        tenant_id        = $enrollment.tenant_id
        enrollment_token = $enrollment.enrollment_token
        agent_name       = "$env:COMPUTERNAME Agent"
        server_names     = $serverNames
    } | ConvertTo-Json -Compress

    $url = "$($enrollment.portal_url)/api/agent/enroll"
    Write-AgentLog "Enrolling at $url"

    try {
        $result = Invoke-RestMethod -Uri $url -Method POST -Body $body `
            -ContentType "application/json" -ErrorAction Stop
    } catch {
        Write-AgentLog "Enrollment failed: $_" "ERROR"
        return $null
    }

    if ($result.ok) {
        Save-AgentCredentials -AgentId $result.agent_id `
            -AgentSecret $result.agent_secret `
            -PortalUrl $enrollment.portal_url `
            -TenantId $enrollment.tenant_id

        # Delete enrollment file (consumed token)
        Remove-Item $script:EnrollmentFile -Force -ErrorAction SilentlyContinue
        Write-AgentLog "Enrollment successful. Agent ID: $($result.agent_id)"
        return Load-AgentCredentials
    } else {
        Write-AgentLog "Enrollment rejected: $($result.error)" "ERROR"
        return $null
    }
}

# ---------------------------------------------------------------------------
# AD Operations
# ---------------------------------------------------------------------------
function Invoke-ADCommand {
    param([PSCustomObject]$Command)

    $type = $Command.command_type
    $params = $Command.parameters

    Write-AgentLog "Executing command: $type (ID: $($Command.id))"

    try {
        Import-Module ActiveDirectory -ErrorAction Stop
    } catch {
        return @{ ok = $false; error = "ActiveDirectory module not available: $_" }
    }

    switch ($type) {
        "ping" {
            return @{ ok = $true; message = "pong"; hostname = $env:COMPUTERNAME; time = (Get-Date -Format "o") }
        }
        "lookup_user" {
            return Invoke-LookupUser -Params $params
        }
        "update_user" {
            return Invoke-UpdateUser -Params $params
        }
        "add_group_member" {
            return Invoke-AddGroupMember -Params $params
        }
        "remove_group_member" {
            return Invoke-RemoveGroupMember -Params $params
        }
        "apply_template" {
            return Invoke-ApplyTemplate -Params $params
        }
        default {
            return @{ ok = $false; error = "Unknown command type: $type" }
        }
    }
}

function Invoke-LookupUser {
    param([PSCustomObject]$Params)
    $identity = $Params.identity
    if (-not $identity) { return @{ ok = $false; error = "identity parameter required" } }

    try {
        $user = Get-ADUser -Identity $identity -Properties DisplayName, Title, Department,
            TelephoneNumber, EmailAddress, MemberOf, Enabled, DistinguishedName -ErrorAction Stop
        $groups = $user.MemberOf | ForEach-Object {
            try { (Get-ADGroup $_ -ErrorAction Stop).Name } catch { $_ }
        }
        return @{
            ok = $true
            user = @{
                SamAccountName    = $user.SamAccountName
                DisplayName       = $user.DisplayName
                Title             = $user.Title
                Department        = $user.Department
                TelephoneNumber   = $user.TelephoneNumber
                EmailAddress      = $user.EmailAddress
                Enabled           = $user.Enabled
                DistinguishedName = $user.DistinguishedName
                Groups            = @($groups)
            }
        }
    } catch {
        return @{ ok = $false; error = "User lookup failed: $_" }
    }
}

function Invoke-UpdateUser {
    param([PSCustomObject]$Params)
    $identity = $Params.identity
    if (-not $identity) { return @{ ok = $false; error = "identity parameter required" } }

    # Allowed attributes for update (least privilege)
    $allowedAttrs = @("DisplayName", "Title", "Department", "TelephoneNumber",
                      "Description", "Office", "Company", "StreetAddress",
                      "City", "State", "PostalCode", "Country")

    $updates = @{}
    foreach ($attr in $allowedAttrs) {
        $val = $null
        if ($Params.PSObject.Properties[$attr]) { $val = $Params.$attr }
        # Also try lowercase
        $lower = $attr.Substring(0,1).ToLower() + $attr.Substring(1)
        if (-not $val -and $Params.PSObject.Properties[$lower]) { $val = $Params.$lower }
        if ($null -ne $val) { $updates[$attr] = $val }
    }

    if ($updates.Count -eq 0) {
        return @{ ok = $false; error = "No valid attributes to update. Allowed: $($allowedAttrs -join ', ')" }
    }

    try {
        Set-ADUser -Identity $identity @updates -ErrorAction Stop
        return @{ ok = $true; message = "User updated"; attributes = ($updates.Keys -join ", ") }
    } catch {
        return @{ ok = $false; error = "User update failed: $_" }
    }
}

function Invoke-AddGroupMember {
    param([PSCustomObject]$Params)
    $identity = $Params.identity
    $group = $Params.group
    if (-not $identity -or -not $group) {
        return @{ ok = $false; error = "identity and group parameters required" }
    }
    try {
        Add-ADGroupMember -Identity $group -Members $identity -ErrorAction Stop
        return @{ ok = $true; message = "Added $identity to group $group" }
    } catch {
        return @{ ok = $false; error = "Add group member failed: $_" }
    }
}

function Invoke-RemoveGroupMember {
    param([PSCustomObject]$Params)
    $identity = $Params.identity
    $group = $Params.group
    if (-not $identity -or -not $group) {
        return @{ ok = $false; error = "identity and group parameters required" }
    }
    try {
        Remove-ADGroupMember -Identity $group -Members $identity -Confirm:$false -ErrorAction Stop
        return @{ ok = $true; message = "Removed $identity from group $group" }
    } catch {
        return @{ ok = $false; error = "Remove group member failed: $_" }
    }
}

function Invoke-ApplyTemplate {
    param([PSCustomObject]$Params)
    $identity = $Params.identity
    $template = $Params.template
    if (-not $identity -or -not $template) {
        return @{ ok = $false; error = "identity and template parameters required" }
    }

    $results = [System.Collections.Generic.List[string]]::new()

    try {
        # Apply attribute updates from template
        if ($template.attributes) {
            $updates = @{}
            foreach ($prop in $template.attributes.PSObject.Properties) {
                $updates[$prop.Name] = $prop.Value
            }
            if ($updates.Count -gt 0) {
                Set-ADUser -Identity $identity @updates -ErrorAction Stop
                $results.Add("Updated attributes: $($updates.Keys -join ', ')")
            }
        }

        # Apply group memberships
        if ($template.groups) {
            foreach ($group in $template.groups) {
                try {
                    Add-ADGroupMember -Identity $group -Members $identity -ErrorAction Stop
                    $results.Add("Added to group: $group")
                } catch {
                    $results.Add("Failed to add to group ${group}: $_")
                }
            }
        }

        # Move to OU if specified
        if ($template.ou) {
            try {
                $user = Get-ADUser -Identity $identity -ErrorAction Stop
                Move-ADObject -Identity $user.DistinguishedName -TargetPath $template.ou -ErrorAction Stop
                $results.Add("Moved to OU: $($template.ou)")
            } catch {
                $results.Add("Failed to move to OU: $_")
            }
        }

        return @{ ok = $true; message = "Template applied"; details = $results.ToArray() }
    } catch {
        return @{ ok = $false; error = "Template application failed: $_"; details = $results.ToArray() }
    }
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
function Start-AgentLoop {
    # Load or enroll credentials
    $cred = Load-AgentCredentials
    if (-not $cred) {
        Write-AgentLog "No stored credentials — attempting enrollment"
        $cred = Invoke-Enrollment
        if (-not $cred) {
            Write-AgentLog "Enrollment failed. Exiting." "ERROR"
            exit 1
        }
    }

    Write-AgentLog "Agent started (ID: $($cred.AgentId), Portal: $($cred.PortalUrl))"

    $heartbeatCounter = 0

    do {
        try {
            # Heartbeat every 5 poll cycles
            if ($heartbeatCounter % 5 -eq 0) {
                $hbBody = @{
                    agent_version = $script:AgentVersion
                } | ConvertTo-Json -Compress

                $hbResult = Invoke-SignedRequest `
                    -Url "$($cred.PortalUrl)/api/agent/heartbeat" `
                    -Body $hbBody `
                    -AgentId $cred.AgentId `
                    -AgentSecret $cred.AgentSecret

                if ($hbResult.ok) {
                    Write-AgentLog "Heartbeat OK" "DEBUG"
                } else {
                    Write-AgentLog "Heartbeat failed: $($hbResult.error)" "WARN"
                    if ($hbResult.error -match "revoked|unknown") {
                        Write-AgentLog "Agent appears revoked. Stopping." "ERROR"
                        break
                    }
                }
            }
            $heartbeatCounter++

            # Poll for commands
            $pollResult = Invoke-SignedRequest `
                -Url "$($cred.PortalUrl)/api/agent/poll" `
                -Body "{}" `
                -AgentId $cred.AgentId `
                -AgentSecret $cred.AgentSecret

            if ($pollResult.ok -and $pollResult.commands -and $pollResult.commands.Count -gt 0) {
                Write-AgentLog "Received $($pollResult.commands.Count) command(s)"

                foreach ($cmd in $pollResult.commands) {
                    $cmdResult = Invoke-ADCommand -Command $cmd
                    $status = if ($cmdResult.ok) { "completed" } else { "failed" }

                    $resultBody = @{
                        command_id = $cmd.id
                        result     = $cmdResult
                        status     = $status
                    } | ConvertTo-Json -Depth 10 -Compress

                    $reportResult = Invoke-SignedRequest `
                        -Url "$($cred.PortalUrl)/api/agent/result" `
                        -Body $resultBody `
                        -AgentId $cred.AgentId `
                        -AgentSecret $cred.AgentSecret

                    if ($reportResult.ok) {
                        Write-AgentLog "Command $($cmd.id) result reported ($status)"
                    } else {
                        Write-AgentLog "Failed to report command result: $($reportResult.error)" "WARN"
                    }
                }
            }
        } catch {
            Write-AgentLog "Poll cycle error: $_" "ERROR"
        }

        if (-not $RunOnce) {
            Start-Sleep -Seconds $PollIntervalSeconds
        }
    } while (-not $RunOnce)
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
Write-AgentLog "Office Sentry AD Agent v$script:AgentVersion starting on $env:COMPUTERNAME"
Start-AgentLoop
Write-AgentLog "Agent stopped"
