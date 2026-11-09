"""Drive the window with fake capture checks and a fake engine; no goggles or FFmpeg needed."""
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from goggles_bridge import app_config
from goggles_bridge.displays import Display
from goggles_bridge.session import SIMULATOR_SILENT

try:
    import tkinter as tk
    root = tk.Tk()
    root.destroy()
    HAVE_DISPLAY = True
except Exception:
    HAVE_DISPLAY = False


class FakeEngine:
    instances = []

    def __init__(self, settings, log_path, *, on_event, stop_event):
        self.settings, self.log_path, self.on_event, self.stop_event = settings, log_path, on_event, stop_event
        self.release = threading.Event()
        FakeEngine.instances.append(self)

    def run(self):
        self.release.wait(5)
        self.on_event(dict(event='stopped'))


@unittest.skipUnless(HAVE_DISPLAY, 'Tk needs a display')
class GuiTests(unittest.TestCase):
    def setUp(self):
        from goggles_bridge.gui import BridgeApp
        self.folder = tempfile.TemporaryDirectory()
        folder = Path(self.folder.name)
        self.dll, self.ffmpeg = folder / 'driver.dll', folder / 'ffmpeg.exe'
        self.dll.write_bytes(b'driver')
        self.ffmpeg.write_bytes(b'')
        self.patches = [mock.patch.object(app_config, 'VERIFIED_DLL_SHA256', hashlib.sha256(b'driver').hexdigest()),
                        mock.patch.object(app_config, 'WINDOWS', True)]
        for patch in self.patches:
            patch.start()
        FakeEngine.instances.clear()
        self.opened = []
        self.checked = []
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = BridgeApp(self.root, settings_path=folder / 'settings.json', logs_dir=folder / 'logs',
                             dll=self.dll, ffmpeg=self.ffmpeg, engine_factory=FakeEngine,
                             check=lambda s: self.checked.append(s), open_url=self.opened.append,
                             displays=lambda: [Display(0, 3840, 2160, 144, True), Display(1, 1920, 1080, 100)])

    def tearDown(self):
        for engine in FakeEngine.instances:
            engine.release.set()
        if self.app.worker:
            self.app.worker.join(2)
        self.app.cancel_polling()
        self.root.destroy()
        for patch in self.patches:
            patch.stop()
        self.folder.cleanup()

    def pump(self, until, seconds=3):
        deadline = threading.Event()
        timer = threading.Timer(seconds, deadline.set)
        timer.start()
        try:
            while not until() and not deadline.is_set():
                self.root.update()
        finally:
            timer.cancel()
        self.assertTrue(until(), 'condition not reached')

    def test_starts_with_saved_preset_and_streams(self):
        self.app.quality.set('quality')
        self.app.display.current(1)
        self.app._changed()
        self.assertEqual(self.app.pacing['text'], '')
        self.app.toggle()
        self.pump(lambda: FakeEngine.instances)
        engine = FakeEngine.instances[0]
        self.assertEqual((engine.settings.bitrate_mbps, engine.settings.display_index), (40, 1))
        self.assertEqual(len(self.checked), 1)
        for row in (dict(event='waiting', message=SIMULATOR_SILENT),):
            self.app._handle(row)
        self.assertIn('Virtual Flight', self.app.status['text'])
        self.app._handle(dict(event='streaming'))
        self.app._handle(dict(event='stats', fps=99.9, frame_bytes_max=48 * 1024))
        self.assertEqual(self.app.status['text'], 'Streaming')
        self.assertIn('48 KB', self.app.stats['text'])
        self.assertIn('disabled', self.app.display.state())
        self.app.toggle()
        self.assertTrue(engine.stop_event.is_set())
        engine.release.set()
        self.pump(lambda: self.app.state == 'idle')
        self.assertEqual(self.app.start_button['text'], 'Start streaming')
        self.assertNotIn('disabled', self.app.display.state())
        saved = app_config.AppSettings.load(Path(self.folder.name, 'settings.json'))
        self.assertEqual((saved.quality, saved.display_index), ('quality', 1))

    def test_pacing_warning_follows_display(self):
        self.app.display.current(0)
        self.app._changed()
        self.assertIn('144 Hz', self.app.pacing['text'])

    def test_capture_failure_returns_to_idle_with_reason(self):
        self.app.check = lambda s: 'This FFmpeg build has no NVIDIA encoder.'
        self.app.toggle()
        self.pump(lambda: self.app.state == 'idle')
        self.assertEqual(self.app.status['text'], 'Stopped')
        self.assertIn('NVIDIA', self.app.detail['text'])
        self.assertEqual(FakeEngine.instances, [])

    def test_missing_driver_links_to_dji_and_blocks_start(self):
        self.dll.unlink()
        self.app._check_setup()
        self.assertIn('disabled', self.app.start_button.state())
        self.assertEqual(self.app.problem_button['text'], 'Download DJI Assistant 2')
        self.app.problem_button.invoke()
        self.assertEqual(self.opened, [app_config.DJI_ASSISTANT_URL])
        self.app.toggle()
        self.assertEqual(self.app.state, 'idle')
        # Installing the driver and returning to the window enables Start
        self.dll.write_bytes(b'driver')
        self.app._check_setup()
        self.assertNotIn('disabled', self.app.start_button.state())
        self.assertEqual(self.app.status['text'], 'Ready')

    def test_sixty_fps_hides_bitrates_over_the_limit(self):
        self.app.fps.set(60)
        self.app._changed()
        self.assertNotIn('30 Mbps', self.app.bitrate['values'])
        self.assertIn('24 Mbps', self.app.bitrate['values'])


if __name__ == '__main__':
    unittest.main()
