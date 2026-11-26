"""LoRaWAN 1.0.x data frames: build, parse, encrypt and MIC. Pure functions.

Follows PayloadEncrypt and PrepareB0 in LoRaMacCrypto.c from
https://github.com/Lora-net/LoRaMac-node.
"""

from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

UPLINK = 0
DOWNLINK = 1

MTYPE_UNCONFIRMED_UP = 0x40
MTYPE_CONFIRMED_UP = 0x80
MTYPE_UNCONFIRMED_DOWN = 0x60
MTYPE_CONFIRMED_DOWN = 0xA0

FCTRL_ACK = 0x20

# CID -> (name, payload length) of MAC commands sent by a network server
_DOWNLINK_MAC_COMMANDS = {
    0x02: ("LinkCheckAns", 2),
    0x03: ("LinkADRReq", 4),
    0x04: ("DutyCycleReq", 1),
    0x05: ("RXParamSetupReq", 4),
    0x06: ("DevStatusReq", 0),
    0x07: ("NewChannelReq", 5),
    0x08: ("RXTimingSetupReq", 1),
    0x09: ("TxParamSetupReq", 1),
    0x0A: ("DlChannelReq", 4),
    0x0D: ("DeviceTimeAns", 5),
}


class FrameError(ValueError):
    """The frame is not a valid downlink for this device."""


@dataclass(frozen=True)
class Downlink:
    confirmed: bool
    ack: bool
    fcnt: int
    fport: int | None
    payload: bytes  # decrypted FRMPayload
    mac_commands: tuple[str, ...]


def _block(first: int, direction: int, dev_addr: int, fcnt: int, last: int) -> bytes:
    """Layout shared by the B0 (MIC) and Ai (encryption) blocks."""
    return (
        bytes([first, 0, 0, 0, 0, direction])
        + dev_addr.to_bytes(4, "little")
        + fcnt.to_bytes(4, "little")
        + bytes([0, last])
    )


def crypt_payload(key: bytes, dev_addr: int, fcnt: int, direction: int, data: bytes) -> bytes:
    """Encrypt or decrypt FRMPayload (the operation is its own inverse)."""
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    out = bytearray()
    for offset in range(0, len(data), 16):
        counter = offset // 16 + 1
        stream = encryptor.update(_block(0x01, direction, dev_addr, fcnt, counter))
        out += bytes(a ^ b for a, b in zip(data[offset : offset + 16], stream))
    return bytes(out)


def compute_mic(nwk_skey: bytes, dev_addr: int, fcnt: int, direction: int, msg: bytes) -> bytes:
    mac = CMAC(algorithms.AES(nwk_skey))
    mac.update(_block(0x49, direction, dev_addr, fcnt, len(msg)) + msg)
    return mac.finalize()[:4]


def build_uplink(
    dev_addr: int,
    nwk_skey: bytes,
    app_skey: bytes,
    fcnt: int,
    fport: int,
    payload: bytes,
    confirmed: bool = True,
) -> bytes:
    """Build a data uplink with ADR off and no FOpts."""
    mhdr = MTYPE_CONFIRMED_UP if confirmed else MTYPE_UNCONFIRMED_UP
    key = nwk_skey if fport == 0 else app_skey
    msg = (
        bytes([mhdr])
        + dev_addr.to_bytes(4, "little")
        + bytes([0x00])  # FCtrl
        + (fcnt & 0xFFFF).to_bytes(2, "little")
        + bytes([fport])
        + crypt_payload(key, dev_addr, fcnt, UPLINK, payload)
    )
    return msg + compute_mic(nwk_skey, dev_addr, fcnt, UPLINK, msg)


def parse_mac_commands(data: bytes) -> tuple[str, ...]:
    commands = []
    index = 0
    while index < len(data):
        cid = data[index]
        if cid not in _DOWNLINK_MAC_COMMANDS:
            commands.append(f"Unknown(0x{cid:02X}) {data[index:].hex()}")
            break
        name, length = _DOWNLINK_MAC_COMMANDS[cid]
        args = data[index + 1 : index + 1 + length]
        commands.append(f"{name} {args.hex()}".strip())
        index += 1 + length
    return tuple(commands)


def parse_downlink(
    phy: bytes,
    dev_addr: int,
    nwk_skey: bytes,
    app_skey: bytes,
    last_fcnt: int = 0,
) -> Downlink:
    """Verify and decrypt a data downlink.

    Only 16 bits of the frame counter are on the air, so the upper bits are
    guessed from `last_fcnt` and confirmed by the MIC.
    """
    if len(phy) < 12:
        raise FrameError(f"frame too short ({len(phy)} bytes)")
    mtype = phy[0] & 0xE0
    if mtype not in (MTYPE_UNCONFIRMED_DOWN, MTYPE_CONFIRMED_DOWN):
        raise FrameError(f"not a data downlink (MHDR 0x{phy[0]:02X})")
    addr = int.from_bytes(phy[1:5], "little")
    if addr != dev_addr:
        raise FrameError(f"DevAddr {addr:08x} is not ours")

    fctrl = phy[5]
    fopts_end = 8 + (fctrl & 0x0F)
    msg, mic = phy[:-4], phy[-4:]
    if fopts_end > len(msg):
        raise FrameError("FOpts longer than the frame")

    low = int.from_bytes(phy[6:8], "little")
    high = last_fcnt >> 16
    for upper in dict.fromkeys((high, high + 1, 0)):
        fcnt = ((upper << 16) | low) & 0xFFFFFFFF
        if compute_mic(nwk_skey, dev_addr, fcnt, DOWNLINK, msg) == mic:
            break
    else:
        raise FrameError("MIC mismatch (check nwk_skey)")

    fport = None
    payload = b""
    commands = parse_mac_commands(msg[8:fopts_end])
    if len(msg) > fopts_end:
        fport = msg[fopts_end]
        key = nwk_skey if fport == 0 else app_skey
        payload = crypt_payload(key, dev_addr, fcnt, DOWNLINK, msg[fopts_end + 1 :])
        if fport == 0:
            commands += parse_mac_commands(payload)

    return Downlink(
        confirmed=mtype == MTYPE_CONFIRMED_DOWN,
        ack=bool(fctrl & FCTRL_ACK),
        fcnt=fcnt,
        fport=fport,
        payload=payload,
        mac_commands=commands,
    )
