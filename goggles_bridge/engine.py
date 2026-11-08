"""Capture the desktop and stream H.264 over the Goggles 2 PC USB route."""
from dataclasses import dataclass
from functools import partial
from pathlib import Path
import json
import math
import queue
import subprocess
import sys
import threading
import time

from .annexb import AccessUnitStream, strip_aud
from .avi import AviPacketStream
from .control import V1Stream
from .mac_capture import helper_path
from .pc_video import PcVideoPacketizer
from .session import ControlSession, SessionLost
from .usb_transport import ControlUsb, UsbTransportError, USB_WRITE_SIZES


BITRATES_MBPS = (8, 10, 12, 16, 20, 24, 30, 40)
PRESETS = ('p1', 'p4', 'p5', 'p6', 'p7')
VBV_FRAMES = (2, 4, 8, 16)
# Goggles 2 displayed every stream whose frames stayed under about 42 KB and dropped or
# froze on streams whose keyframes reached 67 KB and more, consistent with the 65,404-byte
# fragment limit of the mobile protocol. An oversize frame can leave the goggles on a black
# screen until power-cycled, so the bridge never sends one and keeps the encoder's
# rate-control buffer small enough that keyframes stay below the limit with headroom.
MAX_FRAME_BYTES = 65536
SAFE_FRAME_BYTES = 56_000
MAX_VBV_BITS = SAFE_FRAME_BYTES * 8


@dataclass(frozen=True)
class BridgeSettings:
    dll: Path
    ffmpeg: Path
    fps: int = 100
    bitrate_mbps: int = 20
    capture: str = 'gfxcapture'
    capture_fps: int = 240
    display_index: int = 0
    show_cursor: bool = True
    preset: str = 'p4'
    keyframe_seconds: int = 3
    low_latency: bool = True
    usb_write_size: int = 16384
    h264_profile: str = 'high'
    # Rate-control buffer in frames. Two frames keeps bursts short but forces keyframes
    # into a two-frame budget, which shows as a blocky picture every keyframe interval.
    vbv_frames: int = 2
    # NVENC spatial and temporal adaptive quantization (Windows only). It costs no
    # measurable encode time and scores higher on detailed scenes at the same bitrate.
    adaptive_quant: bool = True

    def __post_init__(self):
        if type(self.low_latency) is not bool:
            raise ValueError('low_latency must be a boolean')
        if type(self.adaptive_quant) is not bool:
            raise ValueError('adaptive_quant must be a boolean')
        if type(self.usb_write_size) is not int or self.usb_write_size not in USB_WRITE_SIZES:
            raise ValueError(f'Unsupported USB write size: {self.usb_write_size}')
        for name, choices in (('fps', (60, 100)), ('bitrate_mbps', BITRATES_MBPS),
                              ('capture', ('gfxcapture', 'ddagrab', 'avfoundation', 'screencapturekit')), ('preset', PRESETS),
                              ('keyframe_seconds', (1, 3)), ('h264_profile', ('baseline', 'high')),
                              ('vbv_frames', VBV_FRAMES)):
            if getattr(self, name) not in choices:
                raise ValueError(f'Unsupported {name}: {getattr(self, name)}')
        # In CBR the buffer cap bounds keyframes, but ordinary frames average bitrate/fps
        if self.bitrate_mbps * 1_000_000 // (8 * self.fps) > SAFE_FRAME_BYTES:
            limit = max(b for b in BITRATES_MBPS if b * 1_000_000 // (8 * self.fps) <= SAFE_FRAME_BYTES)
            raise ValueError(f'{self.bitrate_mbps} Mbps at {self.fps} fps makes frames larger than the goggles '
                             f'accept; use at most {limit} Mbps at {self.fps} fps')
        if type(self.capture_fps) is not int or not self.fps <= self.capture_fps <= 1000:
            raise ValueError('Capture ceiling must be between output fps and 1000')
        if type(self.display_index) is not int or self.display_index < 0:
            raise ValueError('Display index must be a nonnegative integer')


def encoder_command(settings):
    bitrate = settings.bitrate_mbps * 1_000_000
    if settings.capture == 'screencapturekit':
        command = [str(helper_path()), '--display', str(settings.display_index), '--fps', str(settings.fps),
                   '--capture-fps', str(settings.capture_fps), '--bitrate', str(bitrate),
                   '--keyframe-seconds', str(settings.keyframe_seconds), '--profile', settings.h264_profile,
                   '--format', 'avi' if settings.low_latency else 'h264']
        return command + ([] if settings.show_cursor else ['--hide-cursor'])
    command = [str(Path(settings.ffmpeg).resolve()), '-hide_banner', '-nostdin', '-loglevel', 'info']
    if settings.capture == 'avfoundation':
        return command + _mac_encoder_args(settings, bitrate)
    if settings.capture == 'gfxcapture':
        source = (f'gfxcapture=monitor_idx={settings.display_index}:max_framerate={settings.capture_fps}'
                  ':width=1920:height=1080:resize_mode=scale_aspect:scale_mode=bicubic'
                  f':capture_cursor={int(settings.show_cursor)}')
        command += ['-f', 'lavfi', '-i', source, '-r', str(settings.fps), '-fps_mode', 'cfr']
    else:
        source = (f'ddagrab=output_idx={settings.display_index}:framerate={settings.fps}'
                  f':draw_mouse={int(settings.show_cursor)}')
        command += ['-f', 'lavfi', '-i', source, '-vf',
                    'hwdownload,format=bgra,scale=1920:1080:force_original_aspect_ratio=decrease:flags=fast_bilinear,'
                    'pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=nv12']
    command += ['-an', '-c:v', 'h264_nvenc', '-preset', settings.preset, '-tune', 'ull', '-rc', 'cbr',
                '-b:v', str(bitrate), '-maxrate', str(bitrate),
                '-bufsize', str(min(bitrate*settings.vbv_frames//settings.fps, MAX_VBV_BITS))]
    if settings.adaptive_quant:
        command += ['-spatial-aq', '1', '-temporal-aq', '1']
    command += ['-zerolatency', '1', '-delay', '0', '-bf', '0', '-g', str(settings.fps*settings.keyframe_seconds),
                '-profile:v', settings.h264_profile, '-level:v', '4.2' if settings.fps == 60 else '5.1',
                '-aud', '1', '-forced-idr', '1', '-flush_packets', '1']
    if settings.low_latency:
        command += ['-bsf:v', 'filter_units=remove_types=12', '-thread_queue_size', '1', '-f', 'avi', 'pipe:1']
    else:
        command += ['-f', 'h264', 'pipe:1']
    return command


def _mac_encoder_args(settings, bitrate):
    # Scale Retina captures on the GPU, then pad to the 1920x1080 the goggles expect
    wide = 'gte(iw/ih,16/9)'
    args = ['-init_hw_device', 'videotoolbox=vt', '-filter_hw_device', 'vt',
            '-f', 'avfoundation', '-capture_cursor', str(int(settings.show_cursor)),
            '-framerate', str(settings.capture_fps), '-pixel_format', 'nv12',
            '-i', f'Capture screen {settings.display_index}:none', '-r', str(settings.fps), '-fps_mode', 'cfr',
            '-vf', f"hwupload,scale_vt=w='if({wide},1920,-2)':h='if({wide},-2,1080)',hwdownload,format=nv12,"
                   'pad=1920:1080:(ow-iw)/2:(oh-ih)/2',
            # low_delay enables low-latency rate control, which rejects plain baseline
            '-an', '-c:v', 'h264_videotoolbox', '-realtime', '1', '-prio_speed', '1', '-flags', '+low_delay',
            '-b:v', str(bitrate), '-maxrate', str(bitrate),
            '-bufsize', str(min(bitrate*settings.vbv_frames//settings.fps, MAX_VBV_BITS)),
            '-bf', '0', '-g', str(settings.fps*settings.keyframe_seconds),
            '-profile:v', 'constrained_baseline' if settings.h264_profile == 'baseline' else 'high',
            '-level:v', '4.2' if settings.fps == 60 else '5.1', '-flush_packets', '1',
            # VideoToolbox writes no AUDs, which both frame readers need
            '-bsf:v', 'h264_metadata=aud=insert']
    if settings.low_latency:
        return args + ['-thread_queue_size', '1', '-f', 'avi', 'pipe:1']
    return args + ['-f', 'h264', 'pipe:1']


def check_encoder(settings, run=subprocess.run):
    """Encode one captured frame and return a setup problem, or None if the encoder can stream."""
    if settings.capture == 'screencapturekit':
        command = encoder_command(settings) + ['--frames', '1']
    else:
        command = encoder_command(settings)[:-3] + ['-frames:v', '1', '-f', 'null', '-']
    try:
        result = run(command, capture_output=True, text=True, errors='replace', timeout=30,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        return 'No frame was captured within 30 seconds'
    if result.returncode == 0:
        return None
    details = '\n  '.join(result.stderr.strip().splitlines()[-3:])
    if settings.capture == 'screencapturekit':
        return f'Screen capture test failed.\n  {details}'
    if settings.capture == 'avfoundation':
        if 'Filter not found' in result.stderr or 'Encoder not found' in result.stderr:
            return 'This FFmpeg build lacks VideoToolbox support. Update it with "brew upgrade ffmpeg"'
        return ('FFmpeg test encode failed. In System Settings > Privacy & Security, allow screen recording '
                f'for this terminal, and check the --display index.\n  {details}')
    if 'Filter not found' in result.stderr:
        return (f'This FFmpeg build has no {settings.capture} capture. '
                'Update it with "winget upgrade Gyan.FFmpeg", or try --capture ddagrab')
    if 'Encoder not found' in result.stderr:
        return 'This FFmpeg build has no NVIDIA encoder. Install it with "winget install Gyan.FFmpeg"'
    return f'FFmpeg test encode failed. Check the NVIDIA driver and --display index.\n  {details}'


def prioritize_gpu(process):
    """Give the encoder realtime GPU scheduling so a saturated simulator cannot queue ahead of it.

    Call it once the process has produced output: the kernel rejects processes that
    have not yet created a graphics context. Needs no elevation. Under synthetic GPU
    saturation it cut the capture-to-packet p95 from about 6.7 to 3.5 ms; the HIGH
    class changed nothing. Returns False where unsupported.
    """
    handle = getattr(process, '_handle', None)
    if handle is None or sys.platform != 'win32':
        return False
    import ctypes
    set_class = ctypes.WinDLL('gdi32').D3DKMTSetProcessSchedulingPriorityClass
    set_class.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return set_class(int(handle), 5) == 0  # D3DKMT_SCHEDULINGPRIORITYCLASS_REALTIME


class BridgeEngine:
    """Manage the encoder and USB session, reconnecting after link loss."""
    def __init__(self, settings, log_path, *, on_event=None, stop_event=None,
                 stop_file=None, duration=0, reconnect=True, retry_delay=1,
                 transport_factory=None, process_factory=subprocess.Popen):
        if not math.isfinite(duration) or duration < 0:
            raise ValueError('Duration must be nonnegative and finite')
        if not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError('Retry delay must be nonnegative and finite')
        self.settings = settings
        self.log_path = Path(log_path)
        self.on_event = on_event or (lambda event: None)
        self.stop_event = stop_event or threading.Event()
        self.stop_file = Path(stop_file) if stop_file else None
        self.duration = duration
        self.reconnect = reconnect
        self.retry_delay = retry_delay
        self.transport_factory = (partial(ControlUsb, write_size=settings.usb_write_size)
                                  if transport_factory is None else transport_factory)
        self.process_factory = process_factory
        self.started = None
        self.log = None
        self.waiting = None

    def stopping(self):
        return (self.stop_event.is_set() or (self.stop_file is not None and self.stop_file.exists())
                or (self.duration > 0 and time.perf_counter()-self.started >= self.duration))

    def emit(self, event, **fields):
        row = dict(seconds=round(time.perf_counter()-self.started, 6), event=event, **fields)
        self.log.write(json.dumps(row)+'\n')
        self.log.flush()
        self.on_event(row)

    def run(self):
        self.started = time.perf_counter()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open('x', encoding='utf-8') as self.log:
            self.emit('starting', fps=self.settings.fps, bitrate_mbps=self.settings.bitrate_mbps,
                      capture=self.settings.capture, capture_fps=self.settings.capture_fps,
                      resolution='1920x1080', preset=self.settings.preset,
                      keyframe_seconds=self.settings.keyframe_seconds, display_index=self.settings.display_index,
                      low_latency=self.settings.low_latency, usb_write_size=self.settings.usb_write_size,
                      h264_profile=self.settings.h264_profile, vbv_frames=self.settings.vbv_frames,
                      adaptive_quant=self.settings.adaptive_quant)
            try:
                attempt = 0
                while not self.stopping():
                    attempt += 1
                    try:
                        self._session(attempt)
                    except (UsbTransportError, SessionLost) as exc:
                        if not self.reconnect:
                            raise
                        if self.waiting != str(exc):
                            self.emit('waiting', message=str(exc))
                        self.waiting = str(exc)
                        retry_at = time.perf_counter()+self.retry_delay
                        while not self.stopping() and time.perf_counter() < retry_at:
                            self.stop_event.wait(min(.1, max(0, retry_at-time.perf_counter())))
            except Exception as exc:
                self.emit('error', message=str(exc))
                raise
            finally:
                self.emit('stopped')

    def _session(self, attempt):
        local_stop = threading.Event()
        incoming = queue.Queue(maxsize=256)
        frames = queue.Queue(maxsize=1 if self.settings.low_latency else 3)
        errors = queue.Queue()
        session = ControlSession()
        decoder = V1Stream()
        packetizer = PcVideoPacketizer()
        process = None
        threads = []
        frame_count = byte_count = previous_frames = oversize_count = largest_frame = 0
        queue_times, send_times = [], []
        last_frame = last_progress = time.perf_counter()
        encoder_path = self.log_path.with_name(f'{self.log_path.stem}.encoder-{attempt}.log')
        with self.transport_factory(self.settings.dll) as usb, encoder_path.open('x', encoding='utf-8') as encoder_log:
            self.emit('connected', attempt=attempt)

            def read_usb():
                try:
                    while not local_stop.is_set():
                        data = usb.read(timeout_ms=50)
                        if data:
                            incoming.put(data, timeout=.2)
                except Exception as exc:
                    if not local_stop.is_set():
                        errors.put(exc)

            def read_encoder():
                splitter = AviPacketStream() if self.settings.low_latency else AccessUnitStream()
                try:
                    while not local_stop.is_set():
                        data = process.stdout.read(16384)
                        if not data:
                            raise RuntimeError(f'Capture ended. See {encoder_path}')
                        for packet in splitter.feed(data):
                            frame = strip_aud(packet) if self.settings.low_latency else packet
                            completed_at = time.perf_counter()
                            while not local_stop.is_set():
                                try:
                                    frames.put((completed_at, frame), timeout=.1)
                                    break
                                except queue.Full:
                                    pass
                except Exception as exc:
                    if not local_stop.is_set():
                        errors.put(exc)

            def send_control(packet):
                data = packet.serialize()
                usb.write(data)
                self.emit('control_send', hex=data.hex())

            try:
                thread = threading.Thread(target=read_usb, daemon=True)
                threads.append(thread)
                thread.start()
                while not self.stopping():
                    if not errors.empty():
                        raise errors.get_nowait()
                    # Accept queued replies before judging their deadlines
                    while not incoming.empty():
                        data = incoming.get_nowait()
                        self.emit('control_receive', hex=data.hex())
                        for reply in decoder.feed(data):
                            for packet in session.receive(reply, time.perf_counter()):
                                send_control(packet)
                    for packet in session.poll(time.perf_counter()):
                        send_control(packet)
                    if session.state == 'ready' and process is None:
                        self.waiting = None
                        self.emit('ready', model=session.model, identity=session.identity)
                        process = self.process_factory(encoder_command(self.settings), stdout=subprocess.PIPE,
                            stderr=encoder_log, stdin=subprocess.DEVNULL, bufsize=0,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        last_frame = time.perf_counter()
                        thread = threading.Thread(target=read_encoder, daemon=True)
                        threads.append(thread)
                        thread.start()
                    if session.state == 'ready':
                        try:
                            completed_at, frame = frames.get(timeout=.002)
                        except queue.Empty:
                            if time.perf_counter()-last_frame > 5:
                                raise RuntimeError(f'No video from display. See {encoder_path}')
                            continue
                        last_frame = time.perf_counter()
                        if len(frame) > MAX_FRAME_BYTES:
                            oversize_count += 1
                            self.emit('oversize_frame', bytes=len(frame), limit=MAX_FRAME_BYTES)
                            continue
                        largest_frame = max(largest_frame, len(frame))
                        queue_times.append((last_frame-completed_at)*1000)
                        wire = b''.join(packet.serialize() for packet in
                                        packetizer.packetize(frame, session.reserve_sequence))
                        usb.write(wire)
                        send_times.append((time.perf_counter()-last_frame)*1000)
                        byte_count += len(wire)
                        frame_count += 1
                        if frame_count == 1:
                            self.emit('streaming', encoder_pid=process.pid, gpu_realtime=prioritize_gpu(process))
                    else:
                        self.stop_event.wait(.002)
                    now = time.perf_counter()
                    if now-last_progress >= 5:
                        def p95(values):
                            return round(sorted(values)[int((len(values)-1)*.95)], 2) if values else 0
                        self.emit('stats', frames=frame_count, bytes=byte_count, queued_frames=frames.qsize(),
                                  fps=round((frame_count-previous_frames)/(now-last_progress), 2),
                                  queue_ms_p95=p95(queue_times), send_ms_p95=p95(send_times),
                                  frame_bytes_max=largest_frame, oversize_frames=oversize_count)
                        previous_frames, last_progress, largest_frame = frame_count, now, 0
                        queue_times.clear()
                        send_times.clear()
            finally:
                local_stop.set()
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                for thread in threads:
                    thread.join(timeout=1)
                if process and process.stdout:
                    process.stdout.close()
                self.emit('session_ended', frames=frame_count, bytes=byte_count,
                          discarded_bytes=decoder.discarded)
