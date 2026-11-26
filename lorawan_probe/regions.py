"""Regional parameters, taken from RegionEU868 and RegionAS923 in
https://github.com/Lora-net/LoRaMac-node.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DataRate:
    modulation: str  # "LORA" or "FSK"
    sf: int | None = None
    bw_khz: int | None = None
    bitrate: int | None = None

    @property
    def datr(self) -> str | int:
        """Value of the Semtech UDP `datr` field."""
        if self.modulation == "FSK":
            return self.bitrate
        return f"SF{self.sf}BW{self.bw_khz}"


# DR0..DR7 are the same in EU868 and AS923
_DATARATES = tuple(
    [DataRate("LORA", sf=sf, bw_khz=125) for sf in (12, 11, 10, 9, 8, 7)]
    + [DataRate("LORA", sf=7, bw_khz=250), DataRate("FSK", bitrate=50000)]
)

# Effective RX1 datarate, indexed [uplink DR][RX1 DR offset]
_AS923_RX1_DWELL0 = (
    (0, 0, 0, 0, 0, 0, 1, 2),
    (1, 0, 0, 0, 0, 0, 2, 3),
    (2, 1, 0, 0, 0, 0, 3, 4),
    (3, 2, 1, 0, 0, 0, 4, 5),
    (4, 3, 2, 1, 0, 0, 5, 6),
    (5, 4, 3, 2, 1, 0, 6, 7),
    (6, 5, 4, 3, 2, 1, 7, 7),
    (7, 6, 5, 4, 3, 2, 7, 7),
)
_AS923_RX1_DWELL1 = (
    (2, 2, 2, 2, 2, 2, 2, 2),
    (2, 2, 2, 2, 2, 2, 2, 3),
    (2, 2, 2, 2, 2, 2, 3, 4),
    (3, 2, 2, 2, 2, 2, 4, 5),
    (4, 3, 2, 2, 2, 2, 5, 6),
    (5, 4, 3, 2, 2, 2, 6, 7),
    (6, 5, 4, 3, 2, 2, 7, 7),
    (7, 6, 5, 4, 3, 2, 7, 7),
)


@dataclass(frozen=True)
class Region:
    name: str
    rx2_frequency_hz: int
    rx2_dr: int
    max_rx1_dr_offset: int
    max_payload: tuple[int, ...]  # application payload bytes per DR
    default_frequencies: tuple[int, ...]  # mandatory channels, DR0-DR5
    datarates: tuple[DataRate, ...] = _DATARATES

    def find_dr(self, datarate: DataRate) -> int | None:
        return self.datarates.index(datarate) if datarate in self.datarates else None

    def rx1_dr(self, uplink_dr: int, offset: int, downlink_dwell_time: bool = False) -> int:
        if self.name.startswith("AS923"):
            table = _AS923_RX1_DWELL1 if downlink_dwell_time else _AS923_RX1_DWELL0
            return table[uplink_dr][offset]
        return max(uplink_dr - offset, 0)


def _as923(group: int, offset_hz: int) -> Region:
    return Region(
        name=f"AS923-{group}",
        rx2_frequency_hz=923_200_000 + offset_hz,
        rx2_dr=2,
        max_rx1_dr_offset=7,
        max_payload=(51, 51, 115, 115, 242, 242, 242, 242),
        default_frequencies=(923_200_000 + offset_hz, 923_400_000 + offset_hz),
    )


REGIONS = {
    region.name: region
    for region in (
        Region(
            name="EU868",
            rx2_frequency_hz=869_525_000,
            rx2_dr=0,
            max_rx1_dr_offset=5,
            max_payload=(51, 51, 51, 115, 242, 242, 242, 242),
            default_frequencies=(868_100_000, 868_300_000, 868_500_000),
        ),
        _as923(1, 0),
        _as923(2, -1_800_000),
        _as923(3, -6_600_000),
        _as923(4, -5_900_000),
    )
}
