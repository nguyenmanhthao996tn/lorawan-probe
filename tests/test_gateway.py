import base64
from datetime import datetime, timezone

import pytest
from helpers import config_text

from lorawan_probe import config
from lorawan_probe.gateway import build_rxpk, decode_data


@pytest.fixture
def cases(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(config_text())
    return config.load(path).cases


NOW = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
COMMON = {
    "tmst": 123, "time": "2026-01-02T03:04:05.000006Z", "chan": 0, "rfch": 0,
    "stat": 1, "rssi": -50, "size": 3, "data": "AQID",
}


def test_lora_rxpk(cases):
    assert build_rxpk(cases[0], b"\x01\x02\x03", 123, NOW) == COMMON | {
        "freq": 868.1, "modu": "LORA", "datr": "SF7BW125", "codr": "4/5", "lsnr": 7.0,
    }


def test_fsk_rxpk_has_integer_datarate_and_no_lora_fields(cases):
    rxpk = build_rxpk(cases[1], b"\x01\x02\x03", 123, NOW)
    assert rxpk == COMMON | {"freq": 868.8, "modu": "FSK", "datr": 50000}
    assert type(rxpk["datr"]) is int


@pytest.mark.parametrize("size", [1, 2, 3])
def test_downlink_data_padding_is_optional(size):
    data = bytes(range(size))
    text = base64.b64encode(data).decode()
    assert decode_data({"data": text}) == data
    assert decode_data({"data": text.rstrip("=")}) == data


@pytest.mark.parametrize("txpk", [{}, {"data": 5}, {"data": "!!!!"}])
def test_bad_downlink_data(txpk):
    with pytest.raises(ValueError):
        decode_data(txpk)
