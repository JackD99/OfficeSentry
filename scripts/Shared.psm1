# Shared.psm1
# Common functions and constants used across all audit scripts.

# ---- Excluded system lists (SharePoint hidden/system lists to skip) ----

$script:ExcludedSystemLists = @(
    "App Packages","appdata","appfiles","Apps in Testing","Cache Profiles",
    "Composed Looks","Content and Structure Reports",
    "Content type publishing error log","Converted Forms",
    "Device Channels","Form Templates","fpdatasources",
    "Get started with Apps for Office and SharePoint",
    "List Template Gallery","Long Running Operation Status",
    "Maintenance Log Library","Style Library","Master Docs","Master Page Gallery",
    "MicroFeed","NintexFormXml","Quick Deploy Items","Relationships List",
    "Reusable Content","Search Config List","Solution Gallery",
    "Site Collection Images","Suggested Content Browser Locations",
    "TaxonomyHiddenList","User Information List","Web Part Gallery",
    "wfpub","wfsvc","Workflow History","Workflow Tasks",
    "Preservation Hold Library","SharePointHomeCacheList"
)

function Get-ExcludedSystemLists {
    return $script:ExcludedSystemLists
}

# ---- PFX Password handling ----

function Get-AuditCertificatePassword {
    <#
    .SYNOPSIS
    Returns the PFX password as a SecureString.
    Checks the explicit parameter first, then the AUDIT_PFX_PASSWORD environment
    variable, then prompts interactively.
    #>
    param(
        [Parameter(Mandatory = $false)]
        [securestring] $CertificatePassword
    )

    if ($null -ne $CertificatePassword) {
        return $CertificatePassword
    }

    if ($env:AUDIT_PFX_PASSWORD) {
        return (ConvertTo-SecureString $env:AUDIT_PFX_PASSWORD -AsPlainText -Force)
    }

    return (Read-Host -Prompt "PFX password" -AsSecureString)
}

# ---- Connection helpers ----

function Connect-AuditSite {
    <#
    .SYNOPSIS
    Connects to a SharePoint site via PnP.PowerShell with cert-based auth.
    Includes retry logic for transient failures.
    #>
    param(
        [Parameter(Mandatory)][string] $SiteUrl,
        [Parameter(Mandatory)][string] $ClientId,
        [Parameter(Mandatory)][string] $Tenant,
        [Parameter(Mandatory)][string] $CertificatePath,
        [Parameter(Mandatory)][securestring] $CertificatePassword,
        [int] $MaxRetries = 3
    )

    for ($i = 0; $i -lt $MaxRetries; $i++) {
        try {
            Connect-PnPOnline `
                -Url                 $SiteUrl `
                -ClientId            $ClientId `
                -Tenant              $Tenant `
                -CertificatePath     $CertificatePath `
                -CertificatePassword $CertificatePassword
            return
        }
        catch {
            if ($i -eq $MaxRetries - 1) { throw }
            $msg = $_.Exception.Message
            $isTransient = ($msg -match 'throttl|timeout|429|503|temporarily|network|connect')
            if (-not $isTransient) { throw }
            $wait = [math]::Pow(2, $i + 1)
            Write-Host "$(Get-Date) WARN: Retrying connection to $SiteUrl in ${wait}s ($($i+1)/$($MaxRetries-1))..."
            Start-Sleep -Seconds $wait
        }
    }
}

function Get-AuditGraphToken {
    <#
    .SYNOPSIS
    Acquires a Graph API token using certificate-based auth via MSAL.PS.
    Securely handles the PFX password.
    #>
    param(
        [Parameter(Mandatory)][string] $Tenant,
        [Parameter(Mandatory)][string] $ClientId,
        [Parameter(Mandatory)][string] $CertificatePath,
        [Parameter(Mandatory)][securestring] $CertificatePassword,
        [string] $Scope = "https://graph.microsoft.com/.default"
    )

    $bstr  = $null
    $plain = $null
    try {
        $bstr  = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($CertificatePassword)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)

        $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
        $cert  = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
            $CertificatePath, $plain, $flags
        )

        return Get-MsalToken `
            -TenantId          $Tenant `
            -ClientId          $ClientId `
            -ClientCertificate $cert `
            -Scopes            $Scope
    }
    finally {
        if ($null -ne $bstr) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        $plain = $null
    }
}

function Invoke-GraphRequest {
    <#
    .SYNOPSIS
    Makes a paginated Graph API GET request with automatic token handling.
    Returns all results across all pages.
    #>
    param(
        [Parameter(Mandatory)][string] $Uri,
        [Parameter(Mandatory)][string] $AccessToken,
        [int] $MaxPages = 100
    )

    $headers = @{ Authorization = "Bearer $AccessToken" }
    $all = [System.Collections.Generic.List[PSCustomObject]]::new()
    $page = 0

    while ($Uri -and $page -lt $MaxPages) {
        $page++
        $resp = Invoke-RestMethod -Uri $Uri -Method GET -Headers $headers
        if ($resp.value) {
            foreach ($item in $resp.value) { $all.Add($item) }
        }
        $Uri = $resp.'@odata.nextLink'
    }

    return $all.ToArray()
}

# ---- Principal type classification ----

function Get-PrincipalType {
    param([string]$LoginName)
    if ($LoginName -match '^c:0t\.c\|tenant\|' -or $LoginName -match 'spo-grid-all-users') { return "Everyone" }
    if ($LoginName -match '#ext#|urn:spo:guest') { return "External" }
    if ($LoginName -match '^i:0#\.f\|membership\|') { return "User" }
    if ($LoginName -match '^c:0o\.c\|federateddirectoryclaimprovider\|') { return "M365 Group" }
    if ($LoginName -match 'sharepoint\|') { return "SP Group" }
    return "Other"
}

# ---- Export helpers ----

function Export-AuditData {
    <#
    .SYNOPSIS
    Exports audit data to XLSX (preferred) or CSV (fallback).
    Returns the path of the created file.
    #>
    param(
        [Parameter(Mandatory)][string] $OutputFolder,
        [Parameter(Mandatory)][string] $FilePrefix,
        [Parameter(Mandatory)][array]  $Sheets  # Array of @{ Name = "Sheet1"; Data = @(...) }
    )

    if (-not (Test-Path $OutputFolder)) {
        New-Item -ItemType Directory -Path $OutputFolder | Out-Null
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmm"

    if (Get-Module -ListAvailable -Name ImportExcel) {
        Import-Module ImportExcel -ErrorAction Stop
        $xlsx = Join-Path $OutputFolder "${FilePrefix}_${stamp}.xlsx"

        foreach ($sheet in $Sheets) {
            if ($sheet.Data -and $sheet.Data.Count -gt 0) {
                $params = @{
                    Path          = $xlsx
                    WorksheetName = $sheet.Name
                    AutoFilter    = $true
                    BoldTopRow    = $true
                    FreezeTopRow  = $true
                    AutoSize      = $true
                }
                if ($Sheets.IndexOf($sheet) -eq 0) { $params.ClearSheet = $true }
                $sheet.Data | Export-Excel @params
            }
        }

        Write-Host "$(Get-Date) INFO: Report saved to $xlsx" -ForegroundColor Green
        return $xlsx
    }
    else {
        $paths = @()
        foreach ($sheet in $Sheets) {
            if ($sheet.Data -and $sheet.Data.Count -gt 0) {
                $csv = Join-Path $OutputFolder "${FilePrefix}_$($sheet.Name)_${stamp}.csv"
                $sheet.Data | Export-Csv -Path $csv -NoTypeInformation
                $paths += $csv
            }
        }
        Write-Host "$(Get-Date) WARN: ImportExcel not found — exported CSV." -ForegroundColor Yellow
        foreach ($p in $paths) { Write-Host "  $p" }
        return $paths[0]
    }
}

Export-ModuleMember -Function @(
    'Get-ExcludedSystemLists',
    'Get-AuditCertificatePassword',
    'Connect-AuditSite',
    'Get-AuditGraphToken',
    'Invoke-GraphRequest',
    'Get-PrincipalType',
    'Export-AuditData'
)
