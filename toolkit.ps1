# toolkit.ps1
# Portable SPO Audit Toolkit (native cert, local modules, app setup + reporting menu)

#requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Session-level PFX password cache — prompt once per run, reuse for all audit options
$script:SessionPfxPassword = $null

# ------------------- Utility -------------------

function Get-Root {
  Split-Path -Parent $PSCommandPath
}

function Ensure-Folders {
  param([string]$Root)
  foreach ($d in @("config","certs","output","scripts","modules","bin")) {
    $p = Join-Path $Root $d
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
  }
}

function Set-LocalModulePath {
  param([string]$Root)
  $localModules = Join-Path $Root "modules"
  if ($env:PSModulePath -notlike "*$localModules*") {
    $env:PSModulePath = "$localModules;$($env:PSModulePath)"
  }
}

function Read-JsonFile {
  param([string]$Path)
  if (-not (Test-Path $Path)) { throw "Config not found: $Path" }
  Get-Content $Path -Raw | ConvertFrom-Json
}

function Write-JsonFile {
  param([string]$Path, $Object)
  $Object | ConvertTo-Json -Depth 10 | Set-Content -Path $Path -Encoding UTF8
}

function Resolve-RelPath {
  param([string]$Root, [string]$MaybeRelative)
  if ([string]::IsNullOrWhiteSpace($MaybeRelative)) { return $null }
  if ([IO.Path]::IsPathRooted($MaybeRelative)) { return $MaybeRelative }
  return (Join-Path $Root $MaybeRelative)
}

function Assert-ModulePresent {
  param([string]$ModuleName)
  if (-not (Get-Module -ListAvailable -Name $ModuleName)) {
    throw "Missing module '$ModuleName'. Run Build-Toolkit.ps1 to download modules."
  }
}

function Get-SessionPfxPassword {
  <# Returns cached PFX password, prompting once if not yet set. #>
  if ($null -eq $script:SessionPfxPassword) {
    $script:SessionPfxPassword = Read-Host -Prompt "Enter PFX password (cached for this session)" -AsSecureString
  }
  return $script:SessionPfxPassword
}

function Ensure-ConfigBasics {
  param($cfg)
  if ([string]::IsNullOrWhiteSpace($cfg.TenantId) -and [string]::IsNullOrWhiteSpace($cfg.TenantDomain)) {
    Write-Host "Tenant not configured. Set it now (option 3) or enter values below."
    $tenantId     = Read-Host "Tenant ID (GUID) — leave blank to use domain instead"
    $tenantDomain = Read-Host "Tenant domain (e.g. contoso.onmicrosoft.com)"
    if ($tenantId)     { $cfg.TenantId     = $tenantId }
    if ($tenantDomain) { $cfg.TenantDomain = $tenantDomain }
  }
}

function Get-ConfiguredTenant {
  <# Returns TenantId if set, else TenantDomain, else $null. #>
  param($cfg)
  if (-not [string]::IsNullOrWhiteSpace($cfg.TenantId))     { return $cfg.TenantId }
  if (-not [string]::IsNullOrWhiteSpace($cfg.TenantDomain)) { return $cfg.TenantDomain }
  return $null
}

function Assert-SetupComplete {
  <#
  Verifies that the minimum required config is present before running audit options.
  Returns $true if all checks pass, $false otherwise (prints guidance).
  #>
  param($cfg, [string]$Root)

  $ok = $true

  if ([string]::IsNullOrWhiteSpace($cfg.ClientId)) {
    Write-Host "  [!] ClientId not set — run option 2 to create the Entra app first." -ForegroundColor Yellow
    $ok = $false
  }

  $tenant = Get-ConfiguredTenant -cfg $cfg
  if ($null -eq $tenant) {
    Write-Host "  [!] Tenant not configured — run option 3 to set TenantId/TenantDomain." -ForegroundColor Yellow
    $ok = $false
  }

  $pfxPath = Resolve-RelPath -Root $Root -MaybeRelative $cfg.Certificate.PfxPath
  if ($null -eq $pfxPath -or -not (Test-Path $pfxPath)) {
    Write-Host "  [!] PFX certificate not found at '$pfxPath' — run option 1 to generate it." -ForegroundColor Yellow
    $ok = $false
  }

  return $ok
}

function Show-SetupStatus {
  <# Prints a status block at the top of the menu. #>
  param($cfg, [string]$Root)

  $tenant  = Get-ConfiguredTenant -cfg $cfg
  $clientId = $cfg.ClientId
  $pfxPath  = Resolve-RelPath -Root $Root -MaybeRelative $cfg.Certificate.PfxPath
  $cerPath  = Resolve-RelPath -Root $Root -MaybeRelative $cfg.Certificate.CerPath
  $outPath  = Resolve-RelPath -Root $Root -MaybeRelative $cfg.Defaults.OutputFolder

  # Cert expiry
  $certStatus = "[NOT FOUND]"
  if ($pfxPath -and (Test-Path $pfxPath)) {
    try {
      $c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($pfxPath)
      $daysLeft = ($c.NotAfter - (Get-Date)).Days
      $certStatus = "Expires $($c.NotAfter.ToString('yyyy-MM-dd')) ($daysLeft days)"
      if ($daysLeft -lt 30) { $certStatus += " [EXPIRING SOON]" }
    } catch { $certStatus = "[FOUND — could not read expiry]" }
  }

  Write-Host "  Tenant   : $(if ($tenant)   { $tenant }   else { '[NOT SET — run option 3]' })"
  Write-Host "  ClientId : $(if ($clientId) { $clientId } else { '[NOT SET — run option 2]' })"
  Write-Host "  Cert     : $certStatus"
  Write-Host "  Output   : $(if ($outPath)  { $outPath }  else { '[NOT SET]' })"
  if ($script:SessionPfxPassword) {
    Write-Host "  PFX pass : [cached for this session]"
  }
}

# ------------------- Certificate -------------------

function New-PortableSelfSignedCert {
  param(
    [Parameter(Mandatory)][string]$Subject,
    [Parameter(Mandatory)][string]$CerPath,
    [Parameter(Mandatory)][string]$PfxPath,
    [Parameter(Mandatory)][int]$YearsValid,
    [Parameter(Mandatory)][securestring]$PfxPassword
  )

  if (-not (Get-Command New-SelfSignedCertificate -ErrorAction SilentlyContinue)) {
    throw "New-SelfSignedCertificate not available. Run on Windows with cert cmdlets."
  }

  $notAfter = (Get-Date).AddYears($YearsValid)

  $cert = New-SelfSignedCertificate `
    -Subject $Subject `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -HashAlgorithm "SHA256" `
    -KeyExportPolicy Exportable `
    -NotAfter $notAfter `
    -KeySpec Signature

  Export-Certificate    -Cert $cert -FilePath $CerPath -Force | Out-Null
  Export-PfxCertificate -Cert $cert -FilePath $PfxPath -Password $PfxPassword -Force | Out-Null

  if (-not (Test-Path $CerPath)) { throw "CER export failed: $CerPath" }
  if (-not (Test-Path $PfxPath)) { throw "PFX export failed: $PfxPath" }

  [PSCustomObject]@{
    Thumbprint = $cert.Thumbprint
    NotAfter   = $cert.NotAfter
    Subject    = $cert.Subject
    CerPath    = $CerPath
    PfxPath    = $PfxPath
  }
}

# ------------------- Entra App Setup -------------------

function Connect-GraphForAppSetup {
  Assert-ModulePresent "Microsoft.Graph.Authentication"
  Assert-ModulePresent "Microsoft.Graph.Applications"

  Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
  Import-Module Microsoft.Graph.Applications    -ErrorAction Stop

  $scopes = @(
    "Application.ReadWrite.All",
    "Directory.Read.All",
    "AppRoleAssignment.ReadWrite.All"
  )

  Connect-MgGraph -Scopes $scopes | Out-Null
}

function New-EntraAppWithCertAndPerms {
  param(
    [Parameter(Mandatory)][string]$AppDisplayName,
    [Parameter(Mandatory)][string]$CerPath,
    [Parameter(Mandatory)][int]$CertYearsValid
  )

  $app = New-MgApplication -DisplayName $AppDisplayName
  $sp  = New-MgServicePrincipal -AppId $app.AppId

  $now      = Get-Date
  $end      = $now.AddYears($CertYearsValid)
  $cerBytes = [IO.File]::ReadAllBytes($CerPath)

  $keyCredential = @{
    Type          = "AsymmetricX509Cert"
    Usage         = "Verify"
    Key           = $cerBytes
    DisplayName   = "SPO Toolkit Cert"
    StartDateTime = $now
    EndDateTime   = $end
  }

  Update-MgApplication -ApplicationId $app.Id -KeyCredentials @($keyCredential)

  function Grant-AppRole {
    param(
      [Parameter(Mandatory)][string]$ResourceAppId,
      [Parameter(Mandatory)][string]$RoleValue
    )
    $resourceSp = Get-MgServicePrincipal -Filter "appId eq '$ResourceAppId'" -ConsistencyLevel eventual
    if (-not $resourceSp) { throw "Could not find service principal for appId $ResourceAppId" }

    $role = $resourceSp.AppRoles |
      Where-Object { $_.Value -eq $RoleValue -and $_.AllowedMemberTypes -contains "Application" } |
      Select-Object -First 1

    if (-not $role) { throw "Could not find AppRole '$RoleValue' on appId $ResourceAppId" }

    New-MgServicePrincipalAppRoleAssignment `
      -ServicePrincipalId $sp.Id `
      -PrincipalId        $sp.Id `
      -ResourceId         $resourceSp.Id `
      -AppRoleId          $role.Id | Out-Null
  }

  $spoResourceAppId   = "00000003-0000-0ff1-ce00-000000000000" # SharePoint Online
  $graphResourceAppId = "00000003-0000-0000-c000-000000000000" # Microsoft Graph

  # Sites.Read.All is sufficient for read-only auditing
  Grant-AppRole -ResourceAppId $spoResourceAppId   -RoleValue "Sites.Read.All"
  Grant-AppRole -ResourceAppId $graphResourceAppId -RoleValue "Directory.Read.All"

  [PSCustomObject]@{
    ClientId           = $app.AppId
    ApplicationId      = $app.Id
    ServicePrincipalId = $sp.Id
    DisplayName        = $app.DisplayName
  }
}

function Remove-EntraApp {
  <#
  Removes the Entra app registration, service principal, local cert files,
  and clears ClientId from config.
  #>
  param($cfg, [string]$Root, [string]$ConfigPath)

  if ([string]::IsNullOrWhiteSpace($cfg.ClientId)) {
    Write-Host "No ClientId configured — nothing to remove." -ForegroundColor Yellow
    return
  }

  Assert-ModulePresent "Microsoft.Graph.Authentication"
  Assert-ModulePresent "Microsoft.Graph.Applications"
  Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
  Import-Module Microsoft.Graph.Applications    -ErrorAction Stop

  Write-Host "Connecting to Microsoft Graph to remove app '$($cfg.AppDisplayName)' ($($cfg.ClientId))..."
  Connect-MgGraph -Scopes "Application.ReadWrite.All" | Out-Null

  $app = Get-MgApplication -Filter "appId eq '$($cfg.ClientId)'" -ErrorAction SilentlyContinue
  if ($app) {
    Remove-MgApplication -ApplicationId $app.Id
    Write-Host "  App registration removed from Entra ID." -ForegroundColor Green
  } else {
    Write-Host "  App not found in Entra ID (may have already been deleted)." -ForegroundColor Yellow
  }

  # Delete local cert files
  foreach ($relPath in @($cfg.Certificate.PfxPath, $cfg.Certificate.CerPath)) {
    $fullPath = Resolve-RelPath -Root $Root -MaybeRelative $relPath
    if ($fullPath -and (Test-Path $fullPath)) {
      Remove-Item $fullPath -Force
      Write-Host "  Deleted: $fullPath" -ForegroundColor Green
    }
  }

  # Clear ClientId from config
  $cfg.ClientId = ""
  Write-JsonFile -Path $ConfigPath -Object $cfg

  # Clear session password cache
  $script:SessionPfxPassword = $null

  Write-Host ""
  Write-Host "Cleanup complete. Run options 1 and 2 to set up again for a new engagement." -ForegroundColor Cyan
}

# ------------------- Shared audit helper -------------------

function Get-AuditParams {
  <#
  Returns a hashtable of common parameters needed by all audit scripts.
  Ensures setup is complete and PFX password is cached.
  #>
  param($cfg, [string]$Root, [bool]$NeedSiteUrl = $true)

  if (-not (Assert-SetupComplete -cfg $cfg -Root $Root)) {
    return $null
  }

  $tenant  = Get-ConfiguredTenant -cfg $cfg
  $pfxPath = Resolve-RelPath -Root $Root -MaybeRelative $cfg.Certificate.PfxPath
  $pfxPass = Get-SessionPfxPassword
  $outDir  = Resolve-RelPath -Root $Root -MaybeRelative $cfg.Defaults.OutputFolder

  if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }

  $params = @{
    ClientId            = $cfg.ClientId
    Tenant              = $tenant
    CertificatePath     = $pfxPath
    CertificatePassword = $pfxPass
    OutputFolder        = $outDir
  }

  if ($NeedSiteUrl) {
    $site = $cfg.Defaults.SiteUrl
    if ([string]::IsNullOrWhiteSpace($site)) { $site = Read-Host "Site URL (e.g. https://contoso.sharepoint.com/sites/Finance)" }
    if ([string]::IsNullOrWhiteSpace($site)) {
      Write-Host "No site URL provided." -ForegroundColor Yellow
      return $null
    }
    $params.SiteUrl = $site
  }

  return $params
}

# ------------------- Main -------------------

$root       = Get-Root
Ensure-Folders    -Root $root
Set-LocalModulePath -Root $root

$configPath = Join-Path $root "config\settings.json"
$cfg        = Read-JsonFile -Path $configPath

while ($true) {
  Clear-Host
  Write-Host "============================================"
  Write-Host "  SPO Audit Toolkit"
  Write-Host "============================================"
  Show-SetupStatus -cfg $cfg -Root $root
  Write-Host "--------------------------------------------"
  Write-Host ""
  Write-Host "  --- Setup ---"
  Write-Host "  1) Generate self-signed cert (CER + PFX)"
  Write-Host "  2) Create Entra app + upload cert + assign permissions"
  Write-Host "  3) Configure tenant, site URL, output folder"
  Write-Host ""
  Write-Host "  --- Audit Reports ---"
  Write-Host "  4) Groups audit          (site groups + members)"
  Write-Host "  5) Site permissions      (full permission snapshot)"
  Write-Host "  6) External sharing      (external users + links)"
  Write-Host "  7) User permissions      (one user across all sites)"
  Write-Host ""
  Write-Host "  --- Cleanup ---"
  Write-Host "  8) Remove Entra app + delete local certs"
  Write-Host ""
  Write-Host "  0) Exit"
  Write-Host ""

  $choice = Read-Host "Select"

  switch ($choice) {

    "1" {
      $cerPath = Resolve-RelPath -Root $root -MaybeRelative $cfg.Certificate.CerPath
      $pfxPath = Resolve-RelPath -Root $root -MaybeRelative $cfg.Certificate.PfxPath
      $subject = $cfg.Certificate.Subject
      $years   = [int]$cfg.Certificate.YearsValid

      # Reset cached password since we're creating a new cert
      $script:SessionPfxPassword = $null
      $pfxPass = Get-SessionPfxPassword

      $result = New-PortableSelfSignedCert -Subject $subject -CerPath $cerPath -PfxPath $pfxPath -YearsValid $years -PfxPassword $pfxPass

      Write-Host ""
      Write-Host "Certificate created:" -ForegroundColor Green
      Write-Host "  CER        : $($result.CerPath)"
      Write-Host "  PFX        : $($result.PfxPath)"
      Write-Host "  Thumbprint : $($result.Thumbprint)"
      Write-Host "  Expires    : $($result.NotAfter.ToString('yyyy-MM-dd'))"
      Pause
    }

    "2" {
      Ensure-ConfigBasics -cfg $cfg
      Write-JsonFile -Path $configPath -Object $cfg

      $cerPath = Resolve-RelPath -Root $root -MaybeRelative $cfg.Certificate.CerPath
      if ($null -eq $cerPath -or -not (Test-Path $cerPath)) {
        Write-Host "CER not found at '$cerPath'. Run option 1 first." -ForegroundColor Yellow
        Pause
        break
      }

      Assert-ModulePresent "Microsoft.Graph.Authentication"
      Assert-ModulePresent "Microsoft.Graph.Applications"

      Connect-GraphForAppSetup
      $appInfo = New-EntraAppWithCertAndPerms `
        -AppDisplayName $cfg.AppDisplayName `
        -CerPath        $cerPath `
        -CertYearsValid ([int]$cfg.Certificate.YearsValid)

      $cfg.ClientId = $appInfo.ClientId
      Write-JsonFile -Path $configPath -Object $cfg

      Write-Host ""
      Write-Host "App created:" -ForegroundColor Green
      Write-Host "  Display name : $($appInfo.DisplayName)"
      Write-Host "  ClientId     : $($appInfo.ClientId)"
      Write-Host ""
      Write-Host "IMPORTANT: Some tenants require 'Grant admin consent' in the Entra portal."
      Write-Host "           If audits fail with 'insufficient privileges', grant consent there."
      Pause
    }

    "3" {
      Write-Host "(Leave a field blank to keep the current value)"
      $v = Read-Host "Tenant ID (GUID) — current: '$($cfg.TenantId)'"
      if ($v) { $cfg.TenantId = $v }
      $v = Read-Host "Tenant domain (e.g. contoso.onmicrosoft.com) — current: '$($cfg.TenantDomain)'"
      if ($v) { $cfg.TenantDomain = $v }
      $v = Read-Host "Default site URL — current: '$($cfg.Defaults.SiteUrl)'"
      if ($v) { $cfg.Defaults.SiteUrl = $v }
      $v = Read-Host "Output folder (relative or absolute) — current: '$($cfg.Defaults.OutputFolder)'"
      if ($v) { $cfg.Defaults.OutputFolder = $v }
      Write-JsonFile -Path $configPath -Object $cfg
      Write-Host "Saved." -ForegroundColor Green
      Pause
    }

    "4" {
      $p = Get-AuditParams -cfg $cfg -Root $root
      if ($null -eq $p) { Pause; break }

      Assert-ModulePresent "PnP.PowerShell"
      Import-Module PnP.PowerShell -ErrorAction Stop

      & (Join-Path $root "scripts\Audit-Groups.ps1") `
        -SiteUrl            $p.SiteUrl `
        -OutputFolder       $p.OutputFolder `
        -ClientId           $p.ClientId `
        -Tenant             $p.Tenant `
        -CertificatePath    $p.CertificatePath `
        -CertificatePassword $p.CertificatePassword
      Pause
    }

    "5" {
      $p = Get-AuditParams -cfg $cfg -Root $root
      if ($null -eq $p) { Pause; break }

      Assert-ModulePresent "PnP.PowerShell"
      Import-Module PnP.PowerShell -ErrorAction Stop

      & (Join-Path $root "scripts\Audit-SitePermissions.ps1") `
        -SiteUrl            $p.SiteUrl `
        -OutputFolder       $p.OutputFolder `
        -ClientId           $p.ClientId `
        -Tenant             $p.Tenant `
        -CertificatePath    $p.CertificatePath `
        -CertificatePassword $p.CertificatePassword
      Pause
    }

    "6" {
      $p = Get-AuditParams -cfg $cfg -Root $root
      if ($null -eq $p) { Pause; break }

      Assert-ModulePresent "PnP.PowerShell"
      Import-Module PnP.PowerShell -ErrorAction Stop

      & (Join-Path $root "scripts\Audit-ExternalSharing.ps1") `
        -SiteUrl            $p.SiteUrl `
        -OutputFolder       $p.OutputFolder `
        -ClientId           $p.ClientId `
        -Tenant             $p.Tenant `
        -CertificatePath    $p.CertificatePath `
        -CertificatePassword $p.CertificatePassword
      Pause
    }

    "7" {
      Assert-ModulePresent "PnP.PowerShell"
      Assert-ModulePresent "MSAL.PS"

      $p = Get-AuditParams -cfg $cfg -Root $root -NeedSiteUrl $false
      if ($null -eq $p) { Pause; break }

      $userEmail = Read-Host "User email (UPN) to audit"
      if ([string]::IsNullOrWhiteSpace($userEmail)) {
        Write-Host "No email provided." -ForegroundColor Yellow
        Pause
        break
      }

      $stamp  = Get-Date -Format "yyyyMMdd-HHmm"
      $csv    = Join-Path $p.OutputFolder "UserPerms_${stamp}.csv"
      $xlsx   = Join-Path $p.OutputFolder "UserPerms_${stamp}.xlsx"

      & (Join-Path $root "scripts\Get-SharePointTenantPermissions.ps1") `
        -Tenant             $p.Tenant `
        -UserEmail          $userEmail `
        -CSVPath            $csv `
        -XlsxPath           $xlsx `
        -ClientId           $p.ClientId `
        -CertificatePath    $p.CertificatePath `
        -CertificatePassword $p.CertificatePassword
      Pause
    }

    "8" {
      $confirm = Read-Host "This will delete the Entra app and local cert files. Type YES to confirm"
      if ($confirm -eq "YES") {
        Remove-EntraApp -cfg $cfg -Root $root -ConfigPath $configPath
        # Reload config after save
        $cfg = Read-JsonFile -Path $configPath
      } else {
        Write-Host "Cancelled." -ForegroundColor Yellow
      }
      Pause
    }

    "0" {
      $script:SessionPfxPassword = $null
      break
    }

    default { }
  }

  # Exit the while loop when choice is "0"
  if ($choice -eq "0") { break }
}
