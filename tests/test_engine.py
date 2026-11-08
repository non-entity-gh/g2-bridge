from dataclasses import replace
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from goggles_bridge.control import V1Stream
from goggles_bridge.engine import BridgeEngine, BridgeSettings, check_encoder, encoder_command, prioritize_gpu
from goggles_bridge.mac_capture import helper_path, supported
from goggles_bridge.session import SessionError
from goggles_bridge.usb_transport import DeviceNotFoundError, UsbTransportError
from avi_fixture import stream as avi_stream


class EncoderOutput:
    def __init__(self, stopped, eof=False, framed=False, packet=b'\0\0\0\1\x09\xf0\0\0\0\1\x65test-picture'):
        self.stopped = stopped
        self.first = True
        self.eof = eof
        self.framed = framed
        self.packet = packet

    def read(self, _size):
        if self.eof:
            return b''
        if self.first:
            self.first = False
            return avi_stream([self.packet]) if self.framed else self.packet*3
        self.stopped.wait(2)
        return b''

    def close(self):
        pass


class FakeEncoder:
    pid = 123

    def __init__(self, eof=False, framed=False, **output):
        self.stopped = threading.Event()
        self.stdout = EncoderOutput(self.stopped, eof, framed, **output)
        self.terminated = False

    def poll(self):
        return 0 if self.stopped.is_set() else None

    def terminate(self):
        self.terminated = True
        self.stopped.set()

    def wait(self, timeout):
        return 0


class FakeUsb:
    def __init__(self, stop, *, wrong_model=False, disconnect=False):
        self.stop = stop
        self.wrong_model = wrong_model
        self.disconnect = disconnect
        self.incoming = queue.Queue()
        self.decoder = V1Stream()
        self.video = []
        self.mode = False
        self.closed = False

    def write(self, data):
        for request in self.decoder.feed(data):
            command = request.command_set, request.command_id
            if command == (11, 83):
                if not self.mode:
                    raise AssertionError('Video arrived before simulator acknowledgement')
                if self.disconnect:
                    raise UsbTransportError('Unplugged during video')
                self.video.append(request)
                self.stop.set()
                continue
            payload = b'\0'
            if command == (0, 255):
                payload += b'E3T ZV902' if self.wrong_model else b'E3T ZV900 AC'
            if command == (11, 26):
                self.mode = True
            response = replace(request, sender_type=request.receiver_type, sender_id=request.receiver_id,
                receiver_type=request.sender_type, receiver_id=request.sender_id, is_response=1, ack=2, payload=payload)
            self.incoming.put(response.serialize())

    def read(self, timeout_ms):
        try:
            return self.incoming.get(timeout=timeout_ms/1000)
        except queue.Empty:
            return b''

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class EngineTests(unittest.TestCase):
    def settings(self, **kwargs):
        return BridgeSettings(Path('driver.dll'), Path('ffmpeg.exe'), **kwargs)

    def run_case(self, factory, stop, *, encoder_factory=None, settings=None):
        events, encoders = [], []
        def create_encoder(*args, **kwargs):
            encoder = encoder_factory() if encoder_factory else FakeEncoder(framed=args[0][-2]=='avi')
            encoders.append(encoder)
            return encoder
        with tempfile.TemporaryDirectory() as folder:
            engine = BridgeEngine(settings or self.settings(), Path(folder)/'run.jsonl', stop_event=stop,
                on_event=events.append, transport_factory=factory, process_factory=create_encoder,
                retry_delay=.001, duration=2)
            engine.run()
        return events, encoders

    def test_handshake_video_and_stop_release_owned_resources(self):
        stop = threading.Event()
        usb = FakeUsb(stop)
        events, encoders = self.run_case(lambda _: usb, stop)
        self.assertTrue(usb.closed)
        self.assertTrue(usb.video)
        self.assertTrue(encoders[0].terminated)
        names = [r['event'] for r in events]
        self.assertLess(names.index('ready'), names.index('streaming'))
        self.assertEqual(names[-1], 'stopped')

    def test_disconnect_restarts_both_handshake_and_encoder(self):
        stop = threading.Event()
        devices = [FakeUsb(stop, disconnect=True), FakeUsb(stop)]
        calls = []
        def factory(_):
            result = devices[len(calls)]
            calls.append(result)
            return result
        events, encoders = self.run_case(factory, stop)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(d.closed for d in devices))
        self.assertTrue(all(p.terminated for p in encoders))
        self.assertEqual(sum(e['event']=='ready' for e in events), 2)
        self.assertTrue(any(e['event']=='waiting' for e in events))

    def test_low_latency_sends_one_complete_picture_before_any_next_frame(self):
        stop = threading.Event()
        usb = FakeUsb(stop)
        events, encoders = self.run_case(lambda _: usb, stop, settings=self.settings(low_latency=True))
        self.assertEqual(b''.join(p.payload[32:] for p in usb.video), b'\0\0\0\1\x65test-picture')
        self.assertTrue(usb.closed)
        self.assertTrue(encoders[0].terminated)
        self.assertTrue(any(e['event']=='streaming' for e in events))

    def test_gpu_priority_is_best_effort(self):
        self.assertFalse(prioritize_gpu(FakeEncoder()))
        if sys.platform != 'win32':
            return
        # The kernel accepts the call only after the process touches the graphics stack, as FFmpeg has by its first frame
        process = subprocess.Popen([sys.executable, '-c', "import ctypes; ctypes.WinDLL('user32').GetDesktopWindow()"
                                    "; print('ready', flush=True); import time; time.sleep(5)"],
                                   stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertEqual(process.stdout.readline().strip(), b'ready')
            self.assertTrue(prioritize_gpu(process))
        finally:
            process.kill()
            process.wait(timeout=5)

    def test_absent_device_can_be_stopped_while_waiting(self):
        stop = threading.Event()
        def missing(_):
            stop.set()
            raise DeviceNotFoundError('No goggles')
        events, encoders = self.run_case(missing, stop)
        self.assertEqual(encoders, [])
        self.assertEqual(events[-1]['event'], 'stopped')

    def test_wrong_model_is_fatal_and_never_starts_encoder(self):
        stop = threading.Event()
        usb = FakeUsb(stop, wrong_model=True)
        with self.assertRaises(SessionError):
            self.run_case(lambda _: usb, stop, encoder_factory=lambda: self.fail('Must not encode'))
        self.assertTrue(usb.closed)
        self.assertFalse(usb.video)

    def test_encoder_eof_is_fatal_and_releases_usb(self):
        stop = threading.Event()
        usb = FakeUsb(stop)
        with self.assertRaisesRegex(RuntimeError, 'Capture ended'):
            self.run_case(lambda _: usb, stop, encoder_factory=lambda: FakeEncoder(eof=True))
        self.assertTrue(usb.closed)

    def test_gpu_capture_preserves_verified_encoder_contract(self):
        command = encoder_command(self.settings())
        source = command[command.index('-i')+1]
        self.assertIn('scale_aspect', source)
        self.assertNotIn('-vf', command)
        for key, value in (('-r','100'), ('-level:v','5.1'), ('-bf','0'), ('-aud','1'), ('-bufsize','400000')):
            self.assertEqual(command[command.index(key)+1], value)
        command = encoder_command(self.settings(fps=60, capture='ddagrab'))
        self.assertEqual(command[command.index('-level:v')+1], '4.2')
        self.assertIn('force_original_aspect_ratio=decrease', command[command.index('-vf')+1])

    def test_mac_capture_keeps_stream_contract_and_adds_delimiters(self):
        for low_latency, tail in ((True, ['-f', 'avi', 'pipe:1']), (False, ['-f', 'h264', 'pipe:1'])):
            with self.subTest(low_latency=low_latency):
                command = encoder_command(self.settings(capture='avfoundation', display_index=1,
                                                        low_latency=low_latency))
                self.assertEqual(command[command.index('-i')+1], 'Capture screen 1:none')
                self.assertIn('pad=1920:1080', command[command.index('-vf')+1])
                for key, value in (('-c:v','h264_videotoolbox'), ('-flags','+low_delay'), ('-r','100'),
                                   ('-level:v','5.1'), ('-bf','0'), ('-bufsize','400000'),
                                   ('-bsf:v','h264_metadata=aud=insert')):
                    self.assertEqual(command[command.index(key)+1], value)
                self.assertEqual(command[-3:], tail)
        command = encoder_command(self.settings(capture='avfoundation', h264_profile='baseline'))
        self.assertEqual(command[command.index('-profile:v')+1], 'constrained_baseline')
        settings = self.settings(capture='avfoundation')
        fail = lambda stderr: lambda command, **_: subprocess.CompletedProcess(command, 1, '', stderr)
        self.assertIn('brew upgrade ffmpeg', check_encoder(settings, fail('Error opening output files: Filter not found')))
        self.assertIn('screen recording', check_encoder(settings, fail('Error opening input files: Input/output error')))

    def test_screencapturekit_helper_gets_stream_settings(self):
        command = encoder_command(BridgeSettings(None, None, capture='screencapturekit', display_index=1,
                                                 show_cursor=False, low_latency=False))
        self.assertEqual(Path(command[0]), helper_path())
        for key, value in (('--display', '1'), ('--fps', '100'), ('--capture-fps', '240'), ('--bitrate', '20000000'),
                           ('--keyframe-seconds', '3'), ('--profile', 'high'), ('--format', 'h264')):
            self.assertEqual(command[command.index(key)+1], value)
        self.assertEqual(command[-1], '--hide-cursor')
        for version, expected in (('11.7.10', False), ('12.2', False), ('12.3', True), ('12.3.1', True), ('27.0', True)):
            self.assertIs(supported(version), expected, version)

    def test_low_latency_only_changes_local_framing_and_padding(self):
        command = encoder_command(self.settings(low_latency=True))
        for key, value in (('-r','100'), ('-b:v','20000000'), ('-preset','p4'), ('-bf','0'),
                           ('-bufsize','400000'), ('-bsf:v','filter_units=remove_types=12'),
                           ('-thread_queue_size','1')):
            self.assertEqual(command[command.index(key)+1], value)
        self.assertEqual(command[-3:], ['-f', 'avi', 'pipe:1'])

    def test_quality_profile_keeps_latency_and_capture_settings(self):
        for low_latency in (False, True):
            with self.subTest(low_latency=low_latency):
                baseline = encoder_command(self.settings(low_latency=low_latency))
                candidate = encoder_command(self.settings(low_latency=low_latency, h264_profile='high'))
                baseline[baseline.index('-profile:v')+1] = 'high'
                self.assertEqual(candidate, baseline)
        with self.assertRaises(ValueError):
            self.settings(h264_profile='unsupported')

    def test_quality_switches_default_off_and_only_touch_rate_control(self):
        default = encoder_command(self.settings())
        self.assertEqual(default[default.index('-spatial-aq')+1], '1')
        baseline = encoder_command(self.settings(adaptive_quant=False))
        self.assertNotIn('-spatial-aq', baseline)
        self.assertEqual(baseline[baseline.index('-bufsize')+1], '400000')
        candidate = encoder_command(self.settings(vbv_frames=8, adaptive_quant=True, preset='p7', bitrate_mbps=40))
        # The buffer is capped so keyframes stay under the goggles' frame limit
        self.assertEqual(candidate[candidate.index('-bufsize')+1], '448000')
        small = encoder_command(self.settings(vbv_frames=2, bitrate_mbps=16))
        self.assertEqual(small[small.index('-bufsize')+1], '320000')
        self.assertEqual(candidate[candidate.index('-preset')+1], 'p7')
        self.assertEqual(candidate[candidate.index('-spatial-aq')+1], '1')
        self.assertEqual(candidate[candidate.index('-temporal-aq')+1], '1')
        stripped = [a for i, a in enumerate(candidate) if a not in ('-spatial-aq', '-temporal-aq')
                    and candidate[i-1] not in ('-spatial-aq', '-temporal-aq')]
        for key in ('-bufsize', '-preset', '-b:v', '-maxrate'):
            stripped[stripped.index(key)+1] = baseline[baseline.index(key)+1]
        self.assertEqual(stripped, baseline)
        for bad in (dict(vbv_frames=3), dict(adaptive_quant=1), dict(preset='p8'), dict(bitrate_mbps=50),
                    dict(fps=60, bitrate_mbps=30)):
            with self.assertRaises(ValueError):
                self.settings(**bad)
        with self.assertRaisesRegex(ValueError, 'at most 24 Mbps at 60 fps'):
            self.settings(fps=60, bitrate_mbps=40)
        self.settings(fps=60, bitrate_mbps=24)
        self.settings(fps=100, bitrate_mbps=40)

    def test_oversize_frame_is_logged_and_never_sent(self):
        stop = threading.Event()
        usbs = []
        def factory(_dll):
            usb = FakeUsb(stop)
            usbs.append(usb)
            return usb
        big = b'\0\0\0\1\x09\xf0\0\0\0\1\x65' + bytes(70000)
        events, _ = self.run_case(factory, stop, encoder_factory=lambda: FakeEncoder(framed=True, packet=big))
        self.assertEqual(usbs[0].video, [])
        dropped = [e for e in events if e['event'] == 'oversize_frame']
        self.assertEqual(len(dropped), 1)
        self.assertGreater(dropped[0]['bytes'], 65536)
        self.assertEqual(dropped[0]['limit'], 65536)

    def test_encoder_check_runs_one_frame_and_explains_missing_features(self):
        calls = []
        def run(stderr, code=1):
            def fake(command, **_):
                calls.append(command)
                return subprocess.CompletedProcess(command, code, '', stderr)
            return fake
        settings = self.settings()
        self.assertIsNone(check_encoder(settings, run('', 0)))
        self.assertEqual(calls[0][:-5], encoder_command(settings)[:-3])
        self.assertEqual(calls[0][-5:], ['-frames:v', '1', '-f', 'null', '-'])
        self.assertIn('--capture ddagrab', check_encoder(settings, run('Error opening input files: Filter not found')))
        self.assertIn('NVIDIA encoder', check_encoder(settings, run('Error opening output files: Encoder not found')))
        self.assertIn('OpenEncodeSessionEx failed', check_encoder(settings, run('a\nb\nOpenEncodeSessionEx failed')))
