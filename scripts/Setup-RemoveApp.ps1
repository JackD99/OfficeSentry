# Setup-RemoveApp.ps1
# Removes the Entra app registration, deletes local cert files.
# Outputs a JSON result line that portal/app.py can parse.

param(
    [Parameter(Mandatory)][string] $ClientId,
    [Parameter(Mandatory)][string] $ModulesPath,
    [string] $PfxPath = "",
    [string] $CerPath = ""
)

$ErrorActionPreference = "Stop"

try {
    $env:PSModulePath = "$ModulesPath;$($env:PSModulePath)"

    Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
    Import-Module Microsoft.Graph.Applications    -ErrorAction Stop

    Connect-MgGraph -Scopes "Application.ReadWrite.All" | Out-Null

    $app = Get-MgApplication -Filter "appId eq '$ClientId'" -ErrorAction SilentlyContinue
    if ($app) {
        Remove-MgApplication -ApplicationId $app.Id
    }

    foreach ($path in @($PfxPath, $CerPath)) {
        if ($path -and (Test-Path $path)) { Remove-Item $path -Force }
    }

    @{ ok = $true; message = "App removed successfully." } | ConvertTo-Json -Compress
}
catch {
    @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
