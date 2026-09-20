# Build a standalone Opmize.exe. Needs: python -m pip install -r requirements.txt pyinstaller
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "== smoke test" -ForegroundColor Cyan
python smoke_test.py
if ($LASTEXITCODE -ne 0) { Write-Host "smoke test failed" -ForegroundColor Red; exit 1 }

$tkdnd = python -c "import tkinterdnd2, os; print(os.path.join(os.path.dirname(tkinterdnd2.__file__), 'tkdnd'))"

Write-Host "== pyinstaller" -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist, "Opmize.spec" -ErrorAction SilentlyContinue
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name Opmize --icon "$root\icon.ico" `
  --add-data "$root\sRGB Color Space Profile.icm;." `
  --add-data "$root\fonts;fonts" `
  --add-data "$root\icon.ico;." `
  --add-data "$tkdnd;tkinterdnd2/tkdnd" `
  --hidden-import pillow_heif `
  --exclude-module cv2 --exclude-module scipy --exclude-module matplotlib --exclude-module pandas `
  --exclude-module IPython --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide6 `
  --exclude-module torch --exclude-module imagecodecs --exclude-module zarr --exclude-module dask `
  --exclude-module numpy.f2py `
  "$root\opmize.pyw"

Write-Host "`n== batch-mode check inside the built exe" -ForegroundColor Cyan
New-Item -ItemType Directory -Force dist\check | Out-Null
& "$root\dist\Opmize.exe" --cli "$root\dist\check" "$root\smoke_out\portrait_4x5_16bit.tif"
python -c "import json;r=json.load(open(r'$root\dist\check\cli_report.json'));assert r['frozen'] and not r['errors'] and r['results'], r;print('   exe processed', r['results'][0]['in_size'], '->', r['results'][0]['out_size'])"

Write-Host "`nBuilt dist\Opmize.exe" -ForegroundColor Green
