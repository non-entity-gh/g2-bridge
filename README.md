# G2 Bridge

Stream your Windows or Mac desktop to DJI Goggles 2 or DJI Goggles Integra over USB. The goggles use Virtual Flight mode with stock firmware. The simulator and controller run on the computer.

The bridge detects the model when it connects:

| Goggles | Identity | Tested firmware |
| --- | --- | --- |
| DJI Goggles 2 | `E3T ZV900` | 01.11.00.00, 01.14.00.00 |
| DJI Goggles Integra | `E3T ZV901` | 01.09.00.00 |

We measured **about 12 ms median monitor-to-lens delay** at 1080p100. See [how we measured it](docs/LATENCY.md).

## Requirements

On Windows, you need the following hardware and software:

- 64-bit Windows and Python 3.10 or later.
- An NVIDIA GPU with NVENC support.
- FFmpeg with `gfxcapture` and `h264_nvenc`. To install it, run `winget install Gyan.FFmpeg`.
- [DJI Assistant 2 (Consumer Drones Series)](https://www.dji.com/downloads/softwares/dji-assistant-2-consumer-drones-series), which installs the DJI USB driver 5.0.0.7. See [USB and driver requirements](docs/PROTOCOL.md#usb-and-session).

Tests used FFmpeg build `2026-11-05-git-0893feba65` and NVIDIA driver 610.74.

On macOS, you need the following:

- macOS and Python 3.10 or later.
- Apple's command-line developer tools, which build the screen capture helper on the first run.
- [Homebrew](https://brew.sh), for libusb. Before macOS 12.3, the bridge also needs FFmpeg.
- A USB-A-to-USB-C cable. If the Mac has only USB-C ports, add a USB-C-to-USB-A adapter. Over a USB-C-to-USB-C cable, the goggles take the USB host role and the Mac can't detect them.

The stream script offers to install whatever is missing. Tests used an M2 Pro MacBook Pro with macOS 27.0, Swift 6.4.2, and Homebrew FFmpeg 8.1.1.

## Start streaming

1. Power on the goggles, connect a USB data cable, and open **More > DJI Virtual Flight**. The goggles answer the bridge outside Virtual Flight but ignore its request to start video, so the bridge waits until this screen is open. On a Mac, when macOS asks whether the accessory can connect, select **Allow**.
2. From the repository root, install the Python dependencies.

   On Windows, run:

   ```powershell
   python -m pip install -r requirements.txt
   ```

   On macOS, run:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

3. Start the stream.

   On Windows, run:

   ```powershell
   python scripts/stream_desktop.py
   ```

   On macOS, run:

   ```bash
   .venv/bin/python scripts/stream_desktop.py
   ```

   On Windows, the script finds the DJI driver and FFmpeg automatically. If either is installed somewhere else, pass `--dll` or `--ffmpeg` with the file path.

   On a Mac, the script offers to install anything that's missing, and it builds the screen capture helper on the first run. If the screen can't be captured, go to **System Settings > Privacy & Security** and allow screen recording for your terminal.

The default output is **1920x1080**, with a **100 fps encoder target** and a **20 Mbps** bitrate. Press **Ctrl+C** to stop. Logs are written to `work/logs/`.

Use these options to adjust the stream:

| Option | Values |
| --- | --- |
| `--fps` | `60`, `100` |
| `--bitrate-mbps` | `8` to `40`; at `60` fps, at most `24`. See [picture quality](#picture-quality-and-frame-pacing) |
| `--vbv-frames` | Encoder rate-control buffer in frames: `2`, `4`, `8`, `16`. The bridge caps it so keyframes stay under the goggles' frame limit |
| `--no-aq` | Turn off NVENC adaptive quantization (Windows) |
| `--preset` | NVENC preset `p1`, `p4`, `p5`, `p6`, `p7` (Windows). `p7` measured no gain over `p4` |
| `--display` | Monitor index, starting at `0` |
| `--hide-cursor` | Hide the mouse pointer |
| `--capture` | Windows: `gfxcapture` or `ddagrab`. macOS: `screencapturekit` or `avfoundation` |

Run `python scripts/stream_desktop.py --help` for all options.

## Picture quality and frame pacing

The default 20 Mbps keeps the measured latency. On a desktop GPU, `--bitrate-mbps 40` roughly halves the visible compression; it adds about 1 ms of USB time per frame. Adaptive quantization is on by default and sharpens detailed scenes at no cost.

The goggles accept at most about 64 KB per encoded frame. A larger frame is dropped, and one arriving mid-stream can freeze the picture or leave the goggles on a black screen that only a power cycle clears. The bridge caps the encoder buffer so keyframes stay under 56 KB, refuses bitrate and frame-rate pairs whose ordinary frames would be too large, and skips any frame over the limit with a message. Each status line shows the largest frame sent in the last five seconds. See [the measurements](docs/research/EXPERIMENTS.md#frame-size-limit-and-bitrate).

The goggles run at 100 Hz. If the monitor runs at another rate, the stream repeats or skips frames unevenly. For a steady picture, run the monitor at 100 Hz, or in a game cap the frame rate at 100 fps with variable refresh rate enabled.

## Run tests

Run the tests from the repository root:

```powershell
python -m unittest discover -s tests -v
```

See the [video-delay measurement guide](docs/LATENCY.md), [PC protocol](docs/PROTOCOL.md), and [reverse-engineering data and tools](docs/research/README.md).

## Contributing

See the [contribution guide](CONTRIBUTING.md) to report bugs, submit changes, or share protocol research.

## License

Copyright (C) 2026 G2 Bridge contributors.

The original code and documentation are licensed under the [GNU General Public License v3.0 only](LICENSE) (`GPL-3.0-only`). Third-party materials retain their own licenses.
