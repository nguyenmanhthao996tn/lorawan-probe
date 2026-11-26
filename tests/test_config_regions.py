from pathlib import Path

import pytest
from helpers import CONTROL, FSK, config_text

from lorawan_probe import config
from lorawan_probe.regions import REGIONS, DataRate

ROOT = Path(__file__).parent.parent


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_example_config_loads():
    loaded = config.load(ROOT / "config.example.toml")
    assert [case.name for case in loaded.cases] == ["control-lora", "fsk"]
    assert loaded.network.rx2_frequency_hz == 869_525_000


def test_values_and_defaults(tmp_path):
    loaded = config.load(write(tmp_path, config_text()))
    assert loaded.device.dev_addr == 0x01020304
    assert loaded.device.payload == b"test"
    assert loaded.cases[0].coding_rate == "4/5"
    assert loaded.cases[1].datarate == DataRate("FSK", bitrate=50000)
    assert loaded.test.state_file == tmp_path / "config.state.json"


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('eui = "0102030405060708"', 'eui = "01"', "16 hex characters"),
        ('region = "EU868"', 'region = "US915"', "unknown region"),
        ('region = "EU868"', 'region = "EU868"\nrx1_delay = 1', "unknown key"),
        ('region = "EU868"', 'region = "EU868"\nrx1_dr_offset = 6', "at most 5"),
        ("bitrate = 50000", "bitrate = 50000\nsf = 7", "unknown key"),
        ("bitrate = 50000", "bitrate = true", "type int"),
        ('modulation = "FSK"', 'modulation = "OOK"', "LORA or FSK"),
        ('name = "fsk"', 'name = "control"', "unique"),
        ("packets = 2", "packets = 0", "at least 1"),
        ("bitrate = 50000", 'bitrate = 50000\nexpect = "maybe"', "ack or no_ack"),
    ],
)
def test_invalid_config(tmp_path, old, new, message):
    text = config_text(cases=CONTROL + FSK)
    assert old in text
    with pytest.raises(config.ConfigError, match=message):
        config.load(write(tmp_path, text.replace(old, new)))


def test_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="cannot read"):
        config.load(tmp_path / "nope.toml")


def test_region_datarates():
    eu = REGIONS["EU868"]
    assert eu.find_dr(DataRate("FSK", bitrate=50000)) == 7
    assert eu.find_dr(DataRate("LORA", sf=7, bw_khz=250)) == 6
    assert eu.find_dr(DataRate("LORA", sf=7, bw_khz=500)) is None
    assert eu.rx1_dr(7, 0) == 7
    assert eu.rx1_dr(2, 5) == 0


def test_as923():
    region = REGIONS["AS923-2"]
    assert region.rx2_frequency_hz == 921_400_000
    assert REGIONS["AS923-3"].rx2_frequency_hz == 916_600_000
    assert REGIONS["AS923-4"].rx2_frequency_hz == 917_300_000
    assert region.rx1_dr(0, 0) == 0
    assert region.rx1_dr(0, 0, downlink_dwell_time=True) == 2
    assert region.rx1_dr(5, 7) == 7
