"""Desktop window for G2 Bridge: pick a quality, press Start, and watch the goggles' status.

The window drives the same BridgeEngine as scripts/stream_desktop.py. The engine runs on a
worker thread and reports events through a queue, which the Tk loop drains every 100 ms.
"""
from datetime import datetime
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import webbrowser

import tkinter as tk
from tkinter import ttk

from . import __version__
from .app_config import (APP_NAME, CAPTURES, QUALITY_PRESETS, AppSettings, data_dir, default_dll, find_ffmpeg,
                         max_bitrate, pacing_advice, setup_problems)
from .displays import list_displays
from .engine import BITRATES_MBPS, PRESETS, VBV_FRAMES, BridgeEngine, check_encoder
from .session import SIMULATOR_SILENT

CONNECT_HINT = 'Connect the goggles by USB, power them on, and open More > DJI Virtual Flight.'
COLORS = dict(idle='#555555', busy='#a15c00', good='#1a7f37', bad='#b42318')


class BridgeApp:
    def __init__(self, root, *, settings_path=None, logs_dir=None, dll=None, ffmpeg=None,
                 engine_factory=BridgeEngine, check=check_encoder, displays=list_displays,
                 open_url=webbrowser.open):
        self.root = root
        self.settings_path = Path(settings_path or data_dir() / 'settings.json')
        self.logs_dir = Path(logs_dir or data_dir() / 'logs')
        self.dll = dll if dll is not None else default_dll()
        self.ffmpeg = ffmpeg if ffmpeg is not None else find_ffmpeg()
        self.engine_factory = engine_factory
        self.check = check
        self.list_displays = displays
        self.open_url = open_url
        self.settings = AppSettings.load(self.settings_path)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.state = 'idle'
        self.closing = False
        self.displays = []
        self.problem = None

        root.title(f'{APP_NAME} {__version__}')
        root.resizable(False, False)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._build()
        self._load_into_widgets()
        self._check_setup()
        # Recheck when the window comes back into focus, for example after installing DJI Assistant
        root.bind('<FocusIn>', lambda event: self._check_setup() if event.widget is root else None)
        self.after_id = root.after(100, self._drain)

    # Layout

    def _build(self):
        pad = dict(padx=14, pady=(0, 10))
        frame = ttk.Frame(self.root, padding=(0, 14, 0, 4))
        frame.grid(sticky='nsew')
        self.frame = frame

        status = ttk.Frame(frame)
        status.grid(sticky='ew', **pad)
        self.status = tk.Label(status, text='Ready', font=('Segoe UI', 14, 'bold'), fg=COLORS['idle'], anchor='w')
        self.status.grid(sticky='w')
        self.detail = ttk.Label(status, text=CONNECT_HINT, wraplength=440, justify='left')
        self.detail.grid(sticky='w')

        self.problem_frame = ttk.Frame(frame)
        self.problem_text = tk.Label(self.problem_frame, wraplength=440, justify='left', fg=COLORS['bad'], anchor='w')
        self.problem_text.grid(sticky='w')
        self.problem_button = ttk.Button(self.problem_frame, command=self._open_problem_url)
        self.problem_button.grid(sticky='w', pady=(6, 0))
        # Reserve the row under the status so the banner appears there, not over the next section
        self.problem_frame.grid(row=1, sticky='ew', padx=14, pady=(0, 10))
        self.problem_frame.grid_remove()

        quality = ttk.LabelFrame(frame, text='Picture', padding=10)
        quality.grid(row=2, sticky='ew', **pad)
        self.quality = tk.StringVar()
        for key, preset in QUALITY_PRESETS.items():
            ttk.Radiobutton(quality, text=preset['label'], value=key, variable=self.quality,
                            command=self._changed).grid(sticky='w')
            ttk.Label(quality, text=preset['description'], foreground='#666666').grid(sticky='w', padx=(22, 0))

        screen = ttk.LabelFrame(frame, text='Screen', padding=10)
        screen.grid(sticky='ew', **pad)
        self.display = ttk.Combobox(screen, state='readonly', width=44, postcommand=self._refresh_displays)
        self.display.grid(row=0, column=0, columnspan=3, sticky='w')
        self.display.bind('<<ComboboxSelected>>', lambda _e: self._changed())
        self.fps = tk.IntVar()
        ttk.Label(screen, text='Frame rate').grid(row=1, column=0, sticky='w', pady=(8, 0))
        ttk.Radiobutton(screen, text='100 fps', value=100, variable=self.fps,
                        command=self._changed).grid(row=1, column=1, sticky='w', pady=(8, 0))
        ttk.Radiobutton(screen, text='60 fps', value=60, variable=self.fps,
                        command=self._changed).grid(row=1, column=2, sticky='w', pady=(8, 0))
        self.hide_cursor = tk.BooleanVar()
        ttk.Checkbutton(screen, text='Hide the mouse pointer', variable=self.hide_cursor,
                        command=self._changed).grid(row=2, column=0, columnspan=3, sticky='w', pady=(6, 0))
        self.pacing = ttk.Label(screen, wraplength=420, justify='left', foreground=COLORS['busy'])
        self.pacing.grid(row=3, column=0, columnspan=3, sticky='w', pady=(6, 0))

        self.advanced_shown = tk.BooleanVar(value=False)
        self.advanced_toggle = ttk.Button(frame, text='▸ Advanced settings', command=self._toggle_advanced)
        self.advanced_toggle.grid(sticky='w', padx=14)
        self.advanced = ttk.Frame(frame, padding=(14, 6, 14, 6))
        self.bitrate = ttk.Combobox(self.advanced, state='readonly', width=24)
        self.aq = tk.BooleanVar()
        self.encoder_preset = ttk.Combobox(self.advanced, state='readonly', width=24, values=PRESETS)
        self.vbv = ttk.Combobox(self.advanced, state='readonly', width=24, values=VBV_FRAMES)
        self.capture = ttk.Combobox(self.advanced, state='readonly', width=24, values=CAPTURES)
        for row, (label, widget) in enumerate((('Bitrate', self.bitrate), ('Encoder preset', self.encoder_preset),
                                               ('Buffer (frames)', self.vbv), ('Capture', self.capture))):
            ttk.Label(self.advanced, text=label).grid(row=row, column=0, sticky='w', pady=2)
            widget.grid(row=row, column=1, sticky='w', pady=2, padx=(10, 0))
            widget.bind('<<ComboboxSelected>>', lambda _e: self._changed())
        ttk.Checkbutton(self.advanced, text='Adaptive quantization (sharper detail)', variable=self.aq,
                        command=self._changed).grid(row=4, column=0, columnspan=2, sticky='w', pady=(4, 0))
        self.advanced_row = frame.grid_size()[1]

        buttons = ttk.Frame(frame, padding=(14, 10, 14, 6))
        buttons.grid(row=self.advanced_row + 1, sticky='ew')
        self.start_button = ttk.Button(buttons, text='Start streaming', command=self.toggle, width=18)
        self.start_button.grid(row=0, column=0, sticky='w')
        ttk.Button(buttons, text='Open logs', command=self._open_logs).grid(row=0, column=1, padx=(10, 0))
        self.stats = ttk.Label(frame, text='', foreground='#666666')
        self.stats.grid(row=self.advanced_row + 2, sticky='w', padx=14, pady=(0, 8))
        self.inputs = [w for w in self._descendants(quality) + self._descendants(screen)
                       + self._descendants(self.advanced) if isinstance(w, (ttk.Radiobutton, ttk.Checkbutton,
                                                                            ttk.Combobox))]

    def _descendants(self, widget):
        found = []
        for child in widget.winfo_children():
            found += [child] + self._descendants(child)
        return found

    def _toggle_advanced(self):
        shown = not self.advanced_shown.get()
        self.advanced_shown.set(shown)
        self.advanced_toggle['text'] = ('▾' if shown else '▸') + ' Advanced settings'
        if shown:
            self.advanced.grid(row=self.advanced_row, sticky='ew')
        else:
            self.advanced.grid_remove()

    # Settings

    def _refresh_displays(self):
        try:
            self.displays = self.list_displays()
        except Exception:
            self.displays = []
        self.display['values'] = [d.label() for d in self.displays] or ['Display 1']

    def _bitrate_choices(self):
        return ['Follow picture setting'] + [f'{b} Mbps' for b in BITRATES_MBPS if b <= max_bitrate(self.fps.get())]

    def _load_into_widgets(self):
        s = self.settings
        self._refresh_displays()
        if s.display_index >= len(self.display['values']):
            s.display_index = 0
        self.quality.set(s.quality)
        self.display.current(s.display_index)
        self.fps.set(s.fps)
        self.hide_cursor.set(s.hide_cursor)
        self.aq.set(s.adaptive_quant)
        self.encoder_preset.set(s.encoder_preset)
        self.vbv.set(s.vbv_frames)
        self.capture.set(s.capture)
        self.bitrate['values'] = self._bitrate_choices()
        self.bitrate.set(f'{s.custom_bitrate_mbps} Mbps' if s.custom_bitrate_mbps else 'Follow picture setting')
        self._show_pacing()

    def _changed(self):
        s = self.settings
        s.quality = self.quality.get()
        s.display_index = max(self.display.current(), 0)
        s.fps = self.fps.get()
        s.hide_cursor = self.hide_cursor.get()
        s.adaptive_quant = self.aq.get()
        s.encoder_preset = self.encoder_preset.get()
        s.vbv_frames = int(self.vbv.get())
        s.capture = self.capture.get()
        choice = self.bitrate.get()
        s.custom_bitrate_mbps = int(choice.split()[0]) if choice[:1].isdigit() else 0
        # A custom bitrate above the new frame rate's limit falls back to the preset
        if s.custom_bitrate_mbps > max_bitrate(s.fps):
            s.custom_bitrate_mbps = 0
        self.bitrate['values'] = self._bitrate_choices()
        self.bitrate.set(f'{s.custom_bitrate_mbps} Mbps' if s.custom_bitrate_mbps else 'Follow picture setting')
        self._show_pacing()
        self._check_setup()
        try:
            s.save(self.settings_path)
        except OSError:
            pass

    def _show_pacing(self):
        index = self.settings.display_index
        display = self.displays[index] if index < len(self.displays) else None
        self.pacing['text'] = pacing_advice(display.refresh_hz if display else 0, self.settings.fps) or ''

    def _check_setup(self, _event=None):
        if self.state != 'idle':
            return
        had_problem = self.problem is not None
        problems = setup_problems(self.dll, self.ffmpeg, self.settings.capture)
        self.problem = problems[0] if problems else None
        if self.problem:
            self._set_status('One step before streaming', '', 'bad')
        elif had_problem:
            self._set_status('Ready', CONNECT_HINT, 'idle')
        if self.problem:
            self.problem_text['text'] = self.problem.message
            if self.problem.url:
                self.problem_button['text'] = self.problem.action
                self.problem_button.grid()
            else:
                self.problem_button.grid_remove()
            self.problem_frame.grid()
        else:
            self.problem_frame.grid_remove()
        if self.state == 'idle':
            self.start_button.state(['disabled'] if self.problem else ['!disabled'])

    def _open_problem_url(self):
        if self.problem and self.problem.url:
            self.open_url(self.problem.url)

    def _open_logs(self):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(self.logs_dir)
        else:
            subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(self.logs_dir)])

    # Streaming

    def toggle(self):
        if self.state == 'idle':
            self.start()
        elif self.state in ('checking', 'running'):
            self.stop()

    def start(self):
        self._changed()
        if self.problem:
            return
        try:
            bridge = self.settings.bridge_settings(self.dll, self.ffmpeg)
        except ValueError as exc:
            self._set_status('Settings problem', str(exc), 'bad')
            return
        self.stop_event = threading.Event()
        self.state = 'checking'
        for widget in self.inputs:
            widget.state(['disabled'])
        self.start_button['text'] = 'Stop'
        self.stats['text'] = ''
        self._set_status('Checking screen capture...', 'This takes a second.', 'busy')
        log_path = self.logs_dir / f'{datetime.now():%Y%m%d-%H%M%S}.jsonl'
        self.worker = threading.Thread(target=self._run, args=(bridge, log_path, self.stop_event), daemon=True)
        self.worker.start()

    def _run(self, bridge, log_path, stop_event):
        try:
            problem = self.check(bridge)
            if problem:
                self.events.put(dict(event='failed', message=problem))
                return
            if stop_event.is_set():
                return
            self.events.put(dict(event='checked'))
            engine = self.engine_factory(bridge, log_path, on_event=self.events.put, stop_event=stop_event)
            engine.run()
        except Exception as exc:
            self.events.put(dict(event='failed', message=str(exc)))
        finally:
            self.events.put(dict(event='finished'))

    def stop(self):
        if self.state in ('checking', 'running'):
            self.state = 'stopping'
            self.stop_event.set()
            self.start_button.state(['disabled'])
            self._set_status('Stopping...', '', 'busy')

    def _drain(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.closing and (self.worker is None or not self.worker.is_alive()):
            self.root.destroy()
            return
        self.after_id = self.root.after(100, self._drain)

    def cancel_polling(self):
        """Stop the event poll; tests call this before destroying their own root."""
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None

    def _handle(self, row):
        event = row['event']
        if event == 'checked':
            self.state = 'running'
            self._set_status('Waiting for the goggles', CONNECT_HINT, 'busy')
        elif event == 'waiting':
            message = row['message']
            if message == SIMULATOR_SILENT:
                self._set_status('Open DJI Virtual Flight', 'The goggles are connected but not in More > '
                                 'DJI Virtual Flight. Open it and streaming starts by itself.', 'busy')
            elif 'Waiting for powered goggles' in message:
                self._set_status('Waiting for the goggles', CONNECT_HINT, 'busy')
            else:
                self._set_status('Reconnecting...', message, 'busy')
        elif event == 'connected':
            self._set_status('Goggles connected', 'Starting video...', 'busy')
        elif event == 'ready':
            self._set_status(f"Found {row['model']}", 'Starting video...', 'busy')
        elif event == 'streaming':
            self._set_status('Streaming', 'Look in the goggles. Press Stop when you are done.', 'good')
        elif event == 'stats':
            self.stats['text'] = (f"{row['fps']:.0f} fps   {self.settings.bitrate_mbps()} Mbps   "
                                  f"largest frame {row['frame_bytes_max'] / 1024:.0f} KB")
        elif event == 'session_ended' and self.state == 'running':
            self._set_status('Reconnecting...', CONNECT_HINT, 'busy')
            self.stats['text'] = ''
        elif event == 'oversize_frame':
            self.stats['text'] = (f"Skipped a {row['bytes'] / 1024:.0f} KB frame, too large for the goggles. "
                                  'Choose a lower picture setting.')
        elif event in ('failed', 'error'):
            self._set_status('Stopped', row['message'], 'bad')
            self.state = 'failed'
        elif event == 'finished':
            failed = self.state == 'failed'
            self.state = 'idle'
            self.worker = None
            for widget in self.inputs:
                widget.state(['!disabled'])
            self.start_button['text'] = 'Start streaming'
            self.start_button.state(['!disabled'])
            self._check_setup()
            if not failed:
                self._set_status('Ready', CONNECT_HINT, 'idle')
                self.stats['text'] = ''

    def _set_status(self, title, detail, tone):
        self.status.configure(text=title, fg=COLORS[tone])
        self.detail['text'] = detail

    def close(self):
        self.closing = True
        self.stop()
        if self.worker is None:
            self.root.destroy()


def _enable_dpi_awareness():
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='G2 Bridge', description='Stream the desktop to DJI goggles.')
    parser.add_argument('--start', action='store_true',
                        help='Start streaming as soon as the window opens, for a shortcut that goes straight to the goggles')
    args = parser.parse_args(argv)
    _enable_dpi_awareness()
    root = tk.Tk()
    try:
        root.iconbitmap(str(Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]),
                                 'packaging', 'windows', 'g2bridge.ico')))
    except tk.TclError:
        pass
    app = BridgeApp(root)
    if args.start:
        root.after(200, app.start)
    root.mainloop()


if __name__ == '__main__':
    main()
