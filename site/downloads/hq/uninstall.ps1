[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [switch]$PurgeCredentials
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$MarkerText = "Huangque HQ CLI managed installation"

if (-not $InstallRoot) {
    if (-not $env:LOCALAPPDATA) { throw "HQ CLI uninstall failed: LOCALAPPDATA is not set" }
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Huangque\hq-cli"
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$Marker = Join-Path $InstallRoot ".hq-cli-install"
$BinRoot = Join-Path $InstallRoot "bin"

if (-not (Test-Path -LiteralPath $Marker -PathType Leaf) -or
        ((Get-Content -LiteralPath $Marker -Raw).Trim() -ne $MarkerText)) {
    throw "HQ CLI uninstall failed: managed marker not found; refusing to delete $InstallRoot"
}

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Kept = @($UserPath -split ";" | Where-Object {
    $_ -and $_.Trim() -and $_.TrimEnd("\") -ine $BinRoot.TrimEnd("\")
})
[Environment]::SetEnvironmentVariable("Path", ($Kept -join ";"), "User")

Remove-Item -Recurse -Force -LiteralPath $InstallRoot

if ($PurgeCredentials) {
    $CredentialPaths = @()
    if ($env:APPDATA) {
        $CredentialPaths += Join-Path $env:APPDATA "Huangque\hq-cli\credentials.json"
    }
    if ($env:USERPROFILE) {
        $CredentialPaths += Join-Path $env:USERPROFILE ".config\hq-cli\credentials.json"
    }
    foreach ($CredentialPath in $CredentialPaths) {
        Remove-Item -Force -LiteralPath $CredentialPath -ErrorAction SilentlyContinue
    }
}

Write-Host "HQ CLI uninstalled."
if (-not $PurgeCredentials) {
    Write-Host "Credentials were preserved. Use -PurgeCredentials to remove them."
}
