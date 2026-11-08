"""Read the goggles firmware manifest over the PC USB route.

The request is command 00/4f (Get Cfg File) to the upgrade module 28/5, as
documented in docs/research/DEVICE_DIAGNOSTICS.md. It is read-only: it returns
the signed IM*H manifest container, whose payload is unencrypted XML naming the
firmware version and module versions. No firmware image is transferred.
"""
from dataclasses import dataclass, field
import re
import struct
from .control import V1Packet

MANIFEST_RECEIVER = (28, 5)
CHUNK_REQUEST = 1000
IMAH_HEADER = 224
IMAH_SIGNATURE = 384
MAX_MANIFEST = 65536


def manifest_request(sequence, offset, *, sender=(10, 1), length=CHUNK_REQUEST):
    payload = b'\x01' + struct.pack('<II', offset, length)
    return V1Packet(0x00, 0x4f, sender[0], sender[1], *MANIFEST_RECEIVER, 0, 2, sequence, payload)


def parse_chunk(payload):
    """Return (data, remaining) from a 00/4f reply payload, or raise ValueError."""
    if len(payload) < 9:
        raise ValueError(f'Short manifest reply: {payload.hex()}')
    if payload[0] != 0:
        raise ValueError(f'Manifest request rejected with status {payload[0]:02x}')
    returned, remaining = struct.unpack_from('<II', payload, 1)
    data = payload[9:9+returned]
    if len(data) != returned:
        raise ValueError(f'Manifest reply announced {returned} bytes but carried {len(data)}')
    return data, remaining


@dataclass
class Manifest:
    device: str = None
    firmware: str = None
    modules: list = field(default_factory=list)
    xml: str = ''

    def as_dict(self):
        return dict(device=self.device, firmware=self.firmware, modules=self.modules)


def parse_manifest(container):
    """Extract versions from an IM*H manifest container.

    Parsing is deliberately tolerant: a newer firmware may reorder attributes,
    so each field is matched on its own instead of assuming one XML layout.
    """
    if container[:4] != b'IM*H':
        raise ValueError(f'Not an IM*H container (starts {container[:4].hex()})')
    xml = container[IMAH_HEADER+IMAH_SIGNATURE:].rstrip(b'\0').decode('utf-8', errors='replace')
    if '<' not in xml:
        raise ValueError('Manifest payload is not plain XML; it may be encrypted on this firmware')
    manifest = Manifest(xml=xml)
    device = re.search(r'<device\b[^>]*\bid="([^"]+)"', xml)
    firmware = re.search(r'<firmware\b[^>]*\bformal="([^"]+)"', xml)
    if firmware is None:
        firmware = re.search(r'\b(\d{2}\.\d{2}\.\d{4})\b', xml)
    manifest.device = device and device.group(1)
    manifest.firmware = firmware and firmware.group(1)
    for tag in re.finditer(r'<module\b([^>]*)>([^<]*)<', xml):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag.group(1)))
        manifest.modules.append(dict(id=attrs.get('id'), version=attrs.get('version'),
                                     name=attrs.get('name'), file=tag.group(2).strip() or None))
    return manifest


class ManifestReader:
    """Drive chunked 00/4f reads alongside a ControlSession.

    The caller supplies sequence numbers from the shared allocator, sends what
    poll() returns, and passes every parsed reply to receive().
    """
    def __init__(self, *, sender=(10, 1), timeout=.6, attempts=3):
        self.sender = sender
        self.timeout = timeout
        self.attempts = attempts
        self.data = bytearray()
        self.pending = None
        self.manifest = None
        self.error = None
        self.chunks = 0

    @property
    def done(self):
        return self.manifest is not None or self.error is not None

    def poll(self, now, next_sequence):
        if self.done:
            return []
        if self.pending:
            packet, sent_at, tries = self.pending
            if now - sent_at < self.timeout:
                return []
            if tries >= self.attempts:
                self.error = f'No manifest reply at offset {len(self.data)} after {tries} attempts'
                self.pending = None
                return []
            self.pending = (packet, now, tries + 1)
            return [packet]
        packet = manifest_request(next_sequence(), len(self.data), sender=self.sender)
        self.pending = (packet, now, 1)
        return [packet]

    def receive(self, reply):
        if self.done or not self.pending or not reply.is_response:
            return False
        request = self.pending[0]
        if (reply.sequence, reply.command_set, reply.command_id) != (request.sequence, 0x00, 0x4f):
            return False
        self.pending = None
        try:
            data, remaining = parse_chunk(reply.payload)
            if not data and remaining:
                raise ValueError('Manifest reply returned no data but more remains')
            self.data.extend(data)
            self.chunks += 1
            if len(self.data) > MAX_MANIFEST:
                raise ValueError(f'Manifest exceeded {MAX_MANIFEST} bytes')
            if remaining == 0:
                self.manifest = parse_manifest(bytes(self.data))
        except ValueError as exc:
            self.error = str(exc)
        return True
