<#
.SYNOPSIS
    Installs (or updates) Water Column Plotter on Windows.

.DESCRIPTION
    Run it from PowerShell with:

        irm https://raw.githubusercontent.com/valschmidt/WaterColumnPlotter/master/install.ps1 | iex

    It installs uv (https://docs.astral.sh/uv/) if it isn't already installed, then uses uv to install Water
    Column Plotter, with its own copy of Python and the packages it needs, for the current user only: no
    administrator rights are needed. It puts the wcp command on the PATH and adds Water Column Plotter to the
    Start menu. Running it again updates Water Column Plotter to the latest version.

.PARAMETER Source
    What to install: a git URL (the default) or a local folder holding the code (for testing changes).
#>
param(
    [string]$Source = 'git+https://github.com/valschmidt/WaterColumnPlotter'
)

function Install-WaterColumnPlotter {
    $ErrorActionPreference = 'Stop'

    # Pick up PATH changes made since this window was opened (by an earlier install, for instance).
    function Update-SessionPath {
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
    }

    function Invoke-Uv {
        & $uv @args
        if ($LASTEXITCODE -ne 0) { throw "uv $($args -join ' ') failed (exit code $LASTEXITCODE)" }
    }

    Write-Host "Installing Water Column Plotter" -ForegroundColor Cyan

    # 1. uv, which installs Python and Water Column Plotter's packages.
    Update-SessionPath
    $found = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        $uv = $found.Source
        Write-Host "Using uv at $uv"
    } else {
        Write-Host "Installing uv..."
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        Update-SessionPath
        $found = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { $uv = $found.Source }
        elseif (Test-Path "$HOME\.local\bin\uv.exe") { $uv = "$HOME\.local\bin\uv.exe" }
        else { throw "uv was installed, but can't be found. Open a new PowerShell window and run this again." }
    }

    # 2. Water Column Plotter itself. --reinstall fetches the latest code, so running this again updates it.
    # A running copy holds its files open, and the update would fail halfway, leaving it unable to start.
    if (Get-Process -Name wcp, water_column_plotter -ErrorAction SilentlyContinue) {
        throw "Water Column Plotter is running. Close its window (or press Ctrl-C in it), then run this again."
    }
    Write-Host "Installing Water Column Plotter from $Source..."
    Invoke-Uv tool install --python 3.12 --reinstall --force $Source

    # 3. Put the folder uv installs commands into (usually ~\.local\bin) on the PATH, for new windows.
    Invoke-Uv tool update-shell
    $bin = (& $uv tool dir --bin).Trim()
    $wcp = Join-Path $bin 'wcp.exe'
    if (-not (Test-Path $wcp)) { throw "the installation finished, but $wcp isn't there." }

    # 4. A Start menu shortcut, which opens wcp in a console window. Closing the window stops it. Its icon (the pie
    #    display) is installed with the package; ask the installed Python where, so this works for any install.
    $python = Join-Path (& $uv tool dir).Trim() 'watercolumnplotter\Scripts\python.exe'
    $icon = $null
    if (Test-Path $python) {
        # -I: not the current folder's copy of the code, if there's one there.
        $icon = & $python -I -c "import pathlib, WaterColumnPlotter.Web as w; print(pathlib.Path(list(w.__path__)[0]) / 'static' / 'wcp.ico')"
        if ($LASTEXITCODE -ne 0 -or -not $icon -or -not (Test-Path $icon.Trim())) { $icon = $null }
    }
    # Replace any old shortcut, rather than changing it, so Windows Search notices the new one and its icon.
    $link = Join-Path ([Environment]::GetFolderPath('Programs')) 'Water Column Plotter.lnk'
    Remove-Item $link -ErrorAction SilentlyContinue
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
    $shortcut.TargetPath = $wcp
    $shortcut.WorkingDirectory = $HOME
    $shortcut.Description = 'Real-time display of multibeam sonar water column data'
    if ($icon) { $shortcut.IconLocation = "$($icon.Trim()),0" }
    $shortcut.Save()
    # Have Windows reload its cached icons, so an update's new icon shows.
    try { & "$env:WINDIR\System32\ie4uinit.exe" -show } catch {}

    Write-Host ""
    Write-Host "Water Column Plotter is installed." -ForegroundColor Green
    Write-Host "Start it from the Start menu (Water Column Plotter), or type wcp in a new PowerShell window."
}

try {
    Install-WaterColumnPlotter
} catch {
    # Not exit: when run with "irm ... | iex", exit would close the user's PowerShell window.
    Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
}
