"""Probe an untested goggles model over the PC USB route, one step at a time.

Each step runs only when requested, and every USB exchange goes to a JSON Lines log:

1. Descriptors (always): list DJI USB devices and check MI04 against the
   tested Goggles 2 control interface. Reads descriptors only.
2. Identity (default): claim MI04, exchange heartbeats, and read the identity
   reply. These are the same read-only requests DJI Assistant sends. Then read
   the firmware manifest (00/4f to 28/5) to record the firmware and module
   versions. That read is also read-only; --no-firmware skips it.
3. Simulator mode (--simulator): send 0b/1a and record the goggles' status.
4. Test video (--test-video, needs --simulator): after a success reply, send the
   synthetic 1920x1080 test picture at 30 fps until the run ends.

Two follow-up checks run after identity, without the regular simulator step:

- Route scan (--scan): send the identity request to 28/0-28/7, then simulator
  mode to each module that answered, except CPLD 28/3 and upgrade 28/5. Every
  reply with a matching sequence is recorded, whatever its sender.
- Forced video (--force-video): send the test picture without a simulator
  acknowledgement. Video packets request no acknowledgement either.

Close DJI Assistant and any other bridge before running it.
"""
import argparse
from datetime import datetime
import glob
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from goggles_bridge.control import V1Packet, V1Stream
from goggles_bridge.firmware import ManifestReader
from goggles_bridge.pc_video import PcVideoPacketizer
from goggles_bridge.session import ControlSession, SessionError, MODELS, EXPERIMENTAL_MODELS
from goggles_bridge.usb_transport import DEFAULT_DLL, _select_control_device

TEST_FRAME = ROOT / 'docs/research/evidence/synthetic-frame.h264'
TEST_FRAME_SHA256 = '8b2e9ea7d0189631306e1659227abd94034ea96148f26ec8d2855962992d6735'
TEST_VIDEO_FPS = 30
SCAN_WAIT = .6
SCAN_SKIP_SIMULATOR = {3, 5}  # CPLD and upgrade modules
# libusb speed codes
SPEEDS = {1: 'low', 2: 'full', 3: 'high', 4: 'super', 5: 'super-plus'}


def _descriptor_parser():
    spec = importlib.util.spec_from_file_location('usb_descriptors', ROOT / 'scripts/read_usb_descriptors.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_configuration


def sysfs_configuration(descriptors):
    """Return the first configuration descriptor from a Linux sysfs descriptors file."""
    if len(descriptors) < 18 + 9 or descriptors[:2] != b'\x12\x01':
        raise ValueError('Unexpected sysfs descriptor layout')
    total = int.from_bytes(descriptors[18+2:18+4], 'little')
    config = descriptors[18:18+total]
    if len(config) != total:
        raise ValueError('Truncated sysfs configuration descriptor')
    return config


def read_configuration(device):
    """Read raw configuration bytes; Linux sysfs needs no device permissions."""
    if sys.platform.startswith('linux'):
        for path in glob.glob('/sys/bus/usb/devices/*/'):
            try:
                bus = int(Path(path, 'busnum').read_text())
                address = int(Path(path, 'devnum').read_text())
            except (OSError, ValueError):
                continue
            if (bus, address) == (device.bus, device.address):
                return sysfs_configuration(Path(path, 'descriptors').read_bytes()), 'sysfs'
    header = bytes(device.ctrl_transfer(0x80, 6, 0x0200, 0, 9))
    total = int.from_bytes(header[2:4], 'little')
    return bytes(device.ctrl_transfer(0x80, 6, 0x0200, 0, total)), 'control-transfer'


def describe_packet(packet):
    return dict(command=f'{packet.command_set:02x}/{packet.command_id:02x}',
                sender=f'{packet.sender_type}/{packet.sender_id}',
                receiver=f'{packet.receiver_type}/{packet.receiver_id}',
                sequence=packet.sequence, response=packet.is_response, payload=packet.payload.hex())


class Log:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open('x', encoding='utf-8')
        self.started = time.monotonic()

    def __call__(self, event, **fields):
        row = dict(seconds=round(time.monotonic()-self.started, 6), event=event, **fields)
        self.stream.write(json.dumps(row) + '\n')
        self.stream.flush()
        return row

    def close(self):
        self.stream.close()


def probe_descriptors(log, backend, allow_superspeed):
    import usb.core
    parse_configuration = _descriptor_parser()
    devices = list(usb.core.find(find_all=True, backend=backend, idVendor=0x2ca3))
    print(f'Step 1: found {len(devices)} DJI USB device(s)')
    if not devices:
        print('  Power the goggles, connect a USB data cable, and check that DJI Assistant is closed')
    for device in devices:
        row = dict(vendor_id='2ca3', product_id=f'{device.idProduct:04x}', bcd_usb=f'{device.bcdUSB:04x}',
                   speed=SPEEDS.get(getattr(device, 'speed', None)), bus=device.bus, address=device.address)
        try:
            raw, source = read_configuration(device)
            row.update(configuration_hex=raw.hex(), descriptor_source=source,
                       configuration=parse_configuration(raw))
        except (OSError, ValueError, usb.core.USBError) as exc:
            row['configuration_error'] = str(exc)
        try:
            _select_control_device([device], allow_superspeed=allow_superspeed)
            row['control_interface'] = 'matches the tested MI04 profile'
        except Exception as exc:
            row['control_interface'] = f'{type(exc).__name__}: {exc}'
        mi04 = [i for i in row.get('configuration', {}).get('interfaces', []) if i['number'] == 4]
        log('device', **row)
        print(f"  {row['vendor_id']}:{row['product_id']} USB {row['bcd_usb']} speed={row['speed']}")
        for interface in mi04:
            endpoints = ', '.join(f"{e['address']}/{e['max_packet_size']}" for e in interface['endpoints'])
            print(f"  MI04 class {interface['usb_class']:02x}/{interface['subclass']:02x}/"
                  f"{interface['protocol']:02x}, endpoints {endpoints}")
        print(f"  Control interface: {row['control_interface']}")
    return devices


class RouteScan:
    """Send one request at a time and record any reply that carries its sequence."""
    def __init__(self, log):
        self.log = log
        self.queue = [('identity', receiver_id) for receiver_id in range(8)]
        self.current = None
        self.results = []
        self.simulator_queued = False

    @property
    def done(self):
        return not self.queue and self.current is None and self.simulator_queued

    def receive(self, packet):
        if self.current and packet.is_response and packet.sequence == self.current['sequence']:
            self.current['replies'].append(describe_packet(packet))

    def poll(self, now, next_sequence, send):
        if self.current and now - self.current['sent_at'] >= SCAN_WAIT:
            row = self.current
            self.current = None
            del row['sent_at']
            self.results.append(row)
            self.log('scan', **row)
            replies = ', '.join(f"{r['sender']} status {r['payload'][:2] or 'none'}" for r in row['replies'])
            print(f"  {row['request']} -> 28/{row['receiver_id']}: {replies or 'no reply'}", flush=True)
        if self.current or self.done:
            return
        if not self.queue:
            answered = sorted({row['receiver_id'] for row in self.results
                               if row['request'] == 'identity' and row['replies']})
            self.queue = [('simulator', i) for i in answered if i not in SCAN_SKIP_SIMULATOR]
            self.simulator_queued = True
            if not self.queue:
                return
        request, receiver_id = self.queue.pop(0)
        command, payload = ((0, 0xff), b'\0') if request == 'identity' else ((0x0b, 0x1a), bytes.fromhex('01010000'))
        sequence = next_sequence()
        send(V1Packet(*command, 10, 1, 0x1c, receiver_id, 0, 2, sequence, payload))
        self.current = dict(request=request, receiver_id=receiver_id, sequence=sequence, sent_at=now, replies=[])


def report_firmware(log, reader):
    """Log and print the manifest result once, returning the summary for the result row."""
    if reader.error:
        summary = dict(error=reader.error, bytes=len(reader.data))
        print(f'  Firmware manifest: {reader.error}', flush=True)
    else:
        summary = dict(reader.manifest.as_dict(), bytes=len(reader.data), chunks=reader.chunks)
        print(f"  Firmware: {summary['firmware']} (device {summary['device']})", flush=True)
        for module in summary['modules']:
            print(f"    module {module['id']} {module['name']}: {module['version']} ({module['file']})", flush=True)
    log('firmware', **summary, container_hex=bytes(reader.data).hex())
    return summary


def probe_session(log, args):
    from goggles_bridge.usb_transport import ControlUsb
    session = ControlSession(models={**MODELS, **EXPERIMENTAL_MODELS}, enter_simulator=args.simulator)
    target = 'ready' if args.simulator else 'identified'
    decoder = V1Stream()
    packetizer = PcVideoPacketizer()
    frame = TEST_FRAME.read_bytes() if args.test_video or args.force_video else None
    scan = RouteScan(log) if args.scan else None
    firmware = None if args.no_firmware else ManifestReader()
    if frame is not None and hashlib.sha256(frame).hexdigest() != TEST_FRAME_SHA256:
        raise RuntimeError(f'Unexpected test frame hash: {TEST_FRAME}')
    result = dict(state=None, identity=None, model=None, firmware=None, frames_sent=0, error=None)
    previous = None
    next_frame = None
    started = time.monotonic()
    with ControlUsb(args.dll, allow_superspeed=args.allow_superspeed) as usb:
        log('claimed', interface=4)

        def send(packet):
            data = packet.serialize()
            usb.write(data)
            log('send', hex=data.hex(), **describe_packet(packet))

        try:
            while time.monotonic()-started < args.seconds:
                for request in session.poll(time.monotonic()):
                    send(request)
                data = usb.read(timeout_ms=5 if next_frame is not None else 50)
                if data:
                    log('receive', hex=data.hex())
                for packet in decoder.feed(data):
                    log('packet', **describe_packet(packet))
                    if scan:
                        scan.receive(packet)
                    if firmware:
                        firmware.receive(packet)
                    for request in session.receive(packet, time.monotonic()):
                        send(request)
                if session.state != previous:
                    previous = session.state
                    log('state', value=session.state, identity=session.identity, model=session.model)
                    print(f'  Control state: {session.state}', flush=True)
                    if session.state == 'identity':
                        print('Step 2: heartbeat acknowledged, reading identity')
                    elif session.state in ('simulator', 'identified'):
                        print(f'  Identity: {session.identity} ({session.model})')
                        if session.state == 'simulator':
                            print('Step 3: sending simulator mode 0b/1a')
                    elif session.state == 'ready':
                        print('  Simulator mode acknowledged with status 00')
                        if frame is not None:
                            print(f'Step 4: sending the test picture at {TEST_VIDEO_FPS} fps; watch the lenses')
                            next_frame = time.monotonic()
                if firmware and session.state in ('identified', 'simulator', 'ready'):
                    if not firmware.done:
                        for request in firmware.poll(time.monotonic(), session.reserve_sequence):
                            send(request)
                    if firmware.done and result['firmware'] is None:
                        result['firmware'] = report_firmware(log, firmware)
                if session.state == 'identified' and scan and not scan.done:
                    if not scan.results and scan.current is None:
                        print('Scan: one request at a time, waiting 0.6 s for replies')
                    scan.poll(time.monotonic(), session.reserve_sequence, send)
                if (args.force_video and session.state == 'identified' and next_frame is None
                        and (scan is None or scan.done)):
                    print(f'Forced video: sending the test picture at {TEST_VIDEO_FPS} fps without '
                          'simulator mode; watch the lenses', flush=True)
                    next_frame = time.monotonic()
                if next_frame is not None and time.monotonic() >= next_frame:
                    wire = b''.join(p.serialize() for p in packetizer.packetize(frame, session.reserve_sequence))
                    usb.write(wire)
                    result['frames_sent'] += 1
                    if result['frames_sent'] % TEST_VIDEO_FPS == 1:
                        log('video', frames=result['frames_sent'], bytes=len(wire))
                    next_frame += 1/TEST_VIDEO_FPS
        except SessionError as exc:
            result['error'] = str(exc)
            print(f'  Session ended: {exc}')
        result.update(state=session.state, identity=session.identity, model=session.model,
                      reached=session.state == target and (scan is None or scan.done),
                      discarded_bytes=decoder.discarded)
        if scan:
            result['scan'] = scan.results
    return result


def frame_flag(args):
    return args.test_video or args.force_video


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, help='New JSON Lines log path (default: work/probe/<time>.jsonl)')
    parser.add_argument('--dll', type=Path, default=DEFAULT_DLL, help='Windows only: verified DJI x64 libusb0_device.dll')
    parser.add_argument('--descriptors-only', action='store_true', help='Stop after step 1')
    parser.add_argument('--simulator', action='store_true', help='Step 3: send simulator mode 0b/1a after identity')
    parser.add_argument('--test-video', action='store_true', help='Step 4: send the test picture after simulator mode')
    parser.add_argument('--scan', action='store_true', help='Send identity and simulator requests to 28/0-28/7')
    parser.add_argument('--force-video', action='store_true', help='Send the test picture without simulator mode')
    parser.add_argument('--no-firmware', action='store_true', help='Skip the read-only firmware manifest read')
    parser.add_argument('--allow-superspeed', action='store_true', help='Claim MI04 on an untested SuperSpeed link')
    parser.add_argument('--seconds', type=float, default=15, help='Session length in seconds, from 5 to 300')
    args = parser.parse_args()
    if args.test_video and not args.simulator:
        parser.error('--test-video requires --simulator')
    if args.simulator and (args.scan or args.force_video):
        parser.error('--scan and --force-video replace --simulator; run them without it')
    if not 5 <= args.seconds <= 300:
        parser.error('--seconds must be between 5 and 300')
    output = args.output or ROOT / 'work' / 'probe' / f'{datetime.now():%Y%m%d-%H%M%S}.jsonl'
    if output.exists():
        parser.error(f'Output already exists: {output}')
    from goggles_bridge.usb_transport import get_backend
    log = Log(output)
    exit_code = 1
    try:
        log('starting', platform=sys.platform, args={k: str(v) for k, v in vars(args).items()})
        devices = probe_descriptors(log, get_backend(args.dll), args.allow_superspeed)
        if devices and args.descriptors_only:
            exit_code = 0
        elif devices:
            result = probe_session(log, args)
            log('result', **result)
            print(f"Result: state={result['state']} identity={result['identity']!r}"
                  + (f" frames_sent={result['frames_sent']}" if frame_flag(args) else ''))
            exit_code = 0 if result['reached'] else 1
    except Exception as exc:
        log('error', message=f'{type(exc).__name__}: {exc}')
        print(f'Probe stopped: {exc}', file=sys.stderr)
    finally:
        log('stopped')
        log.close()
        print(f'Log: {output}')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
