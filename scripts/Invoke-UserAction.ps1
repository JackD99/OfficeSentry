# Invoke-UserAction.ps1
# Performs user account management actions via Microsoft Graph API.
# Supports: block sign-in, unblock sign-in, revoke sessions, reset password.
# Designed for MSP helpdesk staff managing multiple M365 tenants.
#
# Required API Permissions (Application):
#   - User.ReadWrite.All (block/unblock, reset password)
#   - Directory.ReadWrite.All (for password reset on some account types)
#
# Usage:
#   .\Invoke-UserAction.ps1 -Action block -TargetUser user@contoso.com ...
#   .\Invoke-UserAction.ps1 -Action revoke_sessions -TargetUser user@contoso.com ...

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][ValidateSet("block","unblock","revoke_sessions","reset_password")]
    [string] $Action,
    [Parameter(Mandatory)][string] $TargetUser,
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $NewPassword = "",
    [string] $OutputFolder = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Acquiring Graph API token for tenant $Tenant..."
$token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId `
    -CertificatePath $CertificatePath -CertificatePassword $CertificatePassword

$accessToken = $token.AccessToken
$headers = @{
    Authorization  = "Bearer $accessToken"
    "Content-Type" = "application/json"
}

Write-Host "$(Get-Date) INFO: Performing action '$Action' on user '$TargetUser'..."

switch ($Action) {
    "block" {
        $body = @{ accountEnabled = $false } | ConvertTo-Json
        $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser"
        try {
            Invoke-RestMethod -Uri $uri -Method PATCH -Headers $headers -Body $body
            Write-Host "$(Get-Date) INFO: SUCCESS - User '$TargetUser' sign-in blocked."
            $result = @{
                ok      = $true
                action  = "block"
                user    = $TargetUser
                message = "Sign-in blocked successfully."
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to block user: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "block"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "unblock" {
        $body = @{ accountEnabled = $true } | ConvertTo-Json
        $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser"
        try {
            Invoke-RestMethod -Uri $uri -Method PATCH -Headers $headers -Body $body
            Write-Host "$(Get-Date) INFO: SUCCESS - User '$TargetUser' sign-in unblocked."
            $result = @{
                ok      = $true
                action  = "unblock"
                user    = $TargetUser
                message = "Sign-in unblocked successfully."
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to unblock user: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "unblock"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "revoke_sessions" {
        $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser/revokeSignInSessions"
        try {
            Invoke-RestMethod -Uri $uri -Method POST -Headers $headers
            Write-Host "$(Get-Date) INFO: SUCCESS - All sessions revoked for '$TargetUser'."
            $result = @{
                ok      = $true
                action  = "revoke_sessions"
                user    = $TargetUser
                message = "All sign-in sessions revoked. User must re-authenticate."
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to revoke sessions: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "revoke_sessions"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "reset_password" {
        if (-not $NewPassword) {
            # Generate a random password if none provided
            $chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%&*"
            $NewPassword = -join (1..16 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
            Write-Host "$(Get-Date) INFO: Generated temporary password (user must change on next sign-in)."
        }
        $body = @{
            passwordProfile = @{
                password                     = $NewPassword
                forceChangePasswordNextSignIn = $true
            }
        } | ConvertTo-Json -Depth 3
        $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser"
        try {
            Invoke-RestMethod -Uri $uri -Method PATCH -Headers $headers -Body $body
            Write-Host "$(Get-Date) INFO: SUCCESS - Password reset for '$TargetUser'. User must change on next sign-in."
            $result = @{
                ok           = $true
                action       = "reset_password"
                user         = $TargetUser
                message      = "Password reset. User must change on next sign-in."
                tempPassword = $NewPassword
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to reset password: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "reset_password"; user = $TargetUser; error = $_.Exception.Message }
        }
    }
}

# Output JSON result on last line for portal parsing
$result | ConvertTo-Json -Compress
Write-Host "$(Get-Date) INFO: Action '$Action' completed for '$TargetUser'."
