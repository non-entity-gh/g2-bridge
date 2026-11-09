"""Stream the Windows or macOS desktop to stock DJI Goggles 2 in Virtual Flight mode."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from goggles_bridge import mac_capture
from goggles_bridge.app_config import find_ffmpeg
from goggles_bridge.engine import BITRATES_MBPS, PRESETS, VBV_FRAMES
from goggles_bridge.usb_transport import DEFAULT_DLL, USB_WRITE_SIZES

MAC = sys.platform == 'darwin'
MAC_CAPTURE = 'screencapturekit' if MAC and mac_capture.supported() else 'avfoundation'
FFMPEG_INSTALL = 'brew install ffmpeg' if MAC else 'winget install Gyan.FFmpeg'

# winget adds this folder to PATH but terminals opened before the install miss it
WINGET_LINKS = Path(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Links')
SETUP_HINTS = {
    'dll': 'DJI USB driver not found. Install DJI Assistant 2 (Consumer Drones Series), or pass --dll',
    'ffmpeg': f'FFmpeg not found. Run "{FFMPEG_INSTALL}", or pass --ffmpeg',
}


def ask(question):
    try:
        return input(f'{question} [y/N] ').strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def install_mac_dependencies(parser, args):
    """Offer to install the developer tools, FFmpeg, and libusb when the chosen capture needs them."""
    if args.capture == 'screencapturekit' and subprocess.run(['xcode-select', '-p'], capture_output=True).returncode:
        if not ask("Apple's command-line developer tools are needed to build the capture helper. Install them?"):
            parser.error('Run "xcode-select --install", then try again')
        subprocess.run(['xcode-select', '--install'])
        parser.error('Finish installing the command-line developer tools, then run this again')
    import usb.backend.libusb1
    missing = [name for name, absent in (('ffmpeg', args.capture == 'avfoundation' and args.ffmpeg is None),
                                         ('libusb', usb.backend.libusb1.get_backend() is None)) if absent]
    if not missing:
        return
    names, command = ' and '.join(missing), f'brew install {" ".join(missing)}'
    brew = shutil.which('brew') or next((p for p in ('/opt/homebrew/bin/brew', '/usr/local/bin/brew')
                                         if Path(p).is_file()), None)
    if brew is None:
        parser.error(f'{names} not found. Install Homebrew from https://brew.sh, then run "{command}"')
    if not ask(f'{names} not found. Install with "{command}"?'):
        parser.error(f'Run "{command}", then try again')
    if subprocess.run([brew, 'install', *missing]).returncode != 0:
        parser.error(f'"{command}" failed')
    if args.ffmpeg is None:
        found = shutil.which('ffmpeg') or shutil.which('ffmpeg', path=str(Path(brew).parent))
        args.ffmpeg = Path(found) if found else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dll', type=Path, default=None if MAC else os.environ.get('DJI_USB_DLL') or DEFAULT_DLL,
                        help='Windows only: DJI x64 libusb0_device.dll (default: DJI_USB_DLL or the installed driver copy)')
    parser.add_argument('--ffmpeg', type=Path, default=find_ffmpeg(),
                        help='FFmpeg executable with NVENC or VideoToolbox and the chosen capture '
                             '(default: the app folder, GOGGLES_FFMPEG, or PATH)')
    parser.add_argument('--python-deps', type=Path, help='Optional directory containing PyUSB 1.3.1')
    parser.add_argument('--fps', type=int, choices=(60, 100), default=100,
                        help='Target encoder frame rate (default: 100)')
    parser.add_argument('--bitrate-mbps', type=int, choices=BITRATES_MBPS, default=20)
    parser.add_argument('--vbv-frames', type=int, choices=VBV_FRAMES, default=2,
                        help='Rate-control buffer in frames; larger values give keyframes more bytes (default: 2)')
    parser.add_argument('--aq', action=argparse.BooleanOptionalAction, default=True,
                        help='NVENC spatial and temporal adaptive quantization (Windows only; default: on)')
    parser.add_argument('--capture', choices=('screencapturekit', 'avfoundation') if MAC else ('gfxcapture', 'ddagrab'),
                        default=MAC_CAPTURE if MAC else 'gfxcapture',
                        help='Windows: Graphics Capture with GPU scaling, or Desktop Duplication with CPU scaling. '
                             'macOS: ScreenCaptureKit on 12.3 and later, or AVFoundation through FFmpeg')
    parser.add_argument('--capture-fps', type=int, default=240, help='Maximum capture rate, in fps')
    parser.add_argument('--display', type=int, default=0, help='Zero-based monitor index')
    parser.add_argument('--hide-cursor', action='store_true')
    parser.add_argument('--preset', choices=PRESETS, default='p4',
                        help='NVENC preset, p1 fastest to p7 best quality (Windows only; default: p4)')
    parser.add_argument('--h264-profile', choices=('baseline', 'high'), default='high',
                        help='H.264 profile (default: high)')
    parser.add_argument('--keyframe-seconds', type=int, choices=(1, 3), default=3)
    parser.add_argument('--low-latency', action=argparse.BooleanOptionalAction, default=True,
                        help='Release complete encoded frames immediately and remove H.264 filler data (default: on)')
    parser.add_argument('--usb-write-size', type=int, choices=USB_WRITE_SIZES, default=16384,
                        help='Maximum bytes per USB write (default: 16384)')
    parser.add_argument('--seconds', type=float, default=0, help='Duration in seconds, including reconnects (0 runs until stopped)')
    parser.add_argument('--no-reconnect', action='store_true', help='Exit on USB or heartbeat loss')
    parser.add_argument('--stop-file', type=Path, help='Stop if this file is created')
    parser.add_argument('--log', type=Path, help='Path for a new JSON Lines log (default: a unique file in work/logs)')
    args = parser.parse_args()
    if args.stop_file and args.stop_file.exists():
        parser.error('--stop-file already exists; use a new path or remove the old stop marker')
    if args.python_deps:
        sys.path.insert(0, str(args.python_deps.resolve()))
    try:
        import usb
    except ImportError:
        usb = None
    if getattr(usb, '__version__', None) != '1.3.1':
        parser.error('PyUSB 1.3.1 is required. Run "python -m pip install -r requirements.txt"')
    if MAC:
        install_mac_dependencies(parser, args)
    if args.capture == 'screencapturekit':
        if not mac_capture.helper_path().is_file():
            print('Building the screen capture helper...', flush=True)
        try:
            mac_capture.build()
        except RuntimeError as exc:
            parser.error(str(exc))
    for name in ('dll', 'ffmpeg') if not MAC else ('ffmpeg',) if args.capture == 'avfoundation' else ():
        path = getattr(args, name)
        if path is None or not path.is_file():
            parser.error(SETUP_HINTS[name] if path in (None, DEFAULT_DLL) else f'--{name} file not found: {path}')
    from goggles_bridge.engine import BridgeEngine, BridgeSettings, check_encoder
    stop = threading.Event()
    log_path = args.log or ROOT / 'work' / 'logs' / f'{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.jsonl'

    def report(row):
        event = row['event']
        if event == 'waiting':
            print(f"Waiting / reconnecting: {row['message']}", flush=True)
        elif event == 'streaming':
            print(f'Streaming 1920x1080 at target {args.fps} fps, {args.bitrate_mbps} Mbps. Ctrl+C stops.', flush=True)
        elif event == 'stats':
            print(f"Sent {row['fps']:.1f} fps | queue p95 {row['queue_ms_p95']:.1f} ms | send p95 {row['send_ms_p95']:.1f} ms"
                  f" | largest frame {row['frame_bytes_max']/1024:.0f} KB", flush=True)
        elif event == 'oversize_frame':
            print(f"Skipped a {row['bytes']/1024:.0f} KB frame: the goggles accept at most {row['limit']/1024:.0f} KB per frame. "
                  'Lower --bitrate-mbps or --vbv-frames.', flush=True)
        elif event == 'ready':
            print(f"Found {row['model']} ({row['identity']})", flush=True)
        elif event in ('connected', 'stopped'):
            print(event.capitalize(), flush=True)

    try:
        settings = BridgeSettings(args.dll, args.ffmpeg, fps=args.fps, bitrate_mbps=args.bitrate_mbps,
            capture=args.capture, capture_fps=args.capture_fps, display_index=args.display,
            show_cursor=not args.hide_cursor, preset=args.preset, keyframe_seconds=args.keyframe_seconds,
            low_latency=args.low_latency, usb_write_size=args.usb_write_size, h264_profile=args.h264_profile,
            vbv_frames=args.vbv_frames, adaptive_quant=args.aq)
        print('Checking screen capture...', flush=True)
        problem = check_encoder(settings)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        engine = BridgeEngine(settings, log_path, on_event=report, stop_event=stop,
            stop_file=args.stop_file, duration=args.seconds, reconnect=not args.no_reconnect)
        previous_handler = signal.signal(signal.SIGINT, lambda *_: stop.set())
        print(f'Log: {log_path}\nPower the goggles and open More > DJI Virtual Flight.', flush=True)
        try:
            engine.run()
        finally:
            signal.signal(signal.SIGINT, previous_handler)
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Bridge stopped: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
