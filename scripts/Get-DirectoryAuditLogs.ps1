#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Retrieves Entra ID / Azure AD directory audit logs for a tenant.
.DESCRIPTION
    Queries Microsoft Graph for directory audit events including
    user, group, app, and policy changes. Supports date range filtering.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tenant,
    [Parameter(Mandatory)][string]$ClientId,
    [Parameter(Mandatory)][string]$CertificatePath,
    [string]$StartDate = "",
    [string]$EndDate = "",
    [int]$Top = 500
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
Import-Module $sharedModule -Force

try {
    $token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId -CertificatePath $CertificatePath
    $headers = @{ Authorization = "Bearer $token" }

    Write-Host "$(Get-Date) INFO: Fetching directory audit logs for $Tenant"

    $uri = "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?`$top=$Top&`$orderby=activityDateTime desc"

    # Apply date filters
    $filters = @()
    if ($StartDate) {
        $filters += "activityDateTime ge $StartDate"
    }
    if ($EndDate) {
        $filters += "activityDateTime le $EndDate"
    }
    if ($filters.Count -gt 0) {
        $filterStr = $filters -join " and "
        $uri += "&`$filter=$filterStr"
    }

    $logs = Invoke-GraphRequest -Uri $uri -Headers $headers

    $logList = [System.Collections.Generic.List[PSCustomObject]]::new()
    foreach ($l in $logs) {
        $targetName = ""
        if ($l.targetResources -and $l.targetResources.Count -gt 0) {
            $targetName = $l.targetResources[0].displayName
        }

        $logList.Add([PSCustomObject]@{
            Id            = $l.id
            Activity      = $l.activityDisplayName
            Category      = $l.category
            Result        = $l.result
            ActorUPN      = $l.initiatedBy.user.userPrincipalName
            ActorApp      = $l.initiatedBy.app.displayName
            TargetName    = $targetName
            TargetType    = if ($l.targetResources) { $l.targetResources[0].'@odata.type' } else { "" }
            DateTime      = $l.activityDateTime
            CorrelationId = $l.correlationId
        })
    }

    Write-Host "$(Get-Date) INFO: Retrieved $($logList.Count) audit log entries"

    $output = @{
        ok   = $true
        logs = @($logList | ForEach-Object { @{
            id            = $_.Id
            activity      = $_.Activity
            category      = $_.Category
            result        = $_.Result
            actorUPN      = $_.ActorUPN
            actorApp      = $_.ActorApp
            targetName    = $_.TargetName
            targetType    = $_.TargetType
            dateTime      = $_.DateTime
            correlationId = $_.CorrelationId
        }})
        count = $logList.Count
    }
    $output | ConvertTo-Json -Depth 5 -Compress
}
catch {
    Write-Host "$(Get-Date) ERROR: $_"
    @{ ok = $false; error = "$_" } | ConvertTo-Json -Compress
}
