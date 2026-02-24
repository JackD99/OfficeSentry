# Get-SignInLogs.ps1
# Pulls Azure AD sign-in logs via Microsoft Graph API for a given date range.
# Useful for breach investigation: detecting impossible travel, suspicious IPs,
# failed auth attempts, and compromised accounts.
# Requires: MSAL.PS, ImportExcel (optional)

[CmdletBinding()]
param (
    [Parameter(Mandatory)][string] $Tenant,
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $CertificatePath,
    [Parameter(Mandatory)][string] $OutputFolder,
    [string] $StartDate  = ((Get-Date).AddDays(-7).ToString("yyyy-MM-dd")),
    [string] $EndDate    = ((Get-Date).ToString("yyyy-MM-dd")),
    [string] $TargetUser = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path (Split-Path $PSScriptRoot) "scripts\Shared.psm1"
if (Test-Path $sharedModule) { Import-Module $sharedModule -Force }

$CertificatePassword = Get-AuditCertificatePassword

Write-Host "$(Get-Date) INFO: Acquiring Graph token..."
$token = Get-AuditGraphToken `
    -Tenant              $Tenant `
    -ClientId            $ClientId `
    -CertificatePath     $CertificatePath `
    -CertificatePassword $CertificatePassword

$accessToken = $token.AccessToken

# Build filter
$startDT = "${StartDate}T00:00:00Z"
$endDT   = "${EndDate}T23:59:59Z"
$filter  = "createdDateTime ge $startDT and createdDateTime le $endDT"
if ($TargetUser) {
    $filter += " and userPrincipalName eq '$TargetUser'"
}

$select = "createdDateTime,userPrincipalName,userDisplayName,appDisplayName," +
          "ipAddress,clientAppUsed,resourceDisplayName,status,location," +
          "deviceDetail,riskDetail,riskLevelAggregated,riskLevelDuringSignIn," +
          "isInteractive,conditionalAccessStatus"

$uri = "https://graph.microsoft.com/v1.0/auditLogs/signIns?`$filter=$filter&`$select=$select&`$top=999&`$orderby=createdDateTime desc"

Write-Host "$(Get-Date) INFO: Fetching sign-in logs ($StartDate to $EndDate)..."
if ($TargetUser) { Write-Host "$(Get-Date) INFO: Filtering for: $TargetUser" }

$allSignIns = Invoke-GraphRequest -Uri $uri -AccessToken $accessToken -MaxPages 200

Write-Host "$(Get-Date) INFO: Retrieved $($allSignIns.Count) sign-in records."

if ($allSignIns.Count -eq 0) {
    Write-Host "$(Get-Date) INFO: No sign-in logs found for the specified criteria."
    return
}

# Parse into flat rows
$rows = [System.Collections.Generic.List[PSCustomObject]]::new()

foreach ($s in $allSignIns) {
    $errorCode = ""
    $failReason = ""
    if ($s.status) {
        $errorCode  = $s.status.errorCode
        $failReason = $s.status.failureReason
    }

    $city    = ""
    $state   = ""
    $country = ""
    if ($s.location) {
        $city    = $s.location.city
        $state   = $s.location.state
        $country = $s.location.countryOrRegion
    }

    $os      = ""
    $browser = ""
    if ($s.deviceDetail) {
        $os      = $s.deviceDetail.operatingSystem
        $browser = $s.deviceDetail.browser
    }

    $rows.Add([PSCustomObject]@{
        Timestamp       = $s.createdDateTime
        UserPrincipal   = $s.userPrincipalName
        DisplayName     = $s.userDisplayName
        Application     = $s.appDisplayName
        Resource        = $s.resourceDisplayName
        IPAddress       = $s.ipAddress
        ClientApp       = $s.clientAppUsed
        City            = $city
        State           = $state
        Country         = $country
        OS              = $os
        Browser         = $browser
        IsInteractive   = $s.isInteractive
        ErrorCode       = $errorCode
        FailureReason   = $failReason
        RiskLevel       = $s.riskLevelAggregated
        RiskDuringSignIn = $s.riskLevelDuringSignIn
        RiskDetail      = $s.riskDetail
        CAStatus        = $s.conditionalAccessStatus
    })
}

# Summary: unique IPs per user, failed logins, risky sign-ins
$ipSummary = $rows | Group-Object UserPrincipal, IPAddress, Country |
    Select-Object @{N='User';E={($_.Name -split ', ')[0]}},
                  @{N='IP';E={($_.Name -split ', ')[1]}},
                  @{N='Country';E={($_.Name -split ', ')[2]}},
                  Count |
    Sort-Object User, Count -Descending

$failedLogins = $rows | Where-Object { $_.ErrorCode -ne "0" -and $_.ErrorCode -ne "" }

$sheets = @(
    @{ Name = "AllSignIns";    Data = $rows.ToArray() }
    @{ Name = "IPSummary";     Data = $ipSummary }
    @{ Name = "FailedLogins";  Data = @($failedLogins) }
)

$target = if ($TargetUser) { "_$($TargetUser -replace '@','_')" } else { "" }
Export-AuditData -OutputFolder $OutputFolder -FilePrefix "SignInLogs${target}" -Sheets $sheets

Write-Host "$(Get-Date) INFO: Done. $($rows.Count) sign-in records, $($failedLogins.Count) failures."
