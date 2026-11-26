"""Minimal Semtech UDP packet forwarder (protocol version 2).

See PROTOCOL.TXT in https://github.com/Lora-net/packet_forwarder.
Single threaded: datagrams are handled, and
keepalives and status messages are sent, only while `poll` runs.
"""

import base64
import binascii
import json
import logging
import os
import socket
import time
from collections import deque
from datetime import datetime, timezone

from .config import Case

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 2
PUSH_DATA = 0x00
PUSH_ACK = 0x01
PULL_DATA = 0x02
PULL_RESP = 0x03
PULL_ACK = 0x04
TX_ACK = 0x05


class GatewayError(RuntimeError):
    pass


def concentrator_time() -> int:
    """Emulated 32-bit microsecond counter of the concentrator."""
    return (time.monotonic_ns() // 1000) & 0xFFFFFFFF


def build_rxpk(case: Case, phy: bytes, tmst: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rxpk = {
        "tmst": tmst,
        "time": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "chan": case.chan,
        "rfch": case.rfch,
        "freq": case.frequency_hz / 1_000_000,
        "stat": 1,
        "modu": case.datarate.modulation,
        "datr": case.datarate.datr,
        "rssi": case.rssi,
        "size": len(phy),
        "data": base64.b64encode(phy).decode(),
    }
    if case.datarate.modulation == "LORA":
        rxpk["codr"] = case.coding_rate
        rxpk["lsnr"] = case.snr
    return rxpk


def decode_data(txpk: dict) -> bytes:
    """Payload of a downlink. Base64 padding is optional in the protocol."""
    text = txpk.get("data")
    if not isinstance(text, str):
        raise ValueError("txpk has no data")
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except binascii.Error as error:
        raise ValueError(f"txpk data is not base64: {error}") from None


def build_stat(
    uplinks: int, acked: int, downlinks: int, now: datetime | None = None
) -> dict:
    """Gateway status for one period. The emulator has no location to report."""
    now = now or datetime.now(timezone.utc)
    return {
        "time": now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S GMT"),
        "rxnb": uplinks,
        "rxok": uplinks,
        "rxfw": uplinks,
        "ackr": round(100 * acked / uplinks, 1) if uplinks else 0.0,
        "dwnb": downlinks,
        "txnb": downlinks,  # every downlink is accepted
    }


class Gateway:
    def __init__(
        self,
        host: str,
        port: int,
        eui: bytes,
        keepalive_s: float,
        stat_interval_s: float = 30,
        on_status=lambda stat, acked: None,
        ack_wait_s: float = 1.0,
    ):
        """`on_status` reports whether the server acknowledged each status message.

        An uplink without a PUSH_ACK counts as unacknowledged after `ack_wait_s`.
        """
        self.ack_wait_s = ack_wait_s
        self.eui = eui
        self.keepalive_s = keepalive_s
        self.stat_interval_s = stat_interval_s
        self.on_status = on_status
        self._next_status = 0.0
        self._pending_status: dict[bytes, dict] = {}
        self._unreported: list[tuple[bytes, float]] = []  # uplink token, send time
        self._period_downlinks = 0
        self.downlinks: deque[dict] = deque()
        self._push_acked: set[bytes] = set()
        self._pull_tokens: set[bytes] = set()
        self.pull_acked = False
        self._next_keepalive = 0.0
        try:
            family, kind, proto, _, address = socket.getaddrinfo(
                host, port, type=socket.SOCK_DGRAM
            )[0]
            self._socket = socket.socket(family, kind, proto)
            self._socket.connect(address)
        except OSError as error:
            raise GatewayError(f"cannot reach {host}:{port}: {error}") from None

    def close(self):
        self._socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self, timeout_s: float):
        """Open the downlink route and require the server to acknowledge it."""
        deadline = time.monotonic() + timeout_s
        while not self.pull_acked and time.monotonic() < deadline:
            self._pull()
            self.poll(min(deadline, time.monotonic() + 1), until=lambda: self.pull_acked)
        if not self.pull_acked:
            raise GatewayError(
                "no PULL_ACK from the server: check host, port and that the "
                "Semtech UDP backend is listening"
            )

    def finish(self, timeout_s: float):
        """Send a last status for the unreported period and wait for its answer."""
        if not self.pull_acked:
            return
        self.poll(
            time.monotonic() + min(timeout_s, self.ack_wait_s),
            until=lambda: all(self.is_acked(token) for token, _ in self._unreported),
        )
        self._send_status(final=True)
        self.poll(time.monotonic() + timeout_s, until=lambda: not self._pending_status)
        self._report_unanswered_status()

    def push(self, rxpk: dict) -> bytes:
        """Send one uplink and return the token its PUSH_ACK will carry."""
        token = os.urandom(2)
        body = json.dumps({"rxpk": [rxpk]}, separators=(",", ":"))
        log.debug("PUSH_DATA %s", body)
        self._push_acked.discard(token)
        self._unreported.append((token, time.monotonic()))
        self._send(PUSH_DATA, token, self.eui + body.encode())
        return token

    def is_acked(self, token: bytes) -> bool:
        return token in self._push_acked

    def poll(self, deadline: float, until=lambda: False):
        """Handle traffic until `until()` is true or the monotonic deadline passes."""
        while True:
            if time.monotonic() >= self._next_keepalive:
                self._pull()
            if self.pull_acked and time.monotonic() >= self._next_status:
                self._send_status()
            if until():
                return
            wake = min(deadline, self._next_keepalive)
            if self.pull_acked:
                wake = min(wake, self._next_status)
            wait = wake - time.monotonic()
            self._socket.settimeout(max(wait, 0.001))
            try:
                datagram = self._socket.recv(65535)
            except TimeoutError:
                pass
            except ConnectionRefusedError:
                log.debug("port unreachable")
                time.sleep(min(max(wait, 0), 0.1))
            except OSError as error:
                log.warning("socket error: %s", error)
                time.sleep(min(max(wait, 0), 0.5))
            else:
                self._handle(datagram)
            if time.monotonic() >= deadline:
                return

    def drain(self):
        """Discard everything that has already arrived."""
        self.poll(time.monotonic() + 0.05)
        self.downlinks.clear()

    def _pull(self):
        token = os.urandom(2)
        self._pull_tokens.add(token)
        self._next_keepalive = time.monotonic() + self.keepalive_s
        self._send(PULL_DATA, token, self.eui)

    def _send_status(self, final: bool = False):
        """Report the uplinks whose outcome is known; the rest wait for the next one."""
        self._report_unanswered_status()
        expired = time.monotonic() - self.ack_wait_s
        known, waiting = [], []
        for token, sent in self._unreported:
            settled = final or self.is_acked(token) or sent <= expired
            (known if settled else waiting).append((token, sent))
        acked = sum(self.is_acked(token) for token, _ in known)
        stat = build_stat(len(known), acked, self._period_downlinks)
        self._unreported = waiting
        self._period_downlinks = 0
        self._next_status = time.monotonic() + self.stat_interval_s
        token = os.urandom(2)
        body = json.dumps({"stat": stat}, separators=(",", ":"))
        log.debug("PUSH_DATA %s", body)
        self._push_acked.discard(token)
        self._pending_status[token] = stat
        self._send(PUSH_DATA, token, self.eui + body.encode())

    def _report_unanswered_status(self):
        while self._pending_status:
            self.on_status(self._pending_status.popitem()[1], False)

    def _send(self, identifier: int, token: bytes, body: bytes):
        packet = bytes([PROTOCOL_VERSION]) + token + bytes([identifier]) + body
        try:
            self._socket.send(packet)
        except ConnectionRefusedError:
            # Reports an ICMP error caused by an earlier datagram; retry once
            try:
                self._socket.send(packet)
            except ConnectionRefusedError:
                log.debug("port unreachable")
        except OSError as error:
            # The uplink then shows up as missing its PUSH_ACK
            log.warning("socket error: %s", error)

    def _handle(self, datagram: bytes):
        if len(datagram) < 4 or datagram[0] != PROTOCOL_VERSION:
            log.debug("ignoring datagram %s", datagram.hex())
            return
        token, identifier = datagram[1:3], datagram[3]
        if identifier == PUSH_ACK:
            log.debug("PUSH_ACK %s", token.hex())
            self._push_acked.add(token)
            if token in self._pending_status:
                self.on_status(self._pending_status.pop(token), True)
        elif identifier == PULL_ACK:
            if token in self._pull_tokens:
                log.debug("PULL_ACK %s", token.hex())
                self.pull_acked = True
        elif identifier == PULL_RESP:
            self._handle_pull_resp(token, datagram[4:])
        else:
            log.debug("ignoring identifier 0x%02x", identifier)

    def _handle_pull_resp(self, token: bytes, body: bytes):
        try:
            txpk = json.loads(body)["txpk"]
            if not isinstance(txpk, dict):
                raise ValueError("txpk is not an object")
        except (ValueError, KeyError, TypeError) as error:
            log.warning("ignoring malformed PULL_RESP: %s", error)
            return
        log.debug("PULL_RESP %s", body.decode(errors="replace"))
        ack = json.dumps({"txpk_ack": {"error": "NONE"}}, separators=(",", ":"))
        self._send(TX_ACK, token, self.eui + ack.encode())
        self._period_downlinks += 1
        self.downlinks.append(txpk)
