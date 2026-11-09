"""Settings, paths, and setup checks shared by the desktop app and the command line.

Nothing here touches USB or starts FFmpeg, so it can be tested without goggles.
"""
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

from .engine import BITRATES_MBPS, PRESETS, VBV_FRAMES, SAFE_FRAME_BYTES, BridgeSettings
from .usb_transport import DEFAULT_DLL, VERIFIED_DLL_SHA256

APP_NAME = 'G2 Bridge'
ROOT = Path(__file__).resolve().parents[1]
FROZEN = getattr(sys, 'frozen', False)
WINDOWS = sys.platform == 'win32'
MAC = sys.platform == 'darwin'
DJI_ASSISTANT_URL = 'https://www.dji.com/downloads/softwares/dji-assistant-2-consumer-drones-series'
# winget adds this folder to PATH, but processes started before the install miss it
WINGET_LINKS = Path(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Links')

# Each preset stays inside the goggles' per-frame limit; see docs/research/EXPERIMENTS.md
QUALITY_PRESETS = {
    'latency': dict(label='Lowest latency', bitrate_mbps=20,
                    description='20 Mbps. The setting the published latency figures used.'),
    'balanced': dict(label='Balanced', bitrate_mbps=30,
                     description='30 Mbps. Sharper, about 0.4 ms more USB time per frame.'),
    'quality': dict(label='Best quality', bitrate_mbps=40,
                    description='40 Mbps. Sharpest, about 0.5 ms more USB time per frame.'),
}
CAPTURES = ('screencapturekit', 'avfoundation') if MAC else ('gfxcapture', 'ddagrab')


def max_bitrate(fps):
    """Largest offered bitrate whose ordinary frames stay under the goggles' frame limit."""
    return max(b for b in BITRATES_MBPS if b * 1_000_000 // (8 * fps) <= SAFE_FRAME_BYTES)


@dataclass
class AppSettings:
    """What the app remembers between launches. Advanced fields override the preset."""
    quality: str = 'latency'
    display_index: int = 0
    fps: int = 100
    hide_cursor: bool = False
    custom_bitrate_mbps: int = 0  # 0 follows the quality preset
    adaptive_quant: bool = True
    encoder_preset: str = 'p4'
    vbv_frames: int = 2
    capture: str = CAPTURES[0]

    def bitrate_mbps(self):
        wanted = self.custom_bitrate_mbps or QUALITY_PRESETS[self.quality]['bitrate_mbps']
        return min(wanted, max_bitrate(self.fps))

    def bridge_settings(self, dll, ffmpeg):
        return BridgeSettings(dll, ffmpeg, fps=self.fps, bitrate_mbps=self.bitrate_mbps(), capture=self.capture,
                              display_index=self.display_index, show_cursor=not self.hide_cursor,
                              preset=self.encoder_preset, vbv_frames=self.vbv_frames,
                              adaptive_quant=self.adaptive_quant)

    @classmethod
    def load(cls, path):
        """Read saved settings, keeping defaults for anything missing or invalid."""
        try:
            saved = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return cls()
        if not isinstance(saved, dict):
            return cls()
        settings = cls()
        allowed = dict(quality=set(QUALITY_PRESETS), fps={60, 100}, custom_bitrate_mbps={0, *BITRATES_MBPS},
                       encoder_preset=set(PRESETS), vbv_frames=set(VBV_FRAMES), capture=set(CAPTURES))
        for field in fields(cls):
            value = saved.get(field.name)
            if isinstance(getattr(settings, field.name), bool):
                ok = isinstance(value, bool)
            elif field.name == 'display_index':
                ok = type(value) is int and 0 <= value < 16
            else:
                ok = value in allowed.get(field.name, ())
            if ok:
                setattr(settings, field.name, value)
        return settings

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(asdict(self), indent=1), encoding='utf-8')
        os.replace(temporary, path)


def data_dir():
    """Where settings and logs go: the user's app-data folder when packaged, work/ in a checkout."""
    if not FROZEN:
        return ROOT / 'work'
    if WINDOWS:
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local', APP_NAME)
    if MAC:
        return Path.home() / 'Library' / 'Application Support' / APP_NAME
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share', APP_NAME)


def app_dir():
    """The folder holding the packaged executable, or the checkout root."""
    return Path(sys.executable).resolve().parent if FROZEN else ROOT


def find_ffmpeg(environ=os.environ, which=shutil.which, base=None):
    """Prefer the FFmpeg shipped next to the app, then GOGGLES_FFMPEG, then PATH and winget."""
    exe = 'ffmpeg.exe' if WINDOWS else 'ffmpeg'
    bundled = Path(base or app_dir(), 'ffmpeg', exe)
    if bundled.is_file():
        return bundled
    if environ.get('GOGGLES_FFMPEG'):
        return Path(environ['GOGGLES_FFMPEG'])
    found = which('ffmpeg') or (which('ffmpeg', path=str(WINGET_LINKS)) if WINDOWS else None)
    return Path(found) if found else None


@dataclass(frozen=True)
class SetupProblem:
    message: str
    action: str = None  # Button label
    url: str = None


def setup_problems(dll, ffmpeg, capture=CAPTURES[0]):
    """Return what stops streaming before the goggles are even connected."""
    problems = []
    if WINDOWS:
        if dll is None or not Path(dll).is_file():
            problems.append(SetupProblem(
                'The DJI USB driver is not installed. Install DJI Assistant 2 (Consumer Drones Series), '
                'which includes it, then come back to this window.', 'Download DJI Assistant 2', DJI_ASSISTANT_URL))
        elif hashlib.sha256(Path(dll).read_bytes()).hexdigest() != VERIFIED_DLL_SHA256:
            problems.append(SetupProblem(
                f'The DJI USB driver at {dll} is a different version from the tested 5.0.0.7. '
                'Install the latest DJI Assistant 2 (Consumer Drones Series).',
                'Download DJI Assistant 2', DJI_ASSISTANT_URL))
    if capture != 'screencapturekit' and (ffmpeg is None or not Path(ffmpeg).is_file()):
        problems.append(SetupProblem(
            'FFmpeg is missing from the app folder. Extract the whole zip again, keeping the ffmpeg folder.'
            if FROZEN else 'FFmpeg was not found. Run "winget install Gyan.FFmpeg"'
            if WINDOWS else 'FFmpeg was not found. Run "brew install ffmpeg"'))
    return problems


def default_dll():
    return Path(os.environ['DJI_USB_DLL']) if os.environ.get('DJI_USB_DLL') else DEFAULT_DLL if WINDOWS else None


def pacing_advice(refresh_hz, fps):
    """Explain uneven motion when the monitor's refresh rate is not a multiple of the stream's."""
    if not refresh_hz or refresh_hz <= 1 or refresh_hz % fps == 0:
        return None
    return (f'This monitor runs at {refresh_hz} Hz and the goggles show {fps} fps, so motion repeats or skips '
            f'frames. For smooth motion, set the monitor to {fps} Hz if it supports that, or cap games at '
            f'{fps} fps with G-SYNC or FreeSync on.')
