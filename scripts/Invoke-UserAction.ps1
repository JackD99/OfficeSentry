# Invoke-UserAction.ps1
# Performs user account management actions via Microsoft Graph API.
# Supports: block/unblock sign-in, revoke sessions, reset password,
#           create/delete user, assign/remove licenses, invite/remove guests.
# Designed for MSP helpdesk staff managing multiple M365 tenants.
#
# Required API Permissions (Application):
#   - User.ReadWrite.All (block/unblock, reset password, create/delete)
#   - Directory.ReadWrite.All (license management, user creation)
#   - User.Invite.All (guest invitations)
#
# Usage:
#   .\Invoke-UserAction.ps1 -Action block -TargetUser user@contoso.com ...
#   .\Invoke-UserAction.ps1 -Action create_user -TargetUser new@contoso.com -DisplayName "New User" ...
#   .\Invoke-UserAction.ps1 -Action assign_license -TargetUser user@contoso.com -LicenseSku ENTERPRISEPACK ...

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][ValidateSet(
        "block","unblock","revoke_sessions","reset_password",
        "create_user","delete_user",
        "assign_license","remove_license",
        "invite_guest","remove_guest",
        "update_user"
    )]
    [string] $Action,
    [Parameter(Mandatory)][string] $TargetUser,
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $NewPassword   = "",
    [string] $OutputFolder  = "",
    [string] $DisplayName   = "",
    [string] $LicenseSku    = "",
    [string] $JobTitle      = "",
    [string] $Department    = "",
    [string] $UsageLocation = "GB"
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

    "create_user" {
        if (-not $DisplayName) { $DisplayName = $TargetUser.Split("@")[0] }
        if (-not $NewPassword) {
            $chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%&*"
            $NewPassword = -join (1..16 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
        }
        $mailNickname = ($TargetUser.Split("@")[0]) -replace '[^\w]', ''
        $body = @{
            accountEnabled    = $true
            displayName       = $DisplayName
            mailNickname      = $mailNickname
            userPrincipalName = $TargetUser
            usageLocation     = $UsageLocation
            passwordProfile   = @{
                password                     = $NewPassword
                forceChangePasswordNextSignIn = $true
            }
        }
        if ($JobTitle)   { $body.jobTitle   = $JobTitle }
        if ($Department) { $body.department = $Department }
        $bodyJson = $body | ConvertTo-Json -Depth 3
        $uri = "https://graph.microsoft.com/v1.0/users"
        try {
            $created = Invoke-RestMethod -Uri $uri -Method POST -Headers $headers -Body $bodyJson
            Write-Host "$(Get-Date) INFO: SUCCESS - User '$TargetUser' created (ID: $($created.id))."
            $result = @{
                ok           = $true
                action       = "create_user"
                user         = $TargetUser
                message      = "User created. Temp password must be changed on first sign-in."
                tempPassword = $NewPassword
                userId       = $created.id
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to create user: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "create_user"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "delete_user" {
        $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser"
        try {
            Invoke-RestMethod -Uri $uri -Method DELETE -Headers $headers
            Write-Host "$(Get-Date) INFO: SUCCESS - User '$TargetUser' soft-deleted (recoverable for 30 days)."
            $result = @{
                ok      = $true
                action  = "delete_user"
                user    = $TargetUser
                message = "User soft-deleted. Recoverable from Entra ID recycle bin for 30 days."
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to delete user: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "delete_user"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "assign_license" {
        if (-not $LicenseSku) { throw "LicenseSku is required for assign_license." }
        # Look up the SKU ID from the part number
        $skuUri = "https://graph.microsoft.com/v1.0/subscribedSkus"
        $skus = Invoke-GraphRequest -Uri $skuUri -AccessToken $accessToken
        $sku = $skus | Where-Object { $_.skuPartNumber -eq $LicenseSku } | Select-Object -First 1
        if (-not $sku) { throw "License SKU '$LicenseSku' not found in tenant." }

        $body = @{
            addLicenses    = @(@{ skuId = $sku.skuId })
            removeLicenses = @()
        } | ConvertTo-Json -Depth 3
        $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser/assignLicense"
        try {
            Invoke-RestMethod -Uri $uri -Method POST -Headers $headers -Body $body
            Write-Host "$(Get-Date) INFO: SUCCESS - License '$LicenseSku' assigned to '$TargetUser'."
            $result = @{
                ok      = $true
                action  = "assign_license"
                user    = $TargetUser
                message = "License '$LicenseSku' assigned successfully."
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to assign license: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "assign_license"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "remove_license" {
        if (-not $LicenseSku) { throw "LicenseSku is required for remove_license." }
        $skuUri = "https://graph.microsoft.com/v1.0/subscribedSkus"
        $skus = Invoke-GraphRequest -Uri $skuUri -AccessToken $accessToken
        $sku = $skus | Where-Object { $_.skuPartNumber -eq $LicenseSku } | Select-Object -First 1
        if (-not $sku) { throw "License SKU '$LicenseSku' not found in tenant." }

        $body = @{
            addLicenses    = @()
            removeLicenses = @($sku.skuId)
        } | ConvertTo-Json -Depth 3
        $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser/assignLicense"
        try {
            Invoke-RestMethod -Uri $uri -Method POST -Headers $headers -Body $body
            Write-Host "$(Get-Date) INFO: SUCCESS - License '$LicenseSku' removed from '$TargetUser'."
            $result = @{
                ok      = $true
                action  = "remove_license"
                user    = $TargetUser
                message = "License '$LicenseSku' removed successfully."
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to remove license: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "remove_license"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "invite_guest" {
        if (-not $DisplayName) { $DisplayName = $TargetUser.Split("@")[0] }
        $body = @{
            invitedUserEmailAddress = $TargetUser
            invitedUserDisplayName  = $DisplayName
            inviteRedirectUrl       = "https://myapps.microsoft.com"
            sendInvitationMessage   = $true
        } | ConvertTo-Json -Depth 3
        $uri = "https://graph.microsoft.com/v1.0/invitations"
        try {
            $invite = Invoke-RestMethod -Uri $uri -Method POST -Headers $headers -Body $body
            Write-Host "$(Get-Date) INFO: SUCCESS - Guest invitation sent to '$TargetUser'."
            $result = @{
                ok      = $true
                action  = "invite_guest"
                user    = $TargetUser
                message = "Guest invitation sent. User will receive an email to accept."
                status  = $invite.status
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to invite guest: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "invite_guest"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "remove_guest" {
        # Find guest user first
        $uri = "https://graph.microsoft.com/v1.0/users?`$filter=mail eq '$TargetUser' and userType eq 'Guest'&`$select=id,displayName,userPrincipalName"
        $h = @{ Authorization = "Bearer $accessToken" }
        try {
            $resp = Invoke-RestMethod -Uri $uri -Method GET -Headers $h
            if (-not $resp.value -or $resp.value.Count -eq 0) {
                throw "Guest user '$TargetUser' not found."
            }
            $guestId = $resp.value[0].id
            $delUri = "https://graph.microsoft.com/v1.0/users/$guestId"
            Invoke-RestMethod -Uri $delUri -Method DELETE -Headers $headers
            Write-Host "$(Get-Date) INFO: SUCCESS - Guest user '$TargetUser' removed."
            $result = @{
                ok      = $true
                action  = "remove_guest"
                user    = $TargetUser
                message = "Guest user removed from directory."
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to remove guest: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "remove_guest"; user = $TargetUser; error = $_.Exception.Message }
        }
    }

    "update_user" {
        $body = @{}
        if ($DisplayName) { $body.displayName = $DisplayName }
        if ($JobTitle)    { $body.jobTitle    = $JobTitle }
        if ($Department)  { $body.department  = $Department }
        if ($body.Count -eq 0) { throw "At least one field (DisplayName, JobTitle, Department) is required." }

        $bodyJson = $body | ConvertTo-Json
        $uri = "https://graph.microsoft.com/v1.0/users/$TargetUser"
        try {
            Invoke-RestMethod -Uri $uri -Method PATCH -Headers $headers -Body $bodyJson
            $fields = ($body.Keys -join ", ")
            Write-Host "$(Get-Date) INFO: SUCCESS - Updated $fields for '$TargetUser'."
            $result = @{
                ok      = $true
                action  = "update_user"
                user    = $TargetUser
                message = "User profile updated ($fields)."
            }
        }
        catch {
            Write-Host "$(Get-Date) ERROR: Failed to update user: $_" -ForegroundColor Red
            $result = @{ ok = $false; action = "update_user"; user = $TargetUser; error = $_.Exception.Message }
        }
    }
}

# Output JSON result on last line for portal parsing
$result | ConvertTo-Json -Compress
Write-Host "$(Get-Date) INFO: Action '$Action' completed for '$TargetUser'."
