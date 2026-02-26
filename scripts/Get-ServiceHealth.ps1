# Get-ServiceHealth.ps1
# Retrieves M365 service health information via Microsoft Graph API.
# Supports status, incidents, message center, maintenance, and overview reports.
# Requires: MSAL.PS, ImportExcel (optional)

#requires -Modules MSAL.PS

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [string] $OutputFolder = ".",
    [Parameter(Mandatory)]
    [ValidateSet("status", "incidents", "messageCenter", "maintenance", "overview")]
    [string] $ReportType,
    [switch] $IncludeResolved,
    [string] $MessageType = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Acquiring Graph token..."
$token = Get-AuditGraphToken `
    -Tenant              $Tenant `
    -ClientId            $ClientId `
    -CertificatePath     $CertificatePath `
    -CertificatePassword $CertificatePassword

$accessToken = $token.AccessToken
$safeTenant = ($Tenant -replace '[^\w\-\.]', '_')
$ts = Get-Date -Format "yyyyMMdd-HHmm"

switch ($ReportType) {
    "status" {
        Write-Host "$(Get-Date) INFO: Fetching service health status..."
        $uri = "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/healthOverviews"
        $services = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 10

        Write-Host "$(Get-Date) INFO: Found $($services.Count) service(s)."
        foreach ($svc in $services) {
            $statusIcon = if ($svc.status -eq "serviceOperational") { "[OK]" } else { "[!!]" }
            Write-Host "$(Get-Date) INFO: $statusIcon $($svc.service) — $($svc.status)"
        }

        $services | ConvertTo-Json -Depth 5
    }

    "incidents" {
        Write-Host "$(Get-Date) INFO: Fetching service incidents..."
        $uri = "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/issues"
        if (-not $IncludeResolved) {
            $uri += "?`$filter=isResolved eq false"
        }

        $issues = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 50
        Write-Host "$(Get-Date) INFO: Found $($issues.Count) issue(s)."

        if ($issues.Count -gt 0) {
            $results = $issues | Select-Object id, title, service, status,
                classification, impactDescription, startDateTime, endDateTime,
                isResolved, lastModifiedDateTime
            $baseName = "ServiceIncidents_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        }

        foreach ($issue in $issues | Select-Object -First 10) {
            Write-Host "$(Get-Date) INFO: [$($issue.classification)] $($issue.service) — $($issue.title)"
        }
    }

    "messageCenter" {
        Write-Host "$(Get-Date) INFO: Fetching Message Center posts..."
        $uri = "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/messages"
        if ($MessageType) {
            $uri += "?`$filter=category eq '$MessageType'"
        }

        $messages = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 100
        Write-Host "$(Get-Date) INFO: Found $($messages.Count) message center post(s)."

        if ($messages.Count -gt 0) {
            $results = $messages | Select-Object id, title, category, severity,
                services, startDateTime, endDateTime, lastModifiedDateTime,
                isMajorChange, actionRequiredByDateTime
            $baseName = "MessageCenter_${safeTenant}_${ts}"
            Export-AuditData -Data $results -OutputFolder $OutputFolder -BaseName $baseName
        }

        foreach ($msg in $messages | Select-Object -First 10) {
            Write-Host "$(Get-Date) INFO: [$($msg.category)] $($msg.title)"
        }
    }

    "maintenance" {
        Write-Host "$(Get-Date) INFO: Fetching planned maintenance..."
        $uri = "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/issues?`$filter=classification eq 'advisory'"
        $maintenance = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 50
        Write-Host "$(Get-Date) INFO: Found $($maintenance.Count) maintenance item(s)."

        if ($maintenance.Count -gt 0) {
            $results = $maintenance | Select-Object id, title, service, status,
                startDateTime, endDateTime, impactDescription, isResolved
            $results | ConvertTo-Json -Depth 5
        }
    }

    "overview" {
        Write-Host "$(Get-Date) INFO: Generating service health overview..."

        # Service status
        $statusUri = "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/healthOverviews"
        $services = Invoke-GraphRequest -Uri $statusUri -AccessToken $accessToken -MaxPages 10
        $operational = ($services | Where-Object {$_.status -eq "serviceOperational"}).Count
        $degraded = $services.Count - $operational

        # Active incidents
        $incUri = "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/issues?`$filter=isResolved eq false"
        $incidents = Invoke-GraphRequest -Uri $incUri -AccessToken $accessToken -MaxPages 50

        $overview = [PSCustomObject]@{
            Tenant              = $Tenant
            ReportDate          = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
            TotalServices       = $services.Count
            OperationalServices = $operational
            DegradedServices    = $degraded
            ActiveIncidents     = $incidents.Count
        }

        $baseName = "ServiceHealthOverview_${safeTenant}_${ts}"
        Export-AuditData -Data @($overview) -OutputFolder $OutputFolder -BaseName $baseName
        Write-Host "$(Get-Date) INFO: Health Overview — Services: $($services.Count), Operational: $operational, Degraded: $degraded, Active incidents: $($incidents.Count)"
    }
}

Write-Host "$(Get-Date) INFO: Service health report complete."
