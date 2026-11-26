"""Shared test fixtures: a configuration and a fake network server."""

import base64
import json
import socket
import threading

from lorawan_probe import lorawan

DEV_ADDR = 0x01020304
NWK_SKEY = bytes.fromhex("00112233445566778899aabbccddeeff")
APP_SKEY = bytes.fromhex("ffeeddccbbaa99887766554433221100")

CONTROL = """
[[case]]
name = "control"
control = true
frequency_hz = 868100000
modulation = "LORA"
sf = 7
bw_khz = 125
"""

FSK = """
[[case]]
name = "fsk"
frequency_hz = 868800000
modulation = "FSK"
bitrate = 50000
"""


def config_text(port=1700, cases=CONTROL + FSK):
    return f"""
[server]
host = "127.0.0.1"
port = {port}

[gateway]
eui = "0102030405060708"

[device]
dev_addr = "01020304"
nwk_skey = "{NWK_SKEY.hex()}"
app_skey = "{APP_SKEY.hex()}"

[network]
region = "EU868"

[test]
timeout_s = 0.4
packets = 2
interval_s = 0
{cases}
"""


def build_downlink(fcnt=0, ack=True, nwk_skey=NWK_SKEY, dev_addr=DEV_ADDR,
                   fopts=b"", fport=None, payload=b"", confirmed=False):
    fctrl = (lorawan.FCTRL_ACK if ack else 0) | len(fopts)
    msg = (
        bytes([0xA0 if confirmed else 0x60])
        + dev_addr.to_bytes(4, "little")
        + bytes([fctrl])
        + (fcnt & 0xFFFF).to_bytes(2, "little")
        + fopts
    )
    if fport is not None:
        key = nwk_skey if fport == 0 else APP_SKEY
        msg += bytes([fport]) + lorawan.crypt_payload(
            key, dev_addr, fcnt, lorawan.DOWNLINK, payload
        )
    return msg + lorawan.compute_mic(nwk_skey, dev_addr, fcnt, lorawan.DOWNLINK, msg)


class FakeServer:
    """Answers the Semtech UDP protocol.

    `behaviour(rxpk)` names what happens to an uplink: "ack", "silent",
    "bad_mic", "no_ack_bit", "late", "rx2", "wrong_radio" or "no_push_ack".
    """

    def __init__(self, behaviour=lambda rxpk: "ack", pull_ack=True, ack_status=True):
        self.behaviour = behaviour
        self.ack_status = ack_status
        self.statuses = []
        self.pull_ack = pull_ack
        self.uplinks = []
        self.tx_acks = []
        self._fcnt_down = 0
        self._pull_address = None
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.settimeout(0.05)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self._socket.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, address = self._socket.recvfrom(65535)
            except TimeoutError:
                continue
            token, identifier = data[1:3], data[3]
            if identifier == 0x02 and self.pull_ack:
                self._pull_address = address
                self._socket.sendto(b"\x02" + token + b"\x04", address)
            elif identifier == 0x00:
                body = json.loads(data[12:])
                if "stat" in body:
                    self.statuses.append(body["stat"])
                    if self.ack_status:
                        self._socket.sendto(b"\x02" + token + b"\x01", address)
                else:
                    self._uplink(token, body["rxpk"][0], address)
            elif identifier == 0x05:
                self.tx_acks.append(json.loads(data[12:]))

    def _uplink(self, token, rxpk, address):
        self.uplinks.append(rxpk)
        action = self.behaviour(rxpk)
        if action == "no_push_ack":
            return
        self._socket.sendto(b"\x02" + token + b"\x01", address)
        if action == "silent":
            return
        delay = {"late": 7_000_000, "rx2": 2_000_000}.get(action, 1_000_000)
        phy = build_downlink(
            self._fcnt_down,
            ack=action != "no_ack_bit",
            nwk_skey=bytes(16) if action == "bad_mic" else NWK_SKEY,
        )
        self._fcnt_down += 1
        txpk = {
            "imme": False,
            "tmst": (rxpk["tmst"] + delay) & 0xFFFFFFFF,
            "freq": rxpk["freq"],
            "modu": rxpk["modu"],
            "datr": rxpk["datr"],
            "size": len(phy),
            "data": base64.b64encode(phy).decode().rstrip("="),
        }
        if action == "rx2":
            txpk.update(freq=869.525, modu="LORA", datr="SF12BW125")
        if action == "wrong_radio":
            txpk.update(freq=868.3, modu="LORA", datr="SF9BW125")
        body = json.dumps({"txpk": txpk}).encode()
        self._socket.sendto(b"\x02\xaa\xbb\x03" + body, self._pull_address)
