<#
.SYNOPSIS
    Install the 360-cli-tools scripts for use from PowerShell.

.DESCRIPTION
    Copies xfade_concat.py, titles_in_360.py and pieces.py (the engine both
    need) to a folder on this computer, and adds two commands to your
    PowerShell profile:

        xfade      ->  python <folder>\xfade_concat.py
        titles360  ->  python <folder>\titles_in_360.py

    Run it again after every update to the repository. Other files in the
    folder are left alone, and the profile block is replaced, not duplicated.

.PARAMETER Dest
    Where the scripts go. Default: $HOME\bin

.PARAMETER Pillow
    Also install or upgrade Pillow (needed by titles_in_360.py) with pip.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\home\peter\dev\360-cli-tools\install.ps1 -Pillow
#>
param(
    [string]$Dest = (Join-Path $HOME "bin"),
    [switch]$Pillow
)
$ErrorActionPreference = "Stop"

$files = "xfade_concat.py", "titles_in_360.py", "pieces.py"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
foreach ($f in $files) {
    $src = Join-Path $PSScriptRoot $f
    if (-not (Test-Path $src)) { throw "missing $src - run install.ps1 from the repository folder" }
    Copy-Item $src (Join-Path $Dest $f) -Force
    Write-Host "copied  $f -> $Dest"
}

# PowerShell commands, in a marked block that is replaced on every install
$begin = "# >>> 360-cli-tools >>>"
$end = "# <<< 360-cli-tools <<<"
$xf = Join-Path $Dest "xfade_concat.py"
$ti = Join-Path $Dest "titles_in_360.py"
$block = @"
$begin
function xfade { python "$xf" @args }
function titles360 { python "$ti" @args }
$end
"@
if (-not (Test-Path $PROFILE)) { New-Item -ItemType File -Force -Path $PROFILE | Out-Null }
$profileText = Get-Content $PROFILE -Raw
if ($null -eq $profileText) { $profileText = "" }
$pattern = "(?s)" + [regex]::Escape($begin) + ".*?" + [regex]::Escape($end)
if ($profileText -match $pattern) {
    $profileText = [regex]::Replace($profileText, $pattern, $block.TrimEnd())
} elseif ($profileText.Trim()) {
    $profileText = $profileText.TrimEnd() + "`r`n`r`n" + $block
} else {
    $profileText = $block
}
Set-Content -Path $PROFILE -Value $profileText.TrimEnd() -Encoding UTF8
Write-Host "updated $PROFILE (xfade, titles360)"

# Python and Pillow
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Warning "python not found on PATH - install Python 3.9+ for Windows"
} else {
    if ($Pillow) {
        python -m pip install --upgrade pillow
    }
    python -c "import PIL" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Pillow is missing (titles_in_360.py needs it): python -m pip install pillow"
    }
}
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ff) { Write-Warning "ffmpeg not found on PATH (e.g. C:\ffmpeg\bin)" }

Write-Host ""
Write-Host "Done. Open a new PowerShell window (or run: . `$PROFILE), then: xfade --help / titles360 --help"
