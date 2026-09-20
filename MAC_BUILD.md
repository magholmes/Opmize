# Building Opmize on a Mac

This folder is the whole build kit. It lives in OneDrive, so on the Mac it appears at
`~/OneDrive/Desktop/Opmize` once OneDrive has synced (or copy the folder over any other way).
Only these files matter for the Mac build; `Opmize.exe` is the Windows build and can be ignored:

    opmize.pyw                the app (one file, cross-platform)
    fonts/                          Geist + Geist Mono (bundled into the app)
    icon_photo.jpg                  the icon source; make_icns.py turns it into icon.icns
    sRGB Color Space Profile.icm    the colour profile embedded in every export
    build_mac.sh, make_icns.py, smoke_test.py, MAC_BUILD.md

## Steps (in Terminal on the Mac)

1. Have Python 3.11 to 3.13 with Tk. The installer from python.org includes Tk.
   With Homebrew: `brew install python@3.12 python-tk@3.12`.
2. `cd ~/OneDrive/Desktop/Opmize` (or wherever the folder is).
3. `bash build_mac.sh`
   It creates a virtual environment, installs numpy / Pillow / tifffile / tkinterdnd2 / pillow-heif / PyInstaller,
   makes the icon, runs `smoke_test.py`, builds `dist/Opmize.app`, checks the built app in batch mode,
   and zips it as `Opmize (mac arm64).zip` (or `x86_64` on an Intel Mac).
4. Open the app once yourself: right-click `dist/Opmize.app` > Open (it is not code-signed). Drop a photo on it.

## What to ask Claude Code on the Mac

Point it at this folder and say something like:

> Run `bash build_mac.sh`. If anything fails, fix it in `opmize.pyw` (keep it working on Windows too,
> the Windows-only parts are guarded by `IS_WIN`) and rerun until the smoke test and the batch check pass.
> Then launch `dist/Opmize.app`, drop a photo on it, and confirm the export appears next to the photo.

Two things worth verifying by eye on the Mac, because they could not be tested from Windows:

- Fonts: the window should use Geist / Geist Mono (registered per process through CoreText in `register_fonts`).
  If Tk shows Helvetica instead, the CoreText registration needs adjusting.
- Window chrome: on macOS the app keeps the native title bar (the Windows frameless trick is Win32-only).
  If a title-bar-less window is wanted there too, the places to work are `make_frameless` and the `frameless`
  flag in `App.__init__`; Tk on macOS can do it with `::tk::unsupported::MacWindowStyle style . plain none`
  plus a manual drag handler, but it needs testing on a real Mac.

## Sending the Mac build to people

Send the zip. The recipient double-clicks it to get `Opmize.app`, then right-clicks the app and chooses Open
the first time (or on macOS 15 Sequoia: open it once, then System Settings > Privacy & Security > "Open Anyway").
An Apple Silicon build runs on M-series Macs; an Intel Mac needs the x86_64 build (run build_mac.sh on an Intel Mac,
or build on Apple Silicon with `arch -x86_64` using an Intel Python).
