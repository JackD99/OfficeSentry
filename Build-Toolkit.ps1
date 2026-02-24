$ErrorActionPreference = "Stop"
$dest = Join-Path $PSScriptRoot "modules"
New-Item -ItemType Directory -Path $dest -Force | Out-Null

$mods = @(
  "PnP.PowerShell",
  "MSAL.PS",
  "Microsoft.Graph.Authentication",
  "Microsoft.Graph.Applications",
  "Microsoft.Graph.Users",
  "Microsoft.Graph.Groups",
  "Microsoft.Graph.DirectoryObjects",
  "Microsoft.Online.SharePoint.PowerShell",
  "ExchangeOnlineManagement",
  "ImportExcel"
)

foreach ($m in $mods) {
  Write-Host "Saving $m..."
  Save-Module $m -Path $dest -Force
}

Write-Host "Done. Modules saved to: $dest"
