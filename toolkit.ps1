# toolkit.ps1
# Portable SPO Audit Toolkit (native cert, local modules, app setup + reporting menu)

#requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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
  $localModules = Join-Path $Root "bin\pwsh"
  $env:PSModulePath = "$localModules;$($env:PSModulePath)"
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
    throw "Missing module '$ModuleName' in local modules folder. Put it under .\modules\ (pre-packaged)."
  }
}

function Prompt-SecurePassword {
  param([string]$Prompt = "Enter PFX password")
  Read-Host -Prompt $Prompt -AsSecureString
}

function Ensure-ConfigBasics {
  param($cfg)

  if ([string]::IsNullOrWhiteSpace($cfg.TenantId) -and [string]::IsNullOrWhiteSpace($cfg.TenantDomain)) {
    Write-Host "TenantId / TenantDomain not set. You can set them now."
    $tenantId = Read-Host "Tenant ID (GUID) (optional if you set tenant domain)"
    $tenantDomain = Read-Host "Tenant domain (e.g. contoso.onmicrosoft.com) (optional if you set tenant ID)"
    if ($tenantId) { $cfg.TenantId = $tenantId }
    if ($tenantDomain) { $cfg.TenantDomain = $tenantDomain }
  }
}

function New-PortableSelfSignedCert {
  param(
    [Parameter(Mandatory)][string]$Subject,
    [Parameter(Mandatory)][string]$CerPath,
    [Parameter(Mandatory)][string]$PfxPath,
    [Parameter(Mandatory)][int]$YearsValid,
    [Parameter(Mandatory)][securestring]$PfxPassword
  )

  if (-not (Get-Command New-SelfSignedCertificate -ErrorAction SilentlyContinue)) {
    throw "New-SelfSignedCertificate not available. Run on Windows PowerShell 5.1+ / Windows with cert cmdlets."
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

  Export-Certificate -Cert $cert -FilePath $CerPath -Force | Out-Null
  Export-PfxCertificate -Cert $cert -FilePath $PfxPath -Password $PfxPassword -Force | Out-Null

  [PSCustomObject]@{
    Thumbprint = $cert.Thumbprint
    NotAfter   = $cert.NotAfter
    Subject    = $cert.Subject
    CerPath    = $CerPath
    PfxPath    = $PfxPath
  }
}

function Connect-GraphForAppSetup {
  Assert-ModulePresent "Microsoft.Graph.Authentication"
  Assert-ModulePresent "Microsoft.Graph.Applications"

  Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
  Import-Module Microsoft.Graph.Applications -ErrorAction Stop

  # App creation + SP creation + app role assignment
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
    [Parameter(Mandatory)][int]$CertYearsValid = 2
  )

  # Create app registration
  $app = New-MgApplication -DisplayName $AppDisplayName

  # Create service principal
  $sp = New-MgServicePrincipal -AppId $app.AppId

  # Upload certificate to app (keyCredentials)
  $now = Get-Date
  $end = $now.AddYears($CertYearsValid)
  $cerBytes = [IO.File]::ReadAllBytes($CerPath)

  $keyCredential = @{
    Type  = "AsymmetricX509Cert"
    Usage = "Verify"
    Key   = $cerBytes
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
    if (-not $resourceSp) { throw "Could not find resource service principal for appId $ResourceAppId" }

    $role = $resourceSp.AppRoles |
      Where-Object { $_.Value -eq $RoleValue -and $_.AllowedMemberTypes -contains "Application" } |
      Select-Object -First 1

    if (-not $role) { throw "Could not find AppRole '$RoleValue' on resource appId $ResourceAppId" }

    New-MgServicePrincipalAppRoleAssignment `
      -ServicePrincipalId $sp.Id `
      -PrincipalId $sp.Id `
      -ResourceId $resourceSp.Id `
      -AppRoleId $role.Id | Out-Null
  }

  # Resource app IDs
  $spoResourceAppId   = "00000003-0000-0ff1-ce00-000000000000" # SharePoint Online
  $graphResourceAppId = "00000003-0000-0000-c000-000000000000" # Microsoft Graph

  # Permissions required for tenant-wide enumeration + your example script:
  # - SPO: Sites.FullControl.All (Application)
  # - Graph: Directory.Read.All (Application)
  Grant-AppRole -ResourceAppId $spoResourceAppId   -RoleValue "Sites.FullControl.All"
  Grant-AppRole -ResourceAppId $graphResourceAppId -RoleValue "Directory.Read.All"

  [PSCustomObject]@{
    ClientId            = $app.AppId
    ApplicationId       = $app.Id
    ServicePrincipalId  = $sp.Id
    DisplayName         = $app.DisplayName
  }
}

function Connect-PnPFromConfig {
  param(
    [Parameter(Mandatory)][string]$SiteUrl,
    [Parameter(Mandatory)]$cfg,
    [Parameter(Mandatory)][securestring]$PfxPassword,
    [Parameter(Mandatory)][string]$Root
  )

  Assert-ModulePresent "PnP.PowerShell"
  Import-Module PnP.PowerShell -ErrorAction Stop

  if ([string]::IsNullOrWhiteSpace($cfg.ClientId)) { throw "ClientId not set in config." }

  $tenant = $cfg.TenantId
  if ([string]::IsNullOrWhiteSpace($tenant)) { $tenant = $cfg.TenantDomain }
  if ([string]::IsNullOrWhiteSpace($tenant)) { throw "Set TenantId or TenantDomain in config." }

  $pfxPath = Resolve-RelPath -Root $Root -MaybeRelative $cfg.Certificate.PfxPath
  if (-not (Test-Path $pfxPath)) { throw "PFX not found: $pfxPath" }

  Connect-PnPOnline -Url $SiteUrl -ClientId $cfg.ClientId -Tenant $tenant -CertificatePath $pfxPath -CertificatePassword $PfxPassword
}

# ---------------- Main ----------------
$root = Get-Root
Ensure-Folders -Root $root
Set-LocalModulePath -Root $root

$configPath = Join-Path $root "config\settings.json"
$cfg = Read-JsonFile -Path $configPath

while ($true) {
  Clear-Host
  Write-Host "=== SPO Audit Toolkit (Portable) ==="
  Write-Host "Config: $configPath"
  Write-Host ""
  Write-Host "1) Generate self-signed cert (CER + PFX) [native, no OpenSSL]"
  Write-Host "2) Create Entra App + upload CER + assign SPO+Graph app permissions (Graph login required)"
  Write-Host "3) Set TenantId/TenantDomain, default SiteUrl, output folder"
  Write-Host "4) Run: Audit Groups"
  Write-Host "5) Run: Audit Site Permissions"
  Write-Host "6) Run: Audit External Sharing"
  Write-Host "7) Run: Tenant User Permissions (all sites)"
  Write-Host "0) Exit"
  Write-Host ""

  $choice = Read-Host "Select"
  switch ($choice) {
    "1" {
      $cerPath = Resolve-RelPath -Root $root -MaybeRelative $cfg.Certificate.CerPath
      $pfxPath = Resolve-RelPath -Root $root -MaybeRelative $cfg.Certificate.PfxPath
      $subject = $cfg.Certificate.Subject
      $years   = [int]$cfg.Certificate.YearsValid

      $pfxPass = Prompt-SecurePassword -Prompt "Enter a password for the PFX (you'll need it each run)"
      $result = New-PortableSelfSignedCert -Subject $subject -CerPath $cerPath -PfxPath $pfxPath -YearsValid $years -PfxPassword $pfxPass

      Write-Host ""
      Write-Host "Created:"
      Write-Host " CER: $($result.CerPath)"
      Write-Host " PFX: $($result.PfxPath)"
      Write-Host " Thumbprint (store): $($result.Thumbprint)"
      Write-Host " Expires: $($result.NotAfter)"
      Pause
    }

    "2" {
      Ensure-ConfigBasics -cfg $cfg
      Write-JsonFile -Path $configPath -Object $cfg

      $cerPath = Resolve-RelPath -Root $root -MaybeRelative $cfg.Certificate.CerPath
      if (-not (Test-Path $cerPath)) { throw "CER not found. Run option 1 first (generate cert)." }

      # Ensure Graph modules exist locally
      Assert-ModulePresent "Microsoft.Graph.Authentication"
      Assert-ModulePresent "Microsoft.Graph.Applications"

      Connect-GraphForAppSetup
      $appInfo = New-EntraAppWithCertAndPerms -AppDisplayName $cfg.AppDisplayName -CerPath $cerPath -CertYearsValid ([int]$cfg.Certificate.YearsValid)

      $cfg.ClientId = $appInfo.ClientId
      Write-JsonFile -Path $configPath -Object $cfg

      Write-Host ""
      Write-Host "Created app:"
      Write-Host " DisplayName: $($appInfo.DisplayName)"
      Write-Host " ClientId:    $($appInfo.ClientId)"
      Write-Host ""
      Write-Host "IMPORTANT:"
      Write-Host " - Some tenants still require an explicit 'Grant admin consent' in Entra portal."
      Write-Host " - If scripts get 'insufficient privileges', grant admin consent for the app's permissions."
      Pause
    }

    "3" {
      $cfg.TenantId = Read-Host "Tenant ID (GUID) (leave blank if using TenantDomain)"
      $cfg.TenantDomain = Read-Host "Tenant domain (e.g. contoso.onmicrosoft.com) (leave blank if using TenantId)"
      $cfg.Defaults.SiteUrl = Read-Host "Default SiteUrl (e.g. https://contoso.sharepoint.com/sites/Finance)"
      $cfg.Defaults.OutputFolder = Read-Host "Output folder (relative like .\output or absolute like C:\Reports)"
      if ([string]::IsNullOrWhiteSpace($cfg.Defaults.OutputFolder)) { $cfg.Defaults.OutputFolder = ".\output" }
      Write-JsonFile -Path $configPath -Object $cfg
      Write-Host "Saved."
      Pause
    }

    "4" {
      $site = $cfg.Defaults.SiteUrl
      if ([string]::IsNullOrWhiteSpace($site)) { $site = Read-Host "SiteUrl" }
      $pfxPass = Prompt-SecurePassword -Prompt "Enter PFX password"

      Connect-PnPFromConfig -SiteUrl $site -cfg $cfg -PfxPassword $pfxPass -Root $root

      $out = Resolve-RelPath -Root $root -MaybeRelative $cfg.Defaults.OutputFolder
      & (Join-Path $root "scripts\Audit-Groups.ps1") -SiteUrl $site -OutputFolder $out
      Pause
    }

    "5" {
      $site = $cfg.Defaults.SiteUrl
      if ([string]::IsNullOrWhiteSpace($site)) { $site = Read-Host "SiteUrl" }
      $pfxPass = Prompt-SecurePassword -Prompt "Enter PFX password"

      Connect-PnPFromConfig -SiteUrl $site -cfg $cfg -PfxPassword $pfxPass -Root $root

      $out = Resolve-RelPath -Root $root -MaybeRelative $cfg.Defaults.OutputFolder
      & (Join-Path $root "scripts\Audit-SitePermissions.ps1") -SiteUrl $site -OutputFolder $out
      Pause
    }

    "6" {
      $site = $cfg.Defaults.SiteUrl
      if ([string]::IsNullOrWhiteSpace($site)) { $site = Read-Host "SiteUrl" }
      $pfxPass = Prompt-SecurePassword -Prompt "Enter PFX password"

      Connect-PnPFromConfig -SiteUrl $site -cfg $cfg -PfxPassword $pfxPass -Root $root

      $out = Resolve-RelPath -Root $root -MaybeRelative $cfg.Defaults.OutputFolder
      & (Join-Path $root "scripts\Audit-ExternalSharing.ps1") -SiteUrl $site -OutputFolder $out
      Pause
    }

    "7" {
      # Runs scripts\Get-SharePointTenantPermissions.ps1 (your example, fixed to accept these parameters)
      Assert-ModulePresent "PnP.PowerShell"
      Assert-ModulePresent "MSAL.PS"

      if ([string]::IsNullOrWhiteSpace($cfg.ClientId)) { throw "ClientId not set. Run option 2 to create the app (or set ClientId in config)." }

      # Determine tenant short name (contoso) if possible
      $tenantShort = $null
      if ($cfg.TenantDomain) { $tenantShort = ($cfg.TenantDomain -split "\.")[0] }
      if ([string]::IsNullOrWhiteSpace($tenantShort)) {
        $tenantShort = Read-Host "Tenant short name (e.g. contoso)"
      }

      $userEmail = Read-Host "User email (UPN)"
      $csv = Read-Host "CSV output path (default .\output\UserPerms.csv)"
      if ([string]::IsNullOrWhiteSpace($csv)) { $csv = (Join-Path $root "output\UserPerms.csv") }

      $pfxPass = Prompt-SecurePassword -Prompt "Enter PFX password"
      $pfxPath = Resolve-RelPath -Root $root -MaybeRelative $cfg.Certificate.PfxPath
      if (-not (Test-Path $pfxPath)) { throw "PFX not found: $pfxPath. Run option 1 first." }

      & (Join-Path $root "scripts\Get-SharePointTenantPermissions.ps1") `
        -TenantName $tenantShort `
        -UserEmail $userEmail `
        -CSVPath $csv `
        -ClientId $cfg.ClientId `
        -CertificatePath $pfxPath `
        -CertificatePassword $pfxPass

      Pause
    }

    "0" { break }
    default { }
  }
}
