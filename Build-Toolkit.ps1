$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  ========================================"
Write-Host "    Office Sentry — Module Download"
Write-Host "  ========================================"
Write-Host ""

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
  "ExchangeOnlineManagement",
  "ImportExcel"
)

$total   = $mods.Count
$current = 0
$failed  = @()

foreach ($m in $mods) {
  $current++
  $pct = [math]::Round(($current / $total) * 100)
  Write-Progress -Activity "Downloading PowerShell modules" `
    -Status "$current of $total — $m" `
    -PercentComplete $pct

  # Check if already present
  $modDir = Join-Path $dest $m
  if ((Test-Path $modDir) -and (Get-ChildItem $modDir -Recurse -Filter "*.psm1" -ErrorAction SilentlyContinue)) {
    Write-Host "  [OK] $m (already downloaded)" -ForegroundColor Green
    continue
  }

  try {
    Write-Host "  [..] Downloading $m..." -NoNewline
    Save-Module $m -Path $dest -Force -ErrorAction Stop
    Write-Host "`r  [OK] $m                    " -ForegroundColor Green
  }
  catch {
    Write-Host "`r  [FAIL] $m — $($_.Exception.Message)" -ForegroundColor Red
    $failed += $m
  }
}

Write-Progress -Activity "Downloading PowerShell modules" -Completed

Write-Host ""
Write-Host "  ----------------------------------------"
Write-Host "  Downloaded: $($total - $failed.Count) / $total modules"
Write-Host "  Location:   $dest"

if ($failed.Count -gt 0) {
  Write-Host ""
  Write-Host "  Failed modules:" -ForegroundColor Yellow
  foreach ($f in $failed) {
    Write-Host "    - $f" -ForegroundColor Yellow
  }
  Write-Host ""
  Write-Host "  Retry with: pwsh -File Build-Toolkit.ps1" -ForegroundColor Cyan
} else {
  Write-Host "  Status:     All modules ready" -ForegroundColor Green
}
Write-Host ""
