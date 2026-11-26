"""Load and validate the TOML configuration."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .regions import REGIONS, DataRate, Region


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Server:
    host: str
    port: int


@dataclass(frozen=True)
class GatewayConfig:
    eui: bytes
    keepalive_s: float
    stat_interval_s: float


@dataclass(frozen=True)
class Device:
    dev_addr: int
    nwk_skey: bytes
    app_skey: bytes
    fport: int
    payload: bytes


@dataclass(frozen=True)
class Network:
    region: Region
    rx1_delay_s: int
    rx1_dr_offset: int
    rx2_frequency_hz: int
    rx2_dr: int
    downlink_dwell_time: bool


@dataclass(frozen=True)
class TestSettings:
    timeout_s: float
    packets: int
    interval_s: float
    state_file: Path


@dataclass(frozen=True)
class Case:
    name: str
    control: bool
    frequency_hz: int
    datarate: DataRate
    coding_rate: str | None
    rssi: int
    snr: float
    chan: int
    rfch: int
    expect_ack: bool = True


@dataclass(frozen=True)
class Config:
    server: Server
    gateway: GatewayConfig
    device: Device
    network: Network
    test: TestSettings
    cases: tuple[Case, ...]


_REQUIRED = object()


class _Table:
    """Typed access to one TOML table that rejects unknown keys."""

    def __init__(self, data, where: str):
        if not isinstance(data, dict):
            raise ConfigError(f"{where} must be a table")
        self.data = dict(data)
        self.where = where

    def take(self, key: str, kind: type, default=_REQUIRED, minimum=None, maximum=None):
        if key not in self.data:
            if default is _REQUIRED:
                raise ConfigError(f"{self.where}: missing '{key}'")
            return default
        value = self.data.pop(key)
        accepted = (int, float) if kind is float else kind
        # bool is a subclass of int, so check it separately
        if not isinstance(value, accepted) or (isinstance(value, bool) and kind is not bool):
            raise ConfigError(f"{self.where}: '{key}' must be of type {kind.__name__}")
        if minimum is not None and value < minimum:
            raise ConfigError(f"{self.where}: '{key}' must be at least {minimum}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"{self.where}: '{key}' must be at most {maximum}")
        return value

    def take_hex(self, key: str, length: int | None = None, default=_REQUIRED) -> bytes:
        text = self.take(key, str, default)
        try:
            value = bytes.fromhex(text)
        except ValueError:
            raise ConfigError(f"{self.where}: '{key}' is not valid hex") from None
        if length is not None and len(value) != length:
            raise ConfigError(f"{self.where}: '{key}' must be {length * 2} hex characters")
        return value

    def done(self):
        if self.data:
            names = ", ".join(sorted(self.data))
            raise ConfigError(f"{self.where}: unknown key(s): {names}")


def _server(table: _Table) -> Server:
    return Server(
        host=table.take("host", str),
        port=table.take("port", int, 1700, 1, 65535),
    )


def _gateway(table: _Table) -> GatewayConfig:
    return GatewayConfig(
        eui=table.take_hex("eui", 8),
        keepalive_s=table.take("keepalive_s", float, 10, 1),
        stat_interval_s=table.take("stat_interval_s", float, 30, 1),
    )


def _device(table: _Table) -> Device:
    return Device(
        dev_addr=int.from_bytes(table.take_hex("dev_addr", 4), "big"),
        nwk_skey=table.take_hex("nwk_skey", 16),
        app_skey=table.take_hex("app_skey", 16),
        fport=table.take("fport", int, 1, 1, 223),
        payload=table.take_hex("payload", default="74657374"),
    )


def _network(table: _Table) -> Network:
    name = table.take("region", str)
    if name not in REGIONS:
        raise ConfigError(f"[network]: unknown region '{name}' (use {', '.join(REGIONS)})")
    region = REGIONS[name]
    return Network(
        region=region,
        rx1_delay_s=table.take("rx1_delay_s", int, 1, 1, 15),
        rx1_dr_offset=table.take("rx1_dr_offset", int, 0, 0, region.max_rx1_dr_offset),
        rx2_frequency_hz=table.take("rx2_frequency_hz", int, region.rx2_frequency_hz, 1),
        rx2_dr=table.take("rx2_dr", int, region.rx2_dr, 0, len(region.datarates) - 1),
        downlink_dwell_time=table.take("downlink_dwell_time", bool, False),
    )


def _test(table: _Table, config_path: Path) -> TestSettings:
    state_file = table.take("state_file", str, f"{config_path.stem}.state.json")
    return TestSettings(
        timeout_s=table.take("timeout_s", float, 5, 0.1),
        packets=table.take("packets", int, 10, 1),
        interval_s=table.take("interval_s", float, 5, 0),
        # relative paths are relative to the configuration file
        state_file=config_path.parent / state_file,
    )


def _case(table: _Table) -> Case:
    name = table.take("name", str)
    table.where = f"case '{name}'"
    modulation = table.take("modulation", str).upper()
    coding_rate = None
    snr = 0.0
    if modulation == "LORA":
        sf = table.take("sf", int, minimum=5, maximum=12)
        bw_khz = table.take("bw_khz", int)
        if bw_khz not in (125, 250, 500):
            raise ConfigError(f"{table.where}: 'bw_khz' must be 125, 250 or 500")
        datarate = DataRate("LORA", sf=sf, bw_khz=bw_khz)
        coding_rate = table.take("coding_rate", str, "4/5")
        if coding_rate not in ("4/5", "4/6", "4/7", "4/8"):
            raise ConfigError(f"{table.where}: 'coding_rate' must be 4/5, 4/6, 4/7 or 4/8")
        snr = table.take("snr", float, 7.0)
    elif modulation == "FSK":
        datarate = DataRate("FSK", bitrate=table.take("bitrate", int, minimum=1))
    else:
        raise ConfigError(f"{table.where}: 'modulation' must be LORA or FSK")
    control = table.take("control", bool, False)
    expect = table.take("expect", str, "ack")
    if expect not in ("ack", "no_ack"):
        raise ConfigError(f"{table.where}: 'expect' must be ack or no_ack")
    if control and expect != "ack":
        raise ConfigError(f"{table.where}: a control case must expect an ack")
    return Case(
        name=name,
        control=control,
        expect_ack=expect == "ack",
        frequency_hz=table.take("frequency_hz", int, minimum=100_000_000),
        datarate=datarate,
        coding_rate=coding_rate,
        rssi=table.take("rssi", int, -50),
        snr=snr,
        chan=table.take("chan", int, 0, 0),
        rfch=table.take("rfch", int, 0, 0),
    )


def load(path: str | Path, require_cases: bool = True) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror}") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from None

    root = _Table(data, "top level")

    def section(name: str, parse, default=_REQUIRED):
        table = _Table(root.take(name, dict, default), f"[{name}]")
        value = parse(table)
        table.done()
        return value

    raw_cases = root.take("case", list, _REQUIRED if require_cases else [])
    cases = []
    for index, raw in enumerate(raw_cases, 1):
        table = _Table(raw, f"case #{index}")
        cases.append(_case(table))
        table.done()
    if require_cases and not cases:
        raise ConfigError("define at least one [[case]]")
    names = [case.name for case in cases]
    if len(set(names)) != len(names):
        raise ConfigError("case names must be unique")

    config = Config(
        server=section("server", _server),
        gateway=section("gateway", _gateway),
        device=section("device", _device),
        network=section("network", _network),
        test=section("test", lambda table: _test(table, path), {}),
        cases=tuple(cases),
    )
    root.done()
    return config
