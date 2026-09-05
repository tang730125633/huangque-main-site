[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [string]$WheelPath = "",
    [switch]$NoPathUpdate
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

$Version = "0.15.4"
$WheelName = "huangque_hq_cli-$Version-py3-none-any.whl"
$WheelSize = 66911
$WheelSha256 = "f9893cc7611e7adc650ac470880a784730415b70bfdecd35bc598102e5d0ff70"
$WheelUrl = "https://huangquechuanmei.com/downloads/hq/v0.15.4/$WheelName"
$MarkerText = "Huangque HQ CLI managed installation"

function Fail([string]$Message) {
    throw "HQ CLI install failed: $Message"
}

function Find-Python {
    $Candidates = @()
    $Py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($Py) {
        foreach ($Selector in @("-3.13", "-3.12", "-3.11", "-3.10", "-3")) {
            $Candidates += ,@($Py.Source, $Selector)
        }
    }
    foreach ($Name in @("python.exe", "python3.exe")) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command) {
            $Candidates += ,@($Command.Source)
        }
    }
    foreach ($Candidate in $Candidates) {
        $Exe = $Candidate[0]
        $Prefix = @($Candidate | Select-Object -Skip 1)
        $CandidateExitCode = 1
        try {
            & $Exe @Prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            $CandidateExitCode = $LASTEXITCODE
        }
        catch {
            $CandidateExitCode = 1
        }
        if ($CandidateExitCode -eq 0) {
            return @{ Exe = $Exe; Prefix = $Prefix }
        }
    }
    Fail "Python 3.10 or newer is required (python.org with the py launcher is recommended)"
}

function Invoke-Python($Python, [string[]]$Arguments) {
    & $Python.Exe @($Python.Prefix + $Arguments)
    if ($LASTEXITCODE -ne 0) {
        Fail "Python command failed with exit code $LASTEXITCODE"
    }
}

function Test-ManagedLauncher([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $true }
    $FirstLine = Get-Content -LiteralPath $Path -TotalCount 1 -ErrorAction Stop
    return $FirstLine -eq ":: Huangque HQ CLI managed launcher"
}

if (-not $InstallRoot) {
    if (-not $env:LOCALAPPDATA) { Fail "LOCALAPPDATA is not set" }
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Huangque\hq-cli"
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$VersionsRoot = Join-Path $InstallRoot "versions"
$BinRoot = Join-Path $InstallRoot "bin"
$TargetRoot = Join-Path $VersionsRoot $Version
$TargetPython = Join-Path $TargetRoot "venv\Scripts\python.exe"
$TargetHq = Join-Path $TargetRoot "venv\Scripts\hq.exe"
$Launcher = Join-Path $BinRoot "hq.cmd"
$Marker = Join-Path $InstallRoot ".hq-cli-install"

New-Item -ItemType Directory -Force -Path $InstallRoot, $VersionsRoot, $BinRoot | Out-Null
if ((Test-Path -LiteralPath $Marker) -and
        ((Get-Content -LiteralPath $Marker -Raw).Trim() -ne $MarkerText)) {
    Fail "managed marker mismatch; refusing to overwrite $InstallRoot"
}
[IO.File]::WriteAllText($Marker, $MarkerText + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
if (-not (Test-ManagedLauncher $Launcher)) {
    Fail "$Launcher belongs to another program; refusing to overwrite it"
}

$Python = Find-Python
$TempRoot = Join-Path $InstallRoot (".download-" + [Guid]::NewGuid().ToString("N"))
$StageRoot = Join-Path $VersionsRoot (".stage-$Version-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempRoot | Out-Null

try {
    $LocalWheel = Join-Path $TempRoot $WheelName
    if ($WheelPath) {
        $ResolvedWheel = (Resolve-Path -LiteralPath $WheelPath -ErrorAction Stop).Path
        Copy-Item -LiteralPath $ResolvedWheel -Destination $LocalWheel
    }
    else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $WheelUrl -OutFile $LocalWheel
    }

    $ActualHash = (Get-FileHash -LiteralPath $LocalWheel -Algorithm SHA256).Hash.ToLowerInvariant()
    if ((Get-Item -LiteralPath $LocalWheel).Length -ne $WheelSize) {
        Fail "wheel size verification failed"
    }
    if ($ActualHash -ne $WheelSha256) {
        Fail "wheel SHA-256 verification failed"
    }

    $AlreadyInstalled = $false
    if (Test-Path -LiteralPath $TargetHq -PathType Leaf) {
        & $TargetHq version --json *> $null
        $AlreadyInstalled = $LASTEXITCODE -eq 0
    }
    elseif (Test-Path -LiteralPath $TargetRoot) {
        Fail "$TargetRoot exists but is not a complete installation"
    }

    if (-not $AlreadyInstalled) {
        Invoke-Python $Python @("-m", "venv", (Join-Path $StageRoot "venv"))
        $StagePython = Join-Path $StageRoot "venv\Scripts\python.exe"
        & $StagePython -m pip install --disable-pip-version-check --no-index --no-deps $LocalWheel
        if ($LASTEXITCODE -ne 0) { Fail "wheel installation failed" }
        $StageHq = Join-Path $StageRoot "venv\Scripts\hq.exe"
        & $StageHq version --json *> $null
        if ($LASTEXITCODE -ne 0) { Fail "staged HQ CLI did not start" }

        Move-Item -LiteralPath $StageRoot -Destination $TargetRoot
        & $TargetPython -m pip install --disable-pip-version-check --no-index --no-deps --force-reinstall $LocalWheel *> $null
        if ($LASTEXITCODE -ne 0) { Fail "failed to refresh the launcher after moving the environment" }
        & $TargetHq version --json *> $null
        if ($LASTEXITCODE -ne 0) { Fail "final HQ CLI startup check failed" }
    }

    $LauncherBody = @"
:: Huangque HQ CLI managed launcher
@echo off
set "PYTHONUTF8=1"
"%~dp0..\versions\$Version\venv\Scripts\hq.exe" %*
"@
    $LauncherTemp = Join-Path $BinRoot (".hq.cmd." + [Guid]::NewGuid().ToString("N") + ".tmp")
    [IO.File]::WriteAllText($LauncherTemp, $LauncherBody, [Text.ASCIIEncoding]::new())
    Move-Item -Force -LiteralPath $LauncherTemp -Destination $Launcher

    if (-not $NoPathUpdate) {
        $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $Parts = @($UserPath -split ";" | Where-Object { $_ -and $_.Trim() })
        if (-not ($Parts | Where-Object { $_.TrimEnd("\") -ieq $BinRoot.TrimEnd("\") })) {
            $NewPath = (@($Parts) + $BinRoot) -join ";"
            [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        }
        if (-not (($env:Path -split ";") | Where-Object { $_.TrimEnd("\") -ieq $BinRoot.TrimEnd("\") })) {
            $env:Path = $BinRoot + ";" + $env:Path
        }
    }

    Write-Host "HQ CLI $Version installed: $Launcher"
    Write-Host "Open a new PowerShell window, then run: hq login --json"
}
finally {
    Remove-Item -Recurse -Force -LiteralPath $TempRoot -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -LiteralPath $StageRoot -ErrorAction SilentlyContinue
}
