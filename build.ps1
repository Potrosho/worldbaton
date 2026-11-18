# Builds dist\WorldBaton.exe (one file, Syncthing bundled).
# Needs: pip install pyinstaller
# PyInstaller logs to stderr, so check exit codes instead of $ErrorActionPreference = "Stop".
Set-Location $PSScriptRoot
if (-not (Test-Path vendor\syncthing.exe)) { python scripts\get_syncthing.py; if ($LASTEXITCODE) { exit 1 } }
python -m PyInstaller --noconfirm --onefile --windowed --name WorldBaton `
  --add-data "vendor\syncthing.exe;vendor" `
  --add-data "vendor\SYNCTHING-LICENSE.txt;vendor" `
  worldbaton\__main__.py
if ($LASTEXITCODE) { exit 1 }
