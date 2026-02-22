param(
    [string]$AppPath = "src/app1",
    [string]$ModuleName = "app1",
    [string]$Output = "build/windows",
    [string]$StageBase = "build/flet_stage",
    [switch]$KeepStage,
    [switch]$SkipDevModeCheck,
    [switch]$VerboseBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$stageRoot = Join-Path $RepoRoot "$StageBase`_$timestamp"
$archivePath = Join-Path $stageRoot "repo.tar"

function Run-External {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [string]$WorkingDirectory = $RepoRoot
    )

    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed: $FilePath $($Arguments -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

function Patch-ScreenBrightnessWindowsPackage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PackageDir
    )

    $changed = $false

    $cmakePath = Join-Path $PackageDir "windows/CMakeLists.txt"
    if (Test-Path $cmakePath) {
        $content = Get-Content $cmakePath -Raw -Encoding UTF8
        $updated = $content.Replace(
            "target_include_directories(`${PLUGIN_NAME} INTERFACE",
            "target_include_directories(`${PLUGIN_NAME} PUBLIC"
        )
        if ($updated -ne $content) {
            Set-Content $cmakePath -Value $updated -Encoding UTF8
            $changed = $true
        }
    }

    $pluginHeaderPath = Join-Path $PackageDir "windows/include/screen_brightness_windows/screen_brightness_windows_plugin.h"
    if (Test-Path $pluginHeaderPath) {
        $content = Get-Content $pluginHeaderPath -Raw -Encoding UTF8
        $updated = $content.Replace(
            "#include ""../include/screen_brightness_windows/screen_brightness_changed_stream_handler.h""",
            "#include ""screen_brightness_changed_stream_handler.h"""
        )
        if ($updated -ne $content) {
            Set-Content $pluginHeaderPath -Value $updated -Encoding UTF8
            $changed = $true
        }
    }

    $pluginCppPath = Join-Path $PackageDir "windows/src/screen_brightness_windows_plugin.cpp"
    if (Test-Path $pluginCppPath) {
        $content = Get-Content $pluginCppPath -Raw -Encoding UTF8
        $updated = $content.Replace(
            "#include ""../include/screen_brightness_windows/screen_brightness_windows_plugin.h""",
            "#include ""screen_brightness_windows/screen_brightness_windows_plugin.h"""
        )
        if ($updated -ne $content) {
            Set-Content $pluginCppPath -Value $updated -Encoding UTF8
            $changed = $true
        }
    }

    $streamCppPath = Join-Path $PackageDir "windows/src/screen_brightness_changed_stream_handler.cpp"
    if (Test-Path $streamCppPath) {
        $content = Get-Content $streamCppPath -Raw -Encoding UTF8
        $updated = $content.Replace(
            "#include ""../include/screen_brightness_windows/screen_brightness_changed_stream_handler.h""",
            "#include ""screen_brightness_windows/screen_brightness_changed_stream_handler.h"""
        )
        if ($updated -ne $content) {
            Set-Content $streamCppPath -Value $updated -Encoding UTF8
            $changed = $true
        }
    }

    return $changed
}

function Apply-ScreenBrightnessWindowsHotfix {
    $hostedRoot = Join-Path $env:LOCALAPPDATA "Pub\\Cache\\hosted"
    if (-not (Test-Path $hostedRoot)) {
        return $false
    }

    $packageDirs = @()
    foreach ($hostDir in Get-ChildItem $hostedRoot -Directory -ErrorAction SilentlyContinue) {
        $packageDirs += Get-ChildItem $hostDir.FullName -Directory -Filter "screen_brightness_windows-*" -ErrorAction SilentlyContinue
    }

    if ($packageDirs.Count -eq 0) {
        return $false
    }

    $patchedAny = $false
    foreach ($pkg in $packageDirs) {
        if (Patch-ScreenBrightnessWindowsPackage -PackageDir $pkg.FullName) {
            Write-Host "Applied hotfix to $($pkg.FullName)"
            $patchedAny = $true
        }
    }

    return $patchedAny
}

function Test-DeveloperModeEnabled {
    try {
        $item = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" -ErrorAction Stop
        return $item.AllowDevelopmentWithoutDevLicense -eq 1
    }
    catch {
        return $false
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required but was not found in PATH."
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required but was not found in PATH."
}
if (-not $SkipDevModeCheck -and -not (Test-DeveloperModeEnabled)) {
    throw "Windows Developer Mode is required for Flutter plugin symlinks. Enable it in Settings > For developers, or run: start ms-settings:developers"
}

New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

Write-Host "Creating source archive from HEAD..."
Run-External -FilePath "git" -Arguments @("archive", "--format=tar", "-o", $archivePath, "HEAD")
Run-External -FilePath "tar" -Arguments @("-xf", $archivePath, "-C", $stageRoot)

$stageAppPath = Join-Path $stageRoot $AppPath
if (-not (Test-Path $stageAppPath)) {
    throw "App path not found in staged source: $stageAppPath"
}

$outputPath = if ([System.IO.Path]::IsPathRooted($Output)) {
    $Output
}
else {
    (Join-Path $RepoRoot $Output)
}

if (-not (Test-Path $outputPath)) {
    New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$uvArgs = @(
    "run",
    "flet",
    "build",
    "--no-rich-output",
    "windows",
    $stageAppPath,
    "--module-name",
    $ModuleName,
    "--output",
    $outputPath,
    "--exclude",
    ".venv",
    ".git",
    ".idea",
    "logs",
    "test"
)

if ($VerboseBuild) {
    $uvArgs = @("run", "flet", "build", "-v", "--no-rich-output", "windows", $stageAppPath, "--module-name", $ModuleName, "--output", $outputPath, "--exclude", ".venv", ".git", ".idea", "logs", "test")
}

Write-Host "Running Flet build from staged normal files..."
Apply-ScreenBrightnessWindowsHotfix | Out-Null

$buildSucceeded = $false
$buildError = $null
try {
    Run-External -FilePath "uv" -Arguments $uvArgs -WorkingDirectory $RepoRoot
    $buildSucceeded = $true
}
catch {
    $buildError = $_
}

if (-not $buildSucceeded) {
    $patchedAfterFailure = Apply-ScreenBrightnessWindowsHotfix
    if ($patchedAfterFailure) {
        Write-Host "Detected known screen_brightness_windows issue, retrying build once..."
        try {
            Run-External -FilePath "uv" -Arguments $uvArgs -WorkingDirectory $RepoRoot
            $buildSucceeded = $true
        }
        catch {
            $buildError = $_
        }
    }
}

if (-not $buildSucceeded) {
    throw $buildError
}

if (-not $KeepStage) {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force
}

Write-Host "Build completed. Output: $outputPath"
if ($KeepStage) {
    Write-Host "Staging directory kept: $stageRoot"
}
