# Packaging the Windows app

`build_windows.py` turns the repository into a folder that runs without Python: `G2 Bridge.exe`, the Python runtime and PyUSB inside `_internal`, FFmpeg in `ffmpeg`, and the license texts. It then zips the folder for a release.

## Build locally

```powershell
python -m pip install -r requirements.txt pyinstaller==6.22.3
python packaging/build_windows.py
```

The script downloads Gyan Doshi's FFmpeg 9.0.2 essentials build from its GitHub release, checks its SHA-256, and caches it under `build/downloads`. PyInstaller writes to `build/pyinstaller` and `dist/G2 Bridge`. The zip lands in `dist/`. Pass `--no-zip` to skip the archive.

The window's icon is `windows/g2bridge.ico`, drawn with Pillow; `windows/g2bridge-256.png` is the same picture for documentation.

## What the app does differently from the script

- `goggles_bridge/app_config.py` finds FFmpeg next to the exe first, then `GOGGLES_FFMPEG`, then `PATH` and the winget links folder.
- Settings and logs go to `%LOCALAPPDATA%\G2 Bridge` when packaged, and to `work/` from a checkout.
- The DJI USB driver is never bundled. It is DJI's, and the bridge loads it from `System32` where DJI Assistant 2 installs it. The window shows a download button when it is missing or is not the tested 5.0.0.7 build.

## Release

Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`, which runs the tests, checks that the tag matches `goggles_bridge.__version__`, builds the app on a Windows runner, starts the exe once to make sure it opens, and attaches the zip and its SHA-256 to a GitHub release. To cut a release:

1. Set `__version__` in `goggles_bridge/__init__.py`.
2. Commit, then `git tag vX.Y.Z` and `git push origin vX.Y.Z`.

## Licenses

G2 Bridge is GPL-3.0-only. The FFmpeg build is GPL; the zip includes its `LICENSE` and build `README.txt`, and the release notes link to its source. `THIRD_PARTY.txt` in the zip lists everything included. PyInstaller's bootloader license permits bundling programs under any license.

## macOS

Not packaged yet. The script path works on macOS; see the main README. A signed and notarized `.app` would need an Apple developer account, and the ScreenCaptureKit helper would have to be prebuilt.
