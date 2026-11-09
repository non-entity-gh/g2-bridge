import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from goggles_bridge import app_config
from goggles_bridge.app_config import (QUALITY_PRESETS, AppSettings, find_ffmpeg, max_bitrate, pacing_advice,
                                       setup_problems)
from goggles_bridge.displays import Display
from goggles_bridge.engine import BridgeSettings


class AppSettingsTests(unittest.TestCase):
    def test_presets_map_to_valid_bridge_settings(self):
        for key, preset in QUALITY_PRESETS.items():
            with self.subTest(key):
                settings = AppSettings(quality=key).bridge_settings(Path('d.dll'), Path('ffmpeg.exe'))
                self.assertIsInstance(settings, BridgeSettings)
                self.assertEqual(settings.bitrate_mbps, preset['bitrate_mbps'])
                self.assertTrue(settings.adaptive_quant)
                self.assertEqual((settings.fps, settings.preset, settings.vbv_frames), (100, 'p4', 2))

    def test_sixty_fps_clamps_to_the_frame_limit(self):
        self.assertEqual(max_bitrate(100), 40)
        self.assertEqual(max_bitrate(60), 24)
        settings = AppSettings(quality='quality', fps=60)
        self.assertEqual(settings.bitrate_mbps(), 24)
        settings.bridge_settings(Path('d.dll'), Path('ffmpeg.exe'))

    def test_custom_bitrate_overrides_preset(self):
        settings = AppSettings(quality='quality', custom_bitrate_mbps=16, hide_cursor=True, display_index=1)
        bridge = settings.bridge_settings(Path('d.dll'), Path('ffmpeg.exe'))
        self.assertEqual((bridge.bitrate_mbps, bridge.show_cursor, bridge.display_index), (16, False, 1))

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'nested', 'settings.json')
            original = AppSettings(quality='balanced', display_index=2, fps=60, hide_cursor=True,
                                   custom_bitrate_mbps=12, adaptive_quant=False, encoder_preset='p1', vbv_frames=4)
            original.save(path)
            self.assertEqual(AppSettings.load(path), original)
            self.assertFalse(path.with_suffix('.tmp').exists())

    def test_load_ignores_bad_or_missing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'settings.json')
            self.assertEqual(AppSettings.load(path), AppSettings())
            path.write_text('not json')
            self.assertEqual(AppSettings.load(path), AppSettings())
            path.write_text('[1, 2]')
            self.assertEqual(AppSettings.load(path), AppSettings())
            path.write_text(json.dumps(dict(quality='ultra', fps=144, display_index=-1, hide_cursor='yes',
                                            custom_bitrate_mbps=50, encoder_preset='p4', vbv_frames=8,
                                            unknown=True)))
            loaded = AppSettings.load(path)
            self.assertEqual(loaded, AppSettings(encoder_preset='p4', vbv_frames=8))


class EnvironmentTests(unittest.TestCase):
    def test_bundled_ffmpeg_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = 'ffmpeg.exe' if app_config.WINDOWS else 'ffmpeg'
            bundled = Path(folder, 'ffmpeg', exe)
            bundled.parent.mkdir()
            bundled.write_bytes(b'')
            self.assertEqual(find_ffmpeg(environ=dict(GOGGLES_FFMPEG='x'), which=lambda *a, **k: 'y', base=folder),
                             bundled)
            bundled.unlink()
            self.assertEqual(find_ffmpeg(environ=dict(GOGGLES_FFMPEG='x'), which=lambda *a, **k: 'y', base=folder),
                             Path('x'))
            self.assertEqual(find_ffmpeg(environ={}, which=lambda *a, **k: 'y', base=folder), Path('y'))
            self.assertIsNone(find_ffmpeg(environ={}, which=lambda *a, **k: None, base=folder))

    def test_setup_problems_explain_and_link(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(app_config, 'WINDOWS', True):
            ffmpeg = Path(folder, 'ffmpeg.exe')
            ffmpeg.write_bytes(b'')
            missing, = setup_problems(Path(folder, 'none.dll'), ffmpeg, 'gfxcapture')
            self.assertIn('DJI Assistant 2', missing.message)
            self.assertEqual(missing.url, app_config.DJI_ASSISTANT_URL)
            wrong = Path(folder, 'other.dll')
            wrong.write_bytes(b'not the DJI build')
            problem, = setup_problems(wrong, ffmpeg, 'gfxcapture')
            self.assertIn('different version', problem.message)
            self.assertEqual(problem.url, app_config.DJI_ASSISTANT_URL)
            with mock.patch.object(app_config, 'VERIFIED_DLL_SHA256',
                                   '6c8be2a1f0c0bc6f45d3c6b0f6a5d5d1c1d2e3f40000000000000000000000'):
                pass
            problems = setup_problems(wrong, None, 'gfxcapture')
            self.assertEqual(len(problems), 2)
            self.assertIsNone(problems[1].url)

    def test_setup_accepts_the_verified_driver(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(app_config, 'WINDOWS', True):
            dll, ffmpeg = Path(folder, 'ok.dll'), Path(folder, 'ffmpeg.exe')
            dll.write_bytes(b'driver')
            ffmpeg.write_bytes(b'')
            import hashlib
            with mock.patch.object(app_config, 'VERIFIED_DLL_SHA256', hashlib.sha256(b'driver').hexdigest()):
                self.assertEqual(setup_problems(dll, ffmpeg, 'gfxcapture'), [])

    def test_pacing_advice_only_for_uneven_rates(self):
        self.assertIsNone(pacing_advice(100, 100))
        self.assertIsNone(pacing_advice(200, 100))
        self.assertIsNone(pacing_advice(120, 60))
        self.assertIsNone(pacing_advice(0, 100))
        self.assertIn('144 Hz', pacing_advice(144, 100))
        self.assertIn('G-SYNC', pacing_advice(165, 100))

    def test_display_labels(self):
        self.assertEqual(Display(0, 3840, 2160, 144, True).label(), 'Display 1: 3840x2160 at 144 Hz (main)')
        self.assertEqual(Display(1).label(), 'Display 2')


if __name__ == '__main__':
    unittest.main()
