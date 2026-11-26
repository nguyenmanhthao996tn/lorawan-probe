import pytest
from helpers import APP_SKEY, DEV_ADDR, NWK_SKEY, build_downlink

from lorawan_probe import lorawan

# Published ABP example from the lora-packet project
VECTOR_PHY = bytes.fromhex("40F17DBE4900020001954378762B11FF0D")
VECTOR_NWK = bytes.fromhex("44024241ED4CE9A68C6A8BC055233FD3")
VECTOR_APP = bytes.fromhex("EC925802AE430CA77FD3DD73CB2CC588")


def parse(phy, last_fcnt=0):
    return lorawan.parse_downlink(phy, DEV_ADDR, NWK_SKEY, APP_SKEY, last_fcnt)


def test_uplink_matches_published_vector():
    phy = lorawan.build_uplink(
        0x49BE7DF1, VECTOR_NWK, VECTOR_APP, 2, 1, b"test", confirmed=False
    )
    assert phy == VECTOR_PHY


def test_confirmed_uplink_header():
    phy = lorawan.build_uplink(DEV_ADDR, NWK_SKEY, APP_SKEY, 0x12345, 7, b"x")
    assert phy[:9] == bytes.fromhex("800403020100452307")


def test_payload_encryption_over_several_blocks_is_reversible():
    data = bytes(range(40))
    encrypted = lorawan.crypt_payload(APP_SKEY, DEV_ADDR, 5, lorawan.UPLINK, data)
    assert encrypted != data
    assert lorawan.crypt_payload(APP_SKEY, DEV_ADDR, 5, lorawan.UPLINK, encrypted) == data


def test_downlink_round_trip():
    frame = parse(build_downlink(3, fport=2, payload=b"hello", confirmed=True))
    assert (frame.ack, frame.confirmed, frame.fcnt, frame.fport, frame.payload) == (
        True, True, 3, 2, b"hello",
    )


@pytest.mark.parametrize("fcnt, last", [(0x10005, 0x10000), (0x10001, 0xFFFF), (2, 0x30000)])
def test_downlink_counter_upper_bits(fcnt, last):
    assert parse(build_downlink(fcnt), last).fcnt == fcnt


def test_downlink_mac_commands_from_fopts_and_port_zero():
    assert parse(build_downlink(fopts=b"\x06")).mac_commands == ("DevStatusReq",)
    frame = parse(build_downlink(fport=0, payload=bytes.fromhex("0801")))
    assert frame.mac_commands == ("RXTimingSetupReq 01",)


@pytest.mark.parametrize(
    "phy, reason",
    [
        (build_downlink(nwk_skey=bytes(16)), "MIC"),
        (build_downlink(dev_addr=0xAABBCCDD), "DevAddr"),
        (VECTOR_PHY, "not a data downlink"),
        (b"\x60\x00", "too short"),
    ],
)
def test_downlink_rejected(phy, reason):
    with pytest.raises(lorawan.FrameError, match=reason):
        parse(phy)
