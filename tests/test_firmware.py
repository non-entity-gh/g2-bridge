import struct
import unittest
from dataclasses import replace

from goggles_bridge.control import V1Packet
from goggles_bridge.firmware import (ManifestReader, manifest_request, parse_chunk, parse_manifest,
                                     IMAH_HEADER, IMAH_SIGNATURE)

XML = ('<?xml version="1.0" encoding="utf-8"?>\n<dji><device id="zv900"><firmware formal="01.14.0000">'
       '<release><module id="2803" version="34.08.30.02" type="" name="CPLD">zv900_2803_v34.08.30.02_20220830.pro.fw.sig</module>'
       '<module id="2805" version="10.00.81.97" type="" name="ZV900_E3T">zv900_2805_v10.00.81.97_20260811.pro.fw.sig</module></release>'
       '</firmware></device></dji>\n')


def container(xml=XML):
    return b'IM*H' + bytes(IMAH_HEADER + IMAH_SIGNATURE - 4) + xml.encode() + b'\0' * 7


def reply_for(request, payload):
    return replace(request, sender_type=request.receiver_type, sender_id=request.receiver_id,
                   receiver_type=request.sender_type, receiver_id=request.sender_id,
                   is_response=1, ack=0, payload=payload)


def chunk_payload(data, remaining, status=0):
    return bytes((status,)) + struct.pack('<II', len(data), remaining) + data


class FirmwareTests(unittest.TestCase):
    def test_request_matches_documented_layout(self):
        packet = manifest_request(7, 256)
        self.assertEqual((packet.command_set, packet.command_id), (0x00, 0x4f))
        self.assertEqual((packet.sender_type, packet.sender_id), (10, 1))
        self.assertEqual((packet.receiver_type, packet.receiver_id), (28, 5))
        self.assertEqual(packet.ack, 2)
        self.assertEqual(packet.payload, bytes.fromhex('01' '00010000' 'e8030000'))
        self.assertEqual(V1Packet.parse(packet.serialize()), packet)

    def test_chunk_rejects_status_and_truncation(self):
        self.assertEqual(parse_chunk(chunk_payload(b'abc', 5)), (b'abc', 5))
        with self.assertRaisesRegex(ValueError, 'status e3'):
            parse_chunk(chunk_payload(b'', 0, status=0xe3))
        with self.assertRaisesRegex(ValueError, 'announced'):
            parse_chunk(bytes(1) + struct.pack('<II', 10, 0) + b'abc')

    def test_parse_manifest_versions(self):
        manifest = parse_manifest(container())
        self.assertEqual(manifest.device, 'zv900')
        self.assertEqual(manifest.firmware, '01.14.0000')
        self.assertEqual(manifest.modules, [
            dict(id='2803', version='34.08.30.02', name='CPLD', file='zv900_2803_v34.08.30.02_20220830.pro.fw.sig'),
            dict(id='2805', version='10.00.81.97', name='ZV900_E3T', file='zv900_2805_v10.00.81.97_20260811.pro.fw.sig')])

    def test_parse_manifest_tolerates_reordered_attributes(self):
        xml = XML.replace('<firmware formal="01.14.0000">', '<firmware rollback="0" formal="01.14.0000">')
        self.assertEqual(parse_manifest(container(xml)).firmware, '01.14.0000')

    def test_parse_manifest_rejects_non_xml_payload(self):
        with self.assertRaisesRegex(ValueError, 'encrypted'):
            parse_manifest(b'IM*H' + bytes(IMAH_HEADER + IMAH_SIGNATURE) + bytes(range(200, 256)))
        with self.assertRaisesRegex(ValueError, 'IM\\*H'):
            parse_manifest(b'XXXX' + container()[4:])

    def test_reader_reassembles_256_byte_chunks(self):
        data = container()
        reader = ManifestReader()
        sequence = iter(range(100, 200))
        now = 0.0
        while not reader.done:
            requests = reader.poll(now, lambda: next(sequence))
            self.assertEqual(len(requests), 1)
            offset = struct.unpack_from('<I', requests[0].payload, 1)[0]
            chunk = data[offset:offset+256]
            # Unrelated and mismatched replies are ignored.
            self.assertFalse(reader.receive(replace(reply_for(requests[0], b'\0'), sequence=1)))
            self.assertTrue(reader.receive(reply_for(requests[0], chunk_payload(chunk, len(data)-offset-len(chunk)))))
            now += .01
        self.assertIsNone(reader.error)
        self.assertEqual(bytes(reader.data), data)
        self.assertEqual(reader.chunks, -(-len(data) // 256))
        self.assertEqual(reader.manifest.firmware, '01.14.0000')

    def test_reader_retries_then_reports_timeout(self):
        reader = ManifestReader(timeout=.5, attempts=3)
        sequence = iter(range(10))
        first = reader.poll(0, lambda: next(sequence))
        self.assertEqual(reader.poll(.2, lambda: next(sequence)), [])
        self.assertEqual(reader.poll(.6, lambda: next(sequence)), first)
        self.assertEqual(reader.poll(1.2, lambda: next(sequence)), first)
        self.assertEqual(reader.poll(1.8, lambda: next(sequence)), [])
        self.assertTrue(reader.done)
        self.assertIn('after 3 attempts', reader.error)

    def test_reader_records_rejection(self):
        reader = ManifestReader()
        request, = reader.poll(0, lambda: 3)
        reader.receive(reply_for(request, chunk_payload(b'', 0, status=0xe3)))
        self.assertTrue(reader.done)
        self.assertIn('status e3', reader.error)


if __name__ == '__main__':
    unittest.main()
