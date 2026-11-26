from collections import Counter
from pathlib import Path

import pytest
from helpers import CONTROL, FakeServer, config_text

from lorawan_probe import chirpstack, cli, config

FIXTURE = Path(__file__).parent / "fixture_region_as923_2.toml"


def load(tmp_path, cases="", region="AS923-2", port=1700):
    path = tmp_path / "config.toml"
    path.write_text(config_text(port, cases).replace("EU868", region))
    return config.load(path, require_cases=False)


def build(tmp_path, cases="", region_file=FIXTURE):
    return chirpstack.build_plan(load(tmp_path, cases), region_file)


def manual(name, frequency, extra=""):
    return f"""
[[case]]
name = "{name}"
frequency_hz = {frequency}
modulation = "LORA"
sf = 7
bw_khz = 125
{extra}
"""


def test_every_channel_and_datarate(tmp_path):
    plan = build(tmp_path)
    positive = [c for c in plan.cases if c.expect_ack]
    assert len(positive) == 50
    per_frequency = Counter(c.frequency_hz for c in positive)
    assert per_frequency == {
        **{f: 6 for f in range(921_400_000, 922_400_001, 200_000)},
        922_600_000: 6,
        922_800_000: 6,
        922_500_000: 1,
        922_700_000: 1,
    }
    assert [c.name for c in plan.cases if c.control] == ["lora-921.4-dr5"]


def test_negative_cases(tmp_path):
    negative = {c.name: c.datarate.datr for c in build(tmp_path).cases if not c.expect_ack}
    assert negative == {
        "neg-unlisted-923.0": "SF7BW125",
        "neg-fsk-on-lora-921.4": 50000,
        "neg-lora-on-fsk-922.5": "SF7BW125",
        "neg-dr6-921.4": "SF7BW250",
    }


def test_server_settings_override_the_config(tmp_path):
    plan = build(tmp_path)
    assert plan.config.network.rx2_dr == 0
    assert plan.config.network.rx2_frequency_hz == 921_400_000
    assert any(n.startswith("rx2_dr = 0") for n in plan.notices)


def test_hand_written_cases_replace_generated_ones(tmp_path):
    plan = build(
        tmp_path,
        manual("mine", 921_400_000, "control = true") + manual("extra", 920_200_000),
    )
    names = [c.name for c in plan.cases]
    assert "mine" in names and "lora-921.4-dr5" not in names
    assert names[-1] == "extra" and len(names) == 55
    assert [c.name for c in plan.cases if c.control] == ["mine"]


def test_unsupported_datarates_are_noted(tmp_path):
    path = tmp_path / "region.toml"
    path.write_text(FIXTURE.read_text().replace("max_dr=5", "max_dr=9", 1))
    plan = build(tmp_path, region_file=path)
    assert any("DR8, DR9" in n for n in plan.notices)
    assert len([c for c in plan.cases if c.expect_ack]) == 52


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('"AS923_2"', '"EU868"', "found 0"),
        ("rx1_delay=1", "rx1_delay=99", "at most 15"),
        ("min_dr=7", "min_dr=8", "max_dr"),
    ],
)
def test_invalid_region_file(tmp_path, old, new, message):
    path = tmp_path / "region.toml"
    path.write_text(FIXTURE.read_text().replace(old, new))
    with pytest.raises(config.ConfigError, match=message):
        build(tmp_path, region_file=path)


def arguments(tmp_path, port, *extra):
    load(tmp_path, port=port)
    return ["-c", str(tmp_path / "config.toml"), "--chirpstack-region", str(FIXTURE), *extra]


def test_dry_run_sends_nothing(tmp_path, capsys):
    with FakeServer() as server:
        args = arguments(tmp_path, server.port, "--dry-run", "--packets", "10", "--interval", "5")
        assert cli.main(args) == 0
    assert not server.uplinks
    assert not (tmp_path / "config.state.json").exists()
    assert "55 runs, 550 packets" in capsys.readouterr().out


def test_full_run_against_a_server_that_enforces_channels(tmp_path, capsys):
    allowed = {
        (c.frequency_hz / 1e6, c.datarate.datr)
        for c in build(tmp_path).cases
        if c.expect_ack
    }
    behaviour = lambda rx: "ack" if (rx["freq"], rx["datr"]) in allowed else "silent"
    with FakeServer(behaviour) as server:
        path = tmp_path / "config.toml"
        path.write_text(
            config_text(server.port, "")
            .replace("EU868", "AS923-2")
            .replace("timeout_s = 0.4", "timeout_s = 0.15")
        )
        code = cli.main(["-c", str(path), "--chirpstack-region", str(FIXTURE), "--packets", "1"])
    output = capsys.readouterr().out
    assert code == 0, output
    assert len(server.uplinks) == 55
    assert "Totals: 55 PASS" in output
    assert "922.5    .      .      .      .      .      .      .      1/1" in output
