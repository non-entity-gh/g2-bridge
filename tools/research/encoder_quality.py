"""Score the bridge's encoder settings offline, without goggles or a display.

Each mode builds the exact command that the bridge uses, with the screen
capture input replaced by a synthetic source, and encodes a fixed number of
frames. The encoded frames are then compared with the regenerated source
to measure SSIM and PSNR, and packet sizes show how keyframes fit the
rate-control buffer. Results are printed as a table and saved as JSON.

Modes are the bridge's own settings:

    python tools/research/encoder_quality.py baseline p7=preset:p7 \\
        all=preset:p7,vbv_frames:8,adaptive_quant:1,bitrate_mbps:40

Scores measure fidelity to the source, not perceived quality on the goggles.
NVENC is required, so this is Windows only.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from goggles_bridge.engine import BridgeSettings, encoder_command

SCENES = {
    # Moving test pattern with a flat gray block, like the published quality evidence
    'motion': 'testsrc2=size=1920x1080:rate=100,drawbox=x=460:y=240:w=1000:h=600:color=gray:t=fill',
    # Continuously changing fine detail, which stresses the bitrate like foliage in a game
    'detail': 'mandelbrot=size=1920x1080:rate=100:maxiter=200',
}
BOOLEANS = {'adaptive_quant', 'low_latency', 'show_cursor'}


def parse_mode(text):
    name, _, spec = text.partition('=')
    overrides = {}
    for item in filter(None, spec.split(',')):
        key, _, value = item.partition(':')
        if key in BOOLEANS:
            overrides[key] = value in ('1', 'true', 'yes')
        elif key in ('preset', 'capture', 'h264_profile'):
            overrides[key] = value
        else:
            overrides[key] = int(value)
    return name, overrides


def encode(ffmpeg, settings, scene, frames, output):
    command = encoder_command(settings)
    source = command.index('-i') + 1
    command[source] = SCENES[scene]
    assert command[-1] == 'pipe:1' and command[-2] == 'h264'
    command[-1] = str(output)
    command[command.index('-an'):command.index('-an')] = ['-frames:v', str(frames)]
    started = time.perf_counter()
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'Encode failed:\n{result.stderr[-2000:]}')
    return command, frames / (time.perf_counter() - started)


def measure(ffmpeg, scene, frames, encoded, workdir):
    ssim_log, psnr_log = workdir / 'ssim.log', workdir / 'psnr.log'
    graph = (f'[0:v]format=yuv420p,split[a1][a2];[1:v]format=yuv420p,split[b1][b2];'
             f'[a1][b1]ssim=f={ssim_log.name};[a2][b2]psnr=f={psnr_log.name}')
    command = [str(ffmpeg), '-hide_banner', '-nostdin', '-framerate', '100', '-i', str(encoded),
               '-f', 'lavfi', '-i', SCENES[scene], '-frames:v', str(frames), '-lavfi', graph, '-f', 'null', '-']
    result = subprocess.run(command, capture_output=True, text=True, cwd=workdir)
    if result.returncode:
        raise RuntimeError(f'Measurement failed:\n{result.stderr[-2000:]}')
    ssim = [float(m.group(1)) for m in re.finditer(r'All:([0-9.]+)', ssim_log.read_text())]
    psnr = [float(m.group(1)) for m in re.finditer(r'psnr_avg:([0-9.]+|inf)', psnr_log.read_text())]
    probe = subprocess.run([str(Path(ffmpeg).with_name('ffprobe' + Path(ffmpeg).suffix)), '-v', 'error',
                            '-show_entries', 'packet=size,flags', '-of', 'json', str(encoded)],
                           capture_output=True, text=True, check=True)
    packets = json.loads(probe.stdout)['packets']
    sizes = [int(p['size']) for p in packets]
    idr = [i for i, p in enumerate(packets) if 'K' in p['flags']]
    return dict(frames=len(sizes), ssim_mean=round(statistics.fmean(ssim), 6), ssim_min=min(ssim),
                ssim_p05=sorted(ssim)[len(ssim) // 20],
                ssim_at_keyframe=round(statistics.fmean(ssim[i] for i in idr if i < len(ssim)), 6),
                psnr_mean=round(statistics.fmean(psnr), 3), bytes_total=sum(sizes),
                bytes_median=statistics.median(sizes), bytes_p95=sorted(sizes)[int((len(sizes) - 1) * .95)],
                bytes_max=max(sizes), idr_indices=idr, frame_bytes=sizes, ssim=ssim, psnr=psnr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('modes', nargs='+', help='name or name=key:value,key:value using BridgeSettings fields')
    parser.add_argument('--ffmpeg', type=Path, default=shutil.which('ffmpeg'))
    parser.add_argument('--frames', type=int, default=600)
    parser.add_argument('--scenes', default=','.join(SCENES), help=f'Comma-separated subset of {", ".join(SCENES)}')
    parser.add_argument('--output', type=Path, help='JSON result path (default: work/quality/<time>.json)')
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error('FFmpeg not found; pass --ffmpeg')
    output = args.output or ROOT / 'work' / 'quality' / f'{datetime.now():%Y%m%d-%H%M%S}.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    workdir = output.with_suffix('')
    workdir.mkdir(exist_ok=True)
    results = []
    for mode in args.modes:
        name, overrides = parse_mode(mode)
        settings = BridgeSettings(Path('unused.dll'), args.ffmpeg, low_latency=False, **overrides)
        for scene in args.scenes.split(','):
            encoded = workdir / f'{name}-{scene}.h264'
            command, encode_fps = encode(args.ffmpeg, settings, scene, args.frames, encoded)
            row = dict(mode=name, scene=scene, overrides=overrides, offline_encode_fps=round(encode_fps, 1),
                       command=command, **measure(args.ffmpeg, scene, args.frames, encoded, workdir))
            results.append(row)
            print(f"{name:>10} {scene:>7}  SSIM {row['ssim_mean']:.6f} (min {row['ssim_min']:.6f}, "
                  f"keyframe {row['ssim_at_keyframe']:.6f})  PSNR {row['psnr_mean']:.2f} dB  "
                  f"bytes median {row['bytes_median']:>7.0f} max {row['bytes_max']:>7}  "
                  f"encode {row['offline_encode_fps']:.0f} fps", flush=True)
    output.write_text(json.dumps(dict(frames=args.frames, scenes=SCENES, results=results), indent=1))
    print(f'Saved {output}')


if __name__ == '__main__':
    main()
