from dataclasses import replace
import unittest
from goggles_bridge.control import V1Packet
from goggles_bridge.session import ControlSession, SessionError, SessionLost, MODELS, EXPERIMENTAL_MODELS


def answer(request, payload=b'\0'):
    return replace(request, sender_type=request.receiver_type, sender_id=request.receiver_id,
                   receiver_type=request.sender_type, receiver_id=request.sender_id,
                   is_response=1, ack=2, payload=payload)


class SessionTests(unittest.TestCase):
    def test_observed_exchange(self):
        session = ControlSession(sequence=0x6000)
        hb, = session.poll(0)
        self.assertEqual(hb.serialize().hex(), '551304032a1c00604000000000200000007b4f')
        info, = session.receive(V1Packet.parse(bytes.fromhex('5514046d1c2a0060c00000000000200000006d40')), .01)
        self.assertEqual(info.serialize().hex(), '550e04662a3c01604000ff0098ea')
        mode, = session.receive(V1Packet.parse(bytes.fromhex('552f04633c2a0160c000ff00453354205a563930302041432044656320323420323032347c31323a33343a3034141c')), .02)
        self.assertEqual(mode.serialize().hex(), '551104922a1c0260400b1a010100009dcc')
        session.receive(V1Packet.parse(bytes.fromhex('550e04661c2a0260c00b1a003168')), .03)
        self.assertEqual(session.state, 'ready')
        self.assertTrue(session.identity.startswith('E3T ZV900'))
        self.assertEqual(session.model, 'DJI Goggles 2')

    def test_firmware_0114_exchange(self):
        # Goggles 2 firmware 01.14.0000 (E3T 10.00.81.97), captured November 8, 2026.
        session = ControlSession()
        hb, = session.poll(0)
        self.assertEqual(hb.serialize().hex(), '551304032a1c0000400000000020000000b5b2')
        info, = session.receive(V1Packet.parse(bytes.fromhex('5514046d1c2a0000c0000000000020000000e26f')), .01)
        self.assertEqual(info.serialize().hex(), '550e04662a3c01004000ff002b4b')
        mode, = session.receive(V1Packet.parse(bytes.fromhex(
            '552f04633c2a0100c000ff00453354205a563930302041432041756720313120323032367c30373a32313a3237898e')), .02)
        self.assertEqual(mode.serialize().hex(), '551104922a1c0200400b1a010100009c1f')
        session.receive(V1Packet.parse(bytes.fromhex('550e04661c2a0200c00b1a0082c9')), .03)
        self.assertEqual(session.state, 'ready')
        self.assertEqual(session.identity, 'E3T ZV900 AC Aug 11 2026|07:21:27')
        self.assertEqual(session.model, 'DJI Goggles 2')

    def test_silent_simulator_mode_points_to_virtual_flight(self):
        session = ControlSession()
        request, = session.poll(0)
        info, = session.receive(answer(request), .01)
        mode, = session.receive(answer(info, b'\0E3T ZV900 AC'), .02)
        for now in (.52, 1.02):
            due = session.poll(now)
            self.assertIn(mode, due)
            session.receive(answer(next(p for p in due if p.command_id == 0)), now+.01)
        with self.assertRaisesRegex(SessionLost, 'DJI Virtual Flight'):
            session.poll(1.53)

    def test_integra_identity_is_detected(self):
        session = ControlSession()
        request, = session.poll(0)
        info, = session.receive(answer(request), .01)
        mode, = session.receive(answer(info, b'\0E3T ZV901'), .02)
        self.assertEqual((mode.command_set, mode.command_id), (11, 26))
        self.assertEqual(session.model, 'DJI Goggles Integra')

    def test_goggles3_needs_experimental_models(self):
        session = ControlSession(models={**MODELS, **EXPERIMENTAL_MODELS})
        request, = session.poll(0)
        info, = session.receive(answer(request), .01)
        mode, = session.receive(answer(info, b'\0E3T ZV902 AC'), .02)
        self.assertEqual((mode.command_set, mode.command_id), (11, 26))
        self.assertEqual(session.model, 'DJI Goggles 3')

    def test_identify_only_skips_simulator_and_keeps_heartbeat(self):
        session = ControlSession(models={**MODELS, **EXPERIMENTAL_MODELS}, enter_simulator=False)
        request, = session.poll(0)
        info, = session.receive(answer(request), .01)
        self.assertEqual(session.receive(answer(info, b'\0E3T ZV902'), .02), [])
        self.assertEqual((session.state, session.model), ('identified', 'DJI Goggles 3'))
        for now in (.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5):
            due = session.poll(now)
            self.assertEqual([(p.command_set, p.command_id) for p in due], [(0, 0)])
            session.receive(answer(due[0]), now+.01)
        self.assertEqual(session.state, 'identified')

    def test_wrong_route_and_sequence_cannot_unlock_video(self):
        session = ControlSession()
        request, = session.poll(0)
        for invalid in (replace(answer(request), sequence=1), replace(answer(request), receiver_type=2), replace(answer(request), command_id=255)):
            self.assertEqual(session.receive(invalid, .01), [])
        self.assertEqual(session.state, 'heartbeat')

    def test_rejected_identity_and_error_code(self):
        for payload in (b'\0E3T ZV999', b'\0E3T ZV902', b'\xe0'):
            session = ControlSession()
            request, = session.poll(0)
            info, = session.receive(answer(request), .01)
            with self.assertRaises(SessionError):
                session.receive(answer(info, payload), .02)
            self.assertEqual(session.state, 'failed')

    def test_bounded_retry_and_disconnect(self):
        session = ControlSession(sequence=65535)
        request, = session.poll(0)
        info, = session.receive(answer(request), .01)
        self.assertEqual(info.sequence, 0)
        for now in (.51, 1.01):
            due = session.poll(now)
            self.assertIn(info, due)
            hb = next(p for p in due if p.command_id == 0)
            session.receive(answer(hb), now+.01)
        with self.assertRaises(SessionError):
            session.poll(1.52)
        session = ControlSession()
        request, = session.poll(0)
        session.receive(answer(request), .01)
        with self.assertRaises(SessionError):
            session.poll(1.51)

    def test_mobile_address_is_explicit(self):
        session = ControlSession(source_type=2, source_id=0)
        request, = session.poll(0)
        self.assertEqual(request.serialize()[4], 2)
