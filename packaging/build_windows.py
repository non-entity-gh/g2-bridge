"""Build the Windows app: a folder with G2 Bridge.exe and FFmpeg, zipped for a release.

    python packaging/build_windows.py

Steps: download the pinned FFmpeg build (cached under build/), run PyInstaller on
scripts/g2_bridge_app.py, put ffmpeg.exe and its license next to the exe, add the
quick-start text, and zip the folder to dist/. The result needs no Python on the
user's PC. The DJI USB driver still comes from DJI Assistant 2.

FFmpeg is Gyan Doshi's "essentials" build, which has gfxcapture, ddagrab and
h264_nvenc. It is GPL, like this project; the zip keeps its license and README,
and the release notes link to its source.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from goggles_bridge import __version__

FFMPEG_VERSION = '9.0.2'
FFMPEG_URL = f'https://github.com/GyanD/codexffmpeg/releases/download/{FFMPEG_VERSION}/ffmpeg-{FFMPEG_VERSION}-essentials_build.zip'
FFMPEG_SHA256 = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
APP_NAME = 'G2 Bridge'
BUILD = ROOT / 'build'
DIST = ROOT / 'dist'

QUICK_START = f'''G2 Bridge {__version__} for Windows
Stream your desktop to DJI Goggles 2 or Goggles Integra over USB.

Before the first run
  1. Install DJI Assistant 2 (Consumer Drones Series) from dji.com. It installs the
     USB driver the goggles need. The app shows a download button if it is missing.
  2. Use an NVIDIA GPU with up-to-date drivers. The encoder is NVENC.
  3. Update the goggles to firmware 01.14 or later with the DJI Fly app.

Each time
  1. Power on the goggles, connect a USB data cable, open More > DJI Virtual Flight.
  2. Start "G2 Bridge.exe", choose a picture setting, press Start streaming.
  3. Press Stop before unplugging. Logs: Open logs button.

Keep the ffmpeg folder next to the exe; the app needs it.

G2 Bridge is free software under the GNU GPL v3 (see LICENSE.txt). It includes
FFmpeg {FFMPEG_VERSION} (ffmpeg/LICENSE); see THIRD_PARTY.txt for sources.
Project page and source: https://github.com/Ammaar-Alam/g2-bridge
'''

THIRD_PARTY = f'''Third-party software in this package

FFmpeg {FFMPEG_VERSION}, GPL v3 build by Gyan Doshi
  Binary: {FFMPEG_URL}
  Source: https://ffmpeg.org/download.html and https://github.com/GyanD/codexffmpeg
  License: ffmpeg/LICENSE
  Build details: ffmpeg/README.txt

Python {sys.version.split()[0]} runtime, PSF License
  https://www.python.org/

PyUSB 1.3.1, BSD 3-Clause
  https://github.com/pyusb/pyusb

PyInstaller bootloader, GPL with an exception that lets it be used in programs under any license
  https://pyinstaller.org/

The DJI USB driver is not included. It comes with DJI Assistant 2 from DJI.
'''


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_ffmpeg():
    """Download and verify the FFmpeg zip once, then extract ffmpeg.exe and its license."""
    cache = BUILD / 'downloads' / Path(FFMPEG_URL).name
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file() or sha256(cache) != FFMPEG_SHA256:
        print(f'Downloading {FFMPEG_URL}')
        with urllib.request.urlopen(FFMPEG_URL) as response, cache.open('wb') as out:
            shutil.copyfileobj(response, out)
        actual = sha256(cache)
        if actual != FFMPEG_SHA256:
            cache.unlink()
            raise SystemExit(f'FFmpeg download hash {actual} does not match the pinned {FFMPEG_SHA256}')
    target = BUILD / 'ffmpeg'
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    with zipfile.ZipFile(cache) as archive:
        for member in archive.namelist():
            name = Path(member).name
            if member.endswith('/bin/ffmpeg.exe') or (name in ('LICENSE', 'README.txt') and member.count('/') == 1):
                (target / name).write_bytes(archive.read(member))
    for name in ('ffmpeg.exe', 'LICENSE', 'README.txt'):
        if not (target / name).is_file():
            raise SystemExit(f'{name} missing from the FFmpeg archive')
    return target


def run_pyinstaller(icon):
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
               '--name', APP_NAME, '--distpath', str(DIST), '--workpath', str(BUILD / 'pyinstaller'),
               '--specpath', str(BUILD), '--hidden-import', 'usb.backend.libusb0',
               '--hidden-import', 'usb.backend.libusb1', '--exclude-module', 'tkinter.test',
               '--exclude-module', 'unittest', str(ROOT / 'scripts' / 'g2_bridge_app.py')]
    if icon:
        # The exe's icon, and a copy inside the bundle for the window's title bar
        command += ['--icon', str(icon), '--add-data', f'{icon}{os.pathsep}packaging/windows']
    print(' '.join(command))
    subprocess.run(command, check=True)
    return DIST / APP_NAME


def assemble(app_folder, ffmpeg_folder):
    shutil.copytree(ffmpeg_folder, app_folder / 'ffmpeg', dirs_exist_ok=True)
    shutil.copy(ROOT / 'LICENSE', app_folder / 'LICENSE.txt')
    (app_folder / 'README.txt').write_text(QUICK_START, encoding='utf-8')
    (app_folder / 'THIRD_PARTY.txt').write_text(THIRD_PARTY, encoding='utf-8')


def make_zip(app_folder):
    archive = DIST / f'G2-Bridge-{__version__}-windows.zip'
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as out:
        for path in sorted(app_folder.rglob('*')):
            if path.is_file():
                out.write(path, Path(app_folder.name) / path.relative_to(app_folder))
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--icon', type=Path, default=ROOT / 'packaging' / 'windows' / 'g2bridge.ico')
    parser.add_argument('--no-zip', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This build runs on Windows')
    ffmpeg_folder = fetch_ffmpeg()
    app_folder = run_pyinstaller(args.icon if args.icon.is_file() else None)
    assemble(app_folder, ffmpeg_folder)
    print(f'Built {app_folder}')
    if not args.no_zip:
        archive = make_zip(app_folder)
        print(f'Zipped {archive} ({archive.stat().st_size / 1e6:.0f} MB, SHA-256 {sha256(archive)})')


if __name__ == '__main__':
    main()
