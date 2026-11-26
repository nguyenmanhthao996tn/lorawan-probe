"""Build test cases from a ChirpStack v4 region configuration file."""

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config as configuration
from .config import Case, Config, ConfigError
from .regions import DataRate, Region

DEFAULT_CHANNEL_DRS = range(0, 6)
UNLISTED_STEP_HZ = 200_000

# ChirpStack [regions.network] key -> our [network] key
_SETTINGS = {
    "rx1_delay": "rx1_delay_s",
    "rx1_dr_offset": "rx1_dr_offset",
    "rx2_dr": "rx2_dr",
    "rx2_frequency": "rx2_frequency_hz",
    "dwell_time_400ms": "downlink_dwell_time",
}


@dataclass(frozen=True)
class Plan:
    config: Config  # network settings taken from the server file
    cases: list[Case]
    notices: list[str]


def _region_entry(path: Path, region: Region) -> dict:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror}") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from None

    matches = []
    for entry in data.get("regions", []):
        name = str(entry.get("common_name", "")).replace("_", "-")
        if name == "AS923":
            name = "AS923-1"
        if name == region.name:
            matches.append(entry)
    if len(matches) != 1:
        raise ConfigError(
            f"{path}: expected one [[regions]] entry for {region.name}, found {len(matches)}"
        )
    return matches[0]


def _network_settings(config: Config, server: dict, notices: list[str]) -> Config:
    current = config.network
    values = {"region": current.region.name}
    for ours in _SETTINGS.values():
        values[ours] = getattr(current, ours)
    for theirs, ours in _SETTINGS.items():
        if theirs in server:
            if server[theirs] != values[ours]:
                notices.append(f"{ours} = {server[theirs]} from the server (was {values[ours]})")
            values[ours] = server[theirs]
    table = configuration._Table(values, "[regions.network]")
    return dataclasses.replace(config, network=configuration._network(table))


def _channels(region: Region, server: dict, notices: list[str]) -> dict[int, set[int]]:
    """Frequency -> datarates the server accepts on it."""
    channels = {frequency: set(DEFAULT_CHANNEL_DRS) for frequency in region.default_frequencies}
    skipped = set()
    for index, raw in enumerate(server.get("extra_channels", []), 1):
        table = configuration._Table(raw, f"extra channel #{index}")
        frequency = table.take("frequency", int, minimum=100_000_000)
        min_dr = table.take("min_dr", int, minimum=0, maximum=15)
        max_dr = table.take("max_dr", int, minimum=min_dr, maximum=15)
        table.done()
        for dr in range(min_dr, max_dr + 1):
            if dr < len(region.datarates):
                channels.setdefault(frequency, set()).add(dr)
            else:
                skipped.add(dr)
    if skipped:
        listed = ", ".join(f"DR{dr}" for dr in sorted(skipped))
        notices.append(f"not tested (unsupported by this tool): {listed}")
    if server.get("enabled_uplink_channels"):
        notices.append("enabled_uplink_channels is set but not interpreted")
    return channels


def _case(name: str, frequency_hz: int, datarate: DataRate, expect_ack: bool = True) -> Case:
    lora = datarate.modulation == "LORA"
    return Case(
        name=name,
        control=False,
        frequency_hz=frequency_hz,
        datarate=datarate,
        coding_rate="4/5" if lora else None,
        rssi=-50,
        snr=7.0 if lora else 0.0,
        chan=0,
        rfch=0,
        expect_ack=expect_ack,
    )


def _name(frequency_hz: int, datarate: DataRate, dr: int) -> str:
    return f"{datarate.modulation.lower()}-{frequency_hz / 1e6}-dr{dr}"


def _negative_cases(region: Region, channels: dict[int, set[int]]) -> list[Case]:
    """Frequency and datarate pairs that are absent from the server's channels."""
    fsk, wide, top = 7, 6, 5
    ordered = sorted(channels)
    wanted = [("unlisted", max(ordered) + UNLISTED_STEP_HZ, top)]
    first_without = lambda dr: next((f for f in ordered if dr not in channels[f]), None)
    wanted.append(("fsk-on-lora", first_without(fsk), fsk))
    fsk_only = next((f for f in ordered if fsk in channels[f] and top not in channels[f]), None)
    wanted.append(("lora-on-fsk", fsk_only, top))
    wanted.append(("dr6", first_without(wide), wide))
    return [
        _case(f"neg-{kind}-{frequency / 1e6}", frequency, region.datarates[dr], expect_ack=False)
        for kind, frequency, dr in wanted
        if frequency is not None and dr not in channels.get(frequency, ())
    ]


def build_plan(config: Config, path: str | Path) -> Plan:
    """Cases for every channel of the region file, merged with hand-written ones.

    A hand-written case replaces the generated case on the same frequency and
    datarate.
    """
    region = config.network.region
    server = _region_entry(Path(path), region).get("network", {})
    notices: list[str] = []
    config = _network_settings(config, server, notices)
    channels = _channels(region, server, notices)

    generated = [
        _case(_name(f, region.datarates[dr], dr), f, region.datarates[dr])
        for f in sorted(channels)
        for dr in sorted(channels[f])
    ]
    generated += _negative_cases(region, channels)

    def key(case):
        return case.frequency_hz, case.datarate

    manual = {}
    for case in config.cases:
        if key(case) in manual:
            raise ConfigError(
                f"cases '{manual[key(case)].name}' and '{case.name}' test the same channel"
            )
        manual[key(case)] = case
    cases = [manual.pop(key(case), case) for case in generated]
    cases += manual.values()

    if not any(case.control for case in cases):
        default = (region.default_frequencies[0], region.datarates[5])
        index = next(i for i, case in enumerate(cases) if key(case) == default)
        if not cases[index].expect_ack:
            raise ConfigError(f"case '{cases[index].name}' cannot be the control")
        cases[index] = dataclasses.replace(cases[index], control=True)

    names = [case.name for case in cases]
    if len(set(names)) != len(names):
        raise ConfigError("a case name in the configuration clashes with a generated one")
    return Plan(config, cases, notices)
