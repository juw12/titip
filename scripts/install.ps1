<#
.SYNOPSIS
    Put Titip on the Desktop and start it quietly when you sign in to Windows.

.DESCRIPTION
    Creates two shortcuts that run Titip.pyw with pythonw (no console window):
      Desktop\Titip.lnk          opens the page with the QR code
      Startup\Titip.lnk          starts Titip in the background at sign-in
    Safe to run again. Needs no administrator rights.

        scripts\install.ps1            install
        scripts\install.ps1 -Remove    take both shortcuts away
#>
param([switch]$Remove)

$ErrorActionPreference = 'Stop'

$root     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$launcher = Join-Path $root 'Titip.pyw'
$icon     = Join-Path $root 'titip.ico'
$desktop  = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Titip.lnk'
$startup  = Join-Path ([Environment]::GetFolderPath('Startup')) 'Titip.lnk'

if ($Remove) {
    foreach ($link in @($desktop, $startup)) {
        if (Test-Path -LiteralPath $link) {
            Remove-Item -LiteralPath $link
            Write-Host "Removed $link"
        }
    }
    return
}

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    throw 'pythonw.exe was not found. Install Python from python.org and tick "Add python.exe to PATH".'
}

$shell = New-Object -ComObject WScript.Shell
$links = @(
    @{ Path = $desktop; Arguments = "`"$launcher`"";         Description = 'Titip: your Outlook mail on your phone' },
    @{ Path = $startup; Arguments = "`"$launcher`" --quiet"; Description = 'Titip (starts in the background)' }
)
foreach ($link in $links) {
    $shortcut = $shell.CreateShortcut($link.Path)
    $shortcut.TargetPath       = $pythonw
    $shortcut.Arguments        = $link.Arguments
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation     = $icon
    $shortcut.Description      = $link.Description
    $shortcut.Save()
    Write-Host "Created $($link.Path)"
}
Write-Host 'Done. Double-click Titip on the Desktop, then scan the QR code with your phone.'
