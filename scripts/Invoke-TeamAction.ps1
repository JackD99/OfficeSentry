# Invoke-TeamAction.ps1
# Microsoft Teams management actions via Microsoft Graph API.
# Supports: list teams, list members, add/remove members/owners, archive/unarchive.
# Designed for MSP helpdesk staff managing Teams across client tenants.
#
# Required API Permissions (Application):
#   - Team.ReadBasic.All (list teams)
#   - TeamMember.ReadWrite.All (manage members)
#   - TeamSettings.ReadWrite.All (archive/unarchive)
#   - Group.ReadWrite.All (create team, manage membership)
#   - Directory.Read.All (resolve user IDs)

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][ValidateSet(
        "list_teams","team_members",
        "add_team_member","remove_team_member","add_team_owner",
        "archive_team","unarchive_team"
    )]
    [string] $Action,
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OutputFolder = "",
    [string] $TeamName     = "",
    [string] $TargetUser   = ""
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

# Helper: find team by display name
function Find-TeamByName {
    param([string]$Name)
    $encoded = [System.Uri]::EscapeDataString($Name)
    $uri = "https://graph.microsoft.com/v1.0/groups?`$filter=resourceProvisioningOptions/Any(x:x eq 'Team') and displayName eq '$encoded'&`$select=id,displayName,description,mail,visibility"
    $teams = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken
    if ($teams.Count -eq 0) { throw "Team '$Name' not found." }
    return $teams[0]
}

# Helper: resolve user ID from UPN
function Get-UserIdFromUpn {
    param([string]$Upn)
    $uri = "https://graph.microsoft.com/v1.0/users/$Upn`?`$select=id,displayName"
    $h = @{ Authorization = "Bearer $accessToken" }
    $user = Invoke-RestMethod -Uri $uri -Method GET -Headers $h
    return $user
}

Write-Host "$(Get-Date) INFO: Performing Teams action '$Action'..."

switch ($Action) {
    "list_teams" {
        Write-Host "$(Get-Date) INFO: Retrieving all teams..."
        $uri = "https://graph.microsoft.com/v1.0/groups?`$filter=resourceProvisioningOptions/Any(x:x eq 'Team')&`$select=id,displayName,description,mail,visibility,createdDateTime&`$top=999"
        $teams = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken

        $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
        $counter = 0
        foreach ($team in $teams) {
            $counter++
            Write-Progress -Activity "Processing teams" -Status "$counter of $($teams.Count)" `
                -PercentComplete (($counter / [math]::Max($teams.Count, 1)) * 100)

            # Get member/owner counts
            $memberUri = "https://graph.microsoft.com/v1.0/groups/$($team.id)/members?`$select=id&`$top=999"
            $ownerUri = "https://graph.microsoft.com/v1.0/groups/$($team.id)/owners?`$select=id&`$top=999"
            try {
                $members = Invoke-GraphRequest -Uri $memberUri -AccessToken $accessToken
                $owners = Invoke-GraphRequest -Uri $ownerUri -AccessToken $accessToken
                $memberCount = $members.Count
                $ownerCount = $owners.Count
            } catch {
                $memberCount = "N/A"
                $ownerCount = "N/A"
            }

            $rows.Add([PSCustomObject]@{
                TeamName    = $team.displayName
                Description = $team.description
                Email       = $team.mail
                Visibility  = $team.visibility
                Members     = $memberCount
                Owners      = $ownerCount
                Created     = $team.createdDateTime
                GroupId     = $team.id
            })
        }
        Write-Progress -Activity "Processing teams" -Completed

        Write-Host "$(Get-Date) INFO: Found $($rows.Count) teams."
        if ($OutputFolder) {
            $sheets = @(@{ Name = "Teams"; Data = $rows.ToArray() })
            Export-AuditData -OutputFolder $OutputFolder -FilePrefix "Teams_List" -Sheets $sheets
        }
    }

    "team_members" {
        if (-not $TeamName) { throw "TeamName is required for team_members action." }
        $team = Find-TeamByName $TeamName
        Write-Host "$(Get-Date) INFO: Retrieving members of team '$($team.displayName)'..."

        $memberUri = "https://graph.microsoft.com/v1.0/groups/$($team.id)/members?`$select=id,displayName,userPrincipalName,mail,userType&`$top=999"
        $ownerUri = "https://graph.microsoft.com/v1.0/groups/$($team.id)/owners?`$select=id,displayName,userPrincipalName&`$top=999"
        $members = Invoke-GraphRequest -Uri $memberUri -AccessToken $accessToken
        $owners = Invoke-GraphRequest -Uri $ownerUri -AccessToken $accessToken
        $ownerIds = @($owners | ForEach-Object { $_.id })

        $rows = [System.Collections.Generic.List[PSCustomObject]]::new()
        foreach ($m in $members) {
            $rows.Add([PSCustomObject]@{
                DisplayName       = $m.displayName
                UserPrincipalName = $m.userPrincipalName
                Email             = $m.mail
                UserType          = $m.userType
                Role              = if ($ownerIds -contains $m.id) { "Owner" } else { "Member" }
            })
        }

        Write-Host "$(Get-Date) INFO: Found $($rows.Count) members ($($ownerIds.Count) owners)."
        if ($OutputFolder) {
            $sheets = @(@{ Name = "TeamMembers"; Data = $rows.ToArray() })
            $safeName = $team.displayName -replace '[^\w]', '_'
            Export-AuditData -OutputFolder $OutputFolder -FilePrefix "Team_${safeName}" -Sheets $sheets
        }
    }

    "add_team_member" {
        if (-not $TeamName) { throw "TeamName is required." }
        if (-not $TargetUser) { throw "TargetUser is required." }
        $team = Find-TeamByName $TeamName
        $user = Get-UserIdFromUpn $TargetUser

        $body = @{
            "@odata.type"     = "#microsoft.graph.aadUserConversationMember"
            "roles"           = @()
            "user@odata.bind" = "https://graph.microsoft.com/v1.0/users('$($user.id)')"
        } | ConvertTo-Json -Depth 3

        $uri = "https://graph.microsoft.com/v1.0/teams/$($team.id)/members"
        Invoke-RestMethod -Uri $uri -Method POST -Headers $headers -Body $body
        Write-Host "$(Get-Date) INFO: SUCCESS - Added '$TargetUser' as member of '$($team.displayName)'."
    }

    "remove_team_member" {
        if (-not $TeamName) { throw "TeamName is required." }
        if (-not $TargetUser) { throw "TargetUser is required." }
        $team = Find-TeamByName $TeamName

        # Find the membership ID
        $memberUri = "https://graph.microsoft.com/v1.0/teams/$($team.id)/members?`$filter=microsoft.graph.aadUserConversationMember/email eq '$TargetUser'"
        $h = @{ Authorization = "Bearer $accessToken" }
        $result = Invoke-RestMethod -Uri $memberUri -Method GET -Headers $h
        if (-not $result.value -or $result.value.Count -eq 0) {
            throw "User '$TargetUser' is not a member of team '$($team.displayName)'."
        }
        $membershipId = $result.value[0].id

        $delUri = "https://graph.microsoft.com/v1.0/teams/$($team.id)/members/$membershipId"
        Invoke-RestMethod -Uri $delUri -Method DELETE -Headers $headers
        Write-Host "$(Get-Date) INFO: SUCCESS - Removed '$TargetUser' from '$($team.displayName)'."
    }

    "add_team_owner" {
        if (-not $TeamName) { throw "TeamName is required." }
        if (-not $TargetUser) { throw "TargetUser is required." }
        $team = Find-TeamByName $TeamName
        $user = Get-UserIdFromUpn $TargetUser

        $body = @{
            "@odata.type"     = "#microsoft.graph.aadUserConversationMember"
            "roles"           = @("owner")
            "user@odata.bind" = "https://graph.microsoft.com/v1.0/users('$($user.id)')"
        } | ConvertTo-Json -Depth 3

        $uri = "https://graph.microsoft.com/v1.0/teams/$($team.id)/members"
        Invoke-RestMethod -Uri $uri -Method POST -Headers $headers -Body $body
        Write-Host "$(Get-Date) INFO: SUCCESS - Added '$TargetUser' as owner of '$($team.displayName)'."
    }

    "archive_team" {
        if (-not $TeamName) { throw "TeamName is required." }
        $team = Find-TeamByName $TeamName
        $uri = "https://graph.microsoft.com/v1.0/teams/$($team.id)/archive"
        Invoke-RestMethod -Uri $uri -Method POST -Headers $headers
        Write-Host "$(Get-Date) INFO: SUCCESS - Team '$($team.displayName)' archived (read-only)."
    }

    "unarchive_team" {
        if (-not $TeamName) { throw "TeamName is required." }
        $team = Find-TeamByName $TeamName
        $uri = "https://graph.microsoft.com/v1.0/teams/$($team.id)/unarchive"
        Invoke-RestMethod -Uri $uri -Method POST -Headers $headers
        Write-Host "$(Get-Date) INFO: SUCCESS - Team '$($team.displayName)' unarchived."
    }
}

Write-Host "$(Get-Date) INFO: Teams action '$Action' completed."
