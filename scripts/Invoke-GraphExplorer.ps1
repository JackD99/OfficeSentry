#requires -Modules Microsoft.Graph.Authentication
<#
.SYNOPSIS
    Executes arbitrary Microsoft Graph API queries for a tenant.
.DESCRIPTION
    A flexible Graph Explorer that accepts a URI and HTTP method,
    authenticates using certificate-based auth, and returns the raw JSON response.
    Useful for ad-hoc lookups and troubleshooting.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tenant,
    [Parameter(Mandatory)][string]$ClientId,
    [Parameter(Mandatory)][string]$CertificatePath,
    [Parameter(Mandatory)][string]$GraphUri,
    [ValidateSet("GET", "POST", "PATCH", "DELETE")]
    [string]$Method = "GET",
    [string]$Body = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sharedModule = Join-Path $PSScriptRoot "Shared.psm1"
Import-Module $sharedModule -Force

try {
    $token = Get-AuditGraphToken -Tenant $Tenant -ClientId $ClientId -CertificatePath $CertificatePath
    $headers = @{
        Authorization  = "Bearer $token"
        "Content-Type" = "application/json"
    }

    Write-Host "$(Get-Date) INFO: Graph Explorer — $Method $GraphUri"

    $params = @{
        Uri     = $GraphUri
        Headers = $headers
        Method  = $Method
    }

    if ($Body -and $Method -in "POST", "PATCH") {
        $params.Body = $Body
    }

    $response = Invoke-RestMethod @params

    Write-Host "$(Get-Date) INFO: Graph Explorer — response received"

    $output = @{
        ok       = $true
        method   = $Method
        uri      = $GraphUri
        response = $response
    }
    $output | ConvertTo-Json -Depth 10 -Compress
}
catch {
    $statusCode = $null
    if ($_.Exception.Response) {
        $statusCode = [int]$_.Exception.Response.StatusCode
    }
    Write-Host "$(Get-Date) ERROR: $_"
    @{
        ok         = $false
        error      = "$_"
        statusCode = $statusCode
        method     = $Method
        uri        = $GraphUri
    } | ConvertTo-Json -Compress
}
