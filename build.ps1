# Builds dist\WorldBaton.exe (one file, Syncthing bundled).
# Needs: pip install pyinstaller sv-ttk
# PyInstaller logs to stderr, so check exit codes instead of $ErrorActionPreference = "Stop".
Set-Location $PSScriptRoot
if (-not (Test-Path vendor\syncthing.exe)) { python scripts\get_syncthing.py; if ($LASTEXITCODE) { exit 1 } }
python -m PyInstaller --noconfirm --onefile --windowed --name WorldBaton --collect-data sv_ttk `
  --icon assets\worldbaton.ico --add-data "assets\worldbaton.png;assets" --add-data "CHANGELOG.md;." `
  --add-data "vendor\syncthing.exe;vendor" `
  --add-data "vendor\SYNCTHING-LICENSE.txt;vendor" `
  worldbaton\__main__.py
if ($LASTEXITCODE) { exit 1 }
