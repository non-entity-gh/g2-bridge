"""Manage the goggles control handshake independently of byte transport.

The ready state requires a matching simulator-mode success reply. Source
addresses default to the PC USB route. With enter_simulator=False the session
stops in the identified state after the identity reply and keeps heartbeats
running, which lets diagnostics read an untested model's identity without
changing its display mode.
"""
from dataclasses import dataclass, replace
from .control import heartbeat, device_info, sim_mode


# Identity codes that have displayed video through the PC route
MODELS = {'ZV900': 'DJI Goggles 2', 'ZV901': 'DJI Goggles Integra'}
# Identity codes from other sources that have not displayed video through the PC route.
# ZV902 is Goggles 3, as named by github.com/ggurov/dji-goggles3-display-unit.
EXPERIMENTAL_MODELS = {'ZV902': 'DJI Goggles 3'}
SIMULATOR_SILENT = ('The goggles answered but ignored simulator mode. '
                    'Open More > DJI Virtual Flight on the goggles')


class SessionError(RuntimeError):
    pass


class SessionLost(SessionError):
    """Control replies timed out and the connection can be retried."""


@dataclass
class Pending:
    packet: object
    sent_at: float
    attempts: int = 1


class ControlSession:
    def __init__(self, *, source_type=10, source_id=1, sequence=0, models=MODELS, enter_simulator=True):
        # Validate addresses and sequence using the actual serializer.
        replace(heartbeat(sequence), sender_type=source_type, sender_id=source_id).serialize()
        self.models = dict(models)
        self.enter_simulator = enter_simulator
        self.source_type = source_type
        self.source_id = source_id
        self.sequence = sequence
        self.state = 'new'
        self.identity = None
        self.model = None
        self.pending = {}
        self.started_at = None
        self.last_heartbeat_ack = None
        self.next_heartbeat = None

    def reserve_sequence(self):
        """Share the V1 counter with video without reusing pending requests."""
        if self.state == 'failed':
            raise SessionError('Session has failed; create a new one after reconnect')
        for _ in range(65536):
            sequence = self.sequence
            self.sequence = (self.sequence + 1) & 65535
            if sequence not in self.pending:
                return sequence
        raise SessionError('Sequence space exhausted')

    def _new(self, factory, now):
        sequence = self.reserve_sequence()
        packet = replace(factory(sequence), sender_type=self.source_type, sender_id=self.source_id)
        self.pending[sequence] = Pending(packet, now)
        return packet

    def _fail(self, message, error_type=SessionError):
        self.state = 'failed'
        self.pending.clear()
        raise error_type(message)

    def poll(self, now):
        """Return requests due at caller-supplied monotonic seconds."""
        if self.state == 'failed':
            raise SessionError('Session has failed; create a new one after reconnect')
        if self.started_at is None:
            self.started_at = now
            self.next_heartbeat = now
            self.state = 'heartbeat'
        if self.last_heartbeat_ack is not None and now - self.last_heartbeat_ack >= 1.5:
            self._fail('Three heartbeat intervals passed without a matching success reply', SessionLost)
        if self.state not in ('ready', 'identified') and now - self.started_at >= 4:
            self._fail('Control handshake timed out', SessionLost)
        outgoing = []
        for seq, item in list(self.pending.items()):
            if now - item.sent_at < .5:
                continue
            if (item.packet.command_set, item.packet.command_id) == (0, 0):
                # The next heartbeat replaces this expired request.
                del self.pending[seq]
            elif item.attempts >= 3:
                if (item.packet.command_set, item.packet.command_id) == (0x0b, 0x1a):
                    # Heartbeats and identity still answer outside Virtual Flight, but simulator
                    # mode gets no reply at all, on firmware 01.11 and 01.14 alike.
                    self._fail(SIMULATOR_SILENT, SessionLost)
                self._fail('Control request received no response after three attempts', SessionLost)
            else:
                item.attempts += 1
                item.sent_at = now
                outgoing.append(item.packet)
        if now >= self.next_heartbeat:
            outgoing.append(self._new(heartbeat, now))
            self.next_heartbeat = now + .5
        return outgoing

    def receive(self, reply, now):
        """Accept a CRC-validated packet, returning any next handshake request."""
        if self.state == 'failed':
            return []
        pending = self.pending.get(reply.sequence)
        if pending is None or not reply.is_response:
            return []
        req = pending.packet
        if (reply.command_set, reply.command_id, reply.sender_type, reply.sender_id,
                reply.receiver_type, reply.receiver_id) != (
                req.command_set, req.command_id, req.receiver_type, req.receiver_id,
                req.sender_type, req.sender_id):
            return []
        del self.pending[reply.sequence]
        if reply.payload[:1] != b'\0':
            self._fail(f'Goggles rejected {req.command_set:02x}/{req.command_id:02x}: {reply.payload.hex()}')
        command = (reply.command_set, reply.command_id)
        if command == (0, 0):
            self.last_heartbeat_ack = now
            if self.state == 'heartbeat':
                self.state = 'identity'
                return [self._new(device_info, now)]
        elif command == (0, 255) and self.state == 'identity':
            self.identity = reply.payload[1:].rstrip(b'\0').decode('ascii', errors='replace')
            self.model = next((name for code, name in self.models.items()
                               if self.identity.upper().startswith(f'E3T {code}')), None)
            if self.model is None:
                self._fail(f'Unsupported goggles ({self.identity}). Supported: {", ".join(self.models.values())}')
            if not self.enter_simulator:
                self.state = 'identified'
                return []
            self.state = 'simulator'
            return [self._new(sim_mode, now)]
        elif command == (11, 26) and self.state == 'simulator':
            self.state = 'ready'
        return []
