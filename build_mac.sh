#!/bin/bash
# Build "Opmize.app" on macOS. Run from a Terminal on the Mac:  bash build_mac.sh
# Needs Python 3.11 - 3.13 with Tk (the python.org installer includes it; with Homebrew: brew install python-tk@3.12).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo "== using $($PY --version) at $(command -v $PY)"
$PY -c "import tkinter; print('   Tk', tkinter.TkVersion)" || { echo "!! this Python has no Tk. Install python.org Python or: brew install python-tk@3.12"; exit 1; }

echo "== virtual environment"
[ -d .venv-mac ] || $PY -m venv .venv-mac
# shellcheck disable=SC1091
source .venv-mac/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install --upgrade numpy pillow tifffile tkinterdnd2 pillow-heif pyinstaller

echo "== icon"
python make_icns.py icon_photo.jpg icon.icns

echo "== smoke test (pipeline + a hidden window with a simulated drop)"
python smoke_test.py

echo "== pyinstaller"
ARCH="$(uname -m)"                       # arm64 (Apple Silicon) or x86_64 (Intel); the .app runs natively on the arch it was built on
TKDND="$(python -c 'import tkinterdnd2, os; print(os.path.join(os.path.dirname(tkinterdnd2.__file__), "tkdnd"))')"
ADD=()
for d in osx-arm64 osx-arm64-tcl9 osx-x64 osx-x64-tcl9; do
  [ -d "$TKDND/$d" ] && ADD+=(--add-data "$TKDND/$d:tkinterdnd2/tkdnd/$d")
done
rm -rf build dist "Opmize.spec"
pyinstaller --noconfirm --windowed --name "Opmize" --icon icon.icns \
  --osx-bundle-identifier com.magnusholmes.opmize \
  --add-data "sRGB Color Space Profile.icm:." --add-data "fonts:fonts" --add-data "icon_photo.jpg:." \
  ${ADD[@]+"${ADD[@]}"} \
  --hidden-import pillow_heif \
  --exclude-module cv2 --exclude-module scipy --exclude-module skimage --exclude-module matplotlib --exclude-module pandas \
  --exclude-module IPython --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide6 --exclude-module torch \
  --exclude-module imagecodecs --exclude-module zarr --exclude-module fsspec --exclude-module dask --exclude-module xarray \
  --exclude-module numpy.f2py --exclude-module PIL.ImageTk \
  opmize.pyw

APP="dist/Opmize.app"
echo "== batch-mode check inside the built app"
mkdir -p dist/check
"$APP/Contents/MacOS/Opmize" --cli dist/check smoke_out/portrait_4x5_16bit.tif
python - <<'EOF'
import json; r = json.load(open("dist/check/cli_report.json"))
assert r["frozen"] and not r["errors"] and r["results"], r
print("   app processed", r["results"][0]["in_size"], "->", r["results"][0]["out_size"], "| fonts:", r.get("fonts"), "| dnd:", r.get("tkdnd_version"))
EOF

ZIP="Opmize (mac $ARCH).zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
echo "== done: $ZIP  ($(du -h "$ZIP" | cut -f1))"
echo "   open it once yourself: right-click the .app > Open (unsigned app), then drop a photo on it."
