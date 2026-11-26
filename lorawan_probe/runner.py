"""Run the test cases and decide a verdict for each."""

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from . import lorawan
from .config import Case, Config
from .gateway import Gateway, build_rxpk, concentrator_time, decode_data
from .state import State

log = logging.getLogger(__name__)

# Allowed deviation of a downlink timestamp from the receive window
TMST_TOLERANCE_US = 1000


class Verdict(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


@dataclass
class Attempt:
    fcnt: int
    sent_at: str = ""
    push_acked: bool = False
    acked: bool = False  # a valid ACK downlink arrived
    window: str | None = None  # "RX1" or "RX2"
    rejected: list[str] = field(default_factory=list)  # why downlinks did not count
    warnings: list[str] = field(default_factory=list)
    mac_commands: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    name: str
    control: bool
    recheck: bool  # repeat of a control after the negative cases
    expect_ack: bool
    frequency_hz: int
    modulation: str
    datr: str | int
    dr: int | None
    verdict: Verdict
    detail: str
    acked: int
    attempts: list[Attempt]
    warnings: list[str]


def case_warnings(config: Config, case: Case) -> list[str]:
    region = config.network.region
    dr = region.find_dr(case.datarate)
    if dr is None:
        return [f"{case.datarate.datr} is not a {region.name} datarate"]
    if len(config.device.payload) > region.max_payload[dr]:
        return [f"payload exceeds the {region.max_payload[dr]} byte limit of DR{dr}"]
    return []


def receive_window(config: Config, uplink_tmst: int, txpk: dict) -> str:
    """Which receive window of this uplink the downlink is scheduled in.

    Raises ValueError if it belongs to neither.
    """
    tmst = txpk.get("tmst")
    if txpk.get("imme") or not isinstance(tmst, int):
        raise ValueError("downlink is not scheduled by timestamp")
    delta = (tmst - uplink_tmst) & 0xFFFFFFFF
    rx1 = config.network.rx1_delay_s * 1_000_000
    for window, expected in (("RX1", rx1), ("RX2", rx1 + 1_000_000)):
        if abs(delta - expected) <= TMST_TOLERANCE_US:
            return window
    raise ValueError(
        f"scheduled {delta / 1e6:.3f} s after the uplink, expected "
        f"{rx1 / 1e6:.0f} s or {rx1 / 1e6 + 1:.0f} s (check rx1_delay_s)"
    )


def radio_warnings(config: Config, case: Case, window: str, txpk: dict) -> list[str]:
    """Compare the downlink radio parameters with the expected receive window."""
    network = config.network
    region = network.region
    if window == "RX1":
        frequency_hz = case.frequency_hz
        uplink_dr = region.find_dr(case.datarate)
        if uplink_dr is None:
            datarate = None  # no rule for a datarate outside the region table
        else:
            dr = region.rx1_dr(uplink_dr, network.rx1_dr_offset, network.downlink_dwell_time)
            datarate = region.datarates[dr]
    else:
        frequency_hz = network.rx2_frequency_hz
        datarate = region.datarates[network.rx2_dr]

    warnings = []
    freq = txpk.get("freq")
    if not isinstance(freq, (int, float)) or round(freq * 1_000_000) != frequency_hz:
        warnings.append(f"{window} frequency is {freq} MHz, expected {frequency_hz / 1e6}")
    if datarate is not None:
        got = (txpk.get("modu"), txpk.get("datr"))
        expected = (datarate.modulation, datarate.datr)
        if got != expected:
            warnings.append(
                f"{window} datarate is {got[0]} {got[1]}, expected {expected[0]} {expected[1]}"
            )
    return warnings


def _count(attempts: list[Attempt]) -> int:
    return sum(attempt.acked for attempt in attempts)


def _seen(attempts: list[Attempt]) -> str:
    """Summary of what came back over all packets of a case."""
    parts = []
    windows = [attempt.window for attempt in attempts if attempt.acked]
    if windows:
        split = ", ".join(f"{w} {windows.count(w)}" for w in sorted(set(windows)))
        parts.append(f"{len(windows)}/{len(attempts)} ACK ({split})")
    else:
        parts.append(f"0/{len(attempts)} ACK")
    missing = sum(not attempt.push_acked for attempt in attempts)
    if missing:
        parts.append(f"no PUSH_ACK for {missing}")
    rejected = list(dict.fromkeys(r for attempt in attempts for r in attempt.rejected))
    if rejected:
        parts.append("downlink not accepted: " + "; ".join(rejected))
    return ", ".join(parts)


def judge_positive(
    case: Case, attempts: list[Attempt], control_passed: bool | None
) -> tuple[Verdict, str]:
    """Verdict for a case that expects an ACK. `control_passed` is None without a control."""
    detail = _seen(attempts)
    acked = _count(attempts)
    if acked == len(attempts):
        return Verdict.PASS, detail
    if acked:
        return Verdict.PARTIAL, detail
    if not any(attempt.push_acked for attempt in attempts):
        return Verdict.ERROR, detail
    if not case.control:
        if control_passed is False:
            return Verdict.INCONCLUSIVE, f"control case failed; {detail}"
        if control_passed is None and not any(attempt.rejected for attempt in attempts):
            return Verdict.INCONCLUSIVE, f"no control case; {detail}"
    return Verdict.FAIL, detail


def judge_negative(
    attempts: list[Attempt], control_passed: bool | None, recheck_passed: bool | None
) -> tuple[Verdict, str]:
    """Verdict for a case that the server should ignore."""
    detail = _seen(attempts)
    if _count(attempts):
        return Verdict.FAIL, f"server accepted it; {detail}"
    if not any(attempt.push_acked for attempt in attempts):
        return Verdict.ERROR, detail
    if not control_passed:
        return Verdict.INCONCLUSIVE, f"no working control case; {detail}"
    if not recheck_passed:
        return Verdict.INCONCLUSIVE, f"control re-check did not succeed; {detail}"
    if any(attempt.rejected or not attempt.push_acked for attempt in attempts):
        return Verdict.INCONCLUSIVE, detail
    return Verdict.PASS, f"rejected as expected, {detail}"


class Runner:
    def __init__(self, config: Config, gateway: Gateway, state: State):
        self.config = config
        self.gateway = gateway
        self.state = state
        self.results: list[CaseResult] = []  # finished cases, also after an interrupt
        self._sent_before = False

    def run(
        self,
        cases: list[Case],
        on_result=lambda result: None,
        on_packet=lambda number, total, case, packet, attempt: None,
    ) -> list[CaseResult]:
        """Run controls, cases expecting an ACK, negative cases, then controls again.

        Negative results are only final once the controls have been repeated.
        """
        self.gateway.connect(self.config.test.timeout_s)
        controls = [case for case in cases if case.control]
        positives = [case for case in cases if not case.control and case.expect_ack]
        negatives = [case for case in cases if not case.expect_ack]
        rechecks = controls if negatives else []
        total = len(cases) + len(rechecks)

        control_passed = None  # None while no control case has run
        recheck_passed = None
        pending: list[tuple[Case, list[Attempt]]] = []
        number = 0

        def send(case):
            nonlocal number
            number += 1
            return self._send_packets(case, lambda *args: on_packet(number, total, case, *args))

        def finish(case, attempts, judged, recheck=False):
            result = self._result(case, attempts, judged, recheck)
            self.results.append(result)
            on_result(result)

        try:
            for case in controls + positives:
                attempts = send(case)
                if case.control:
                    control_passed = bool(control_passed) or _count(attempts) > 0
                finish(case, attempts, judge_positive(case, attempts, control_passed))
            for case in negatives:
                pending.append((case, send(case)))
            for case in rechecks:
                case = dataclasses.replace(case, name=f"{case.name}-recheck")
                attempts = send(case)
                recheck_passed = bool(recheck_passed) or _count(attempts) > 0
                finish(case, attempts, judge_positive(case, attempts, control_passed), True)
        finally:
            for case, attempts in pending:
                finish(case, attempts, judge_negative(attempts, control_passed, recheck_passed))
        return self.results

    def _send_packets(self, case: Case, on_packet) -> list[Attempt]:
        attempts = []
        for packet in range(1, self.config.test.packets + 1):
            if self._sent_before:
                self.gateway.poll(time.monotonic() + self.config.test.interval_s)
            self._sent_before = True
            attempts.append(self._attempt(case))
            on_packet(packet, attempts[-1])
        return attempts

    def _result(
        self, case: Case, attempts: list[Attempt], judged: tuple[Verdict, str], recheck: bool
    ) -> CaseResult:
        warnings = case_warnings(self.config, case)
        if case.expect_ack:
            warnings += dict.fromkeys(w for attempt in attempts for w in attempt.warnings)
        return CaseResult(
            name=case.name,
            control=case.control,
            recheck=recheck,
            expect_ack=case.expect_ack,
            frequency_hz=case.frequency_hz,
            modulation=case.datarate.modulation,
            datr=case.datarate.datr,
            dr=self.config.network.region.find_dr(case.datarate),
            verdict=judged[0],
            detail=judged[1],
            acked=_count(attempts),
            attempts=attempts,
            warnings=warnings,
        )

    def _attempt(self, case: Case) -> Attempt:
        device = self.config.device
        self.gateway.drain()
        attempt = Attempt(
            fcnt=self.state.next_fcnt_up(),
            sent_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        phy = lorawan.build_uplink(
            device.dev_addr,
            device.nwk_skey,
            device.app_skey,
            attempt.fcnt,
            device.fport,
            device.payload,
        )
        log.debug("case %s fcnt %d uplink %s", case.name, attempt.fcnt, phy.hex())
        tmst = concentrator_time()
        token = self.gateway.push(build_rxpk(case, phy, tmst))

        deadline = time.monotonic() + self.config.test.timeout_s
        while True:
            self.gateway.poll(
                deadline,
                until=lambda: bool(self.gateway.downlinks)
                or (attempt.acked and self.gateway.is_acked(token)),
            )
            while self.gateway.downlinks:
                self._check_downlink(case, attempt, tmst, self.gateway.downlinks.popleft())
            attempt.push_acked = self.gateway.is_acked(token)
            if (attempt.acked and attempt.push_acked) or time.monotonic() >= deadline:
                return attempt

    def _check_downlink(self, case: Case, attempt: Attempt, tmst: int, txpk: dict):
        device = self.config.device
        try:
            phy = decode_data(txpk)
            log.debug("downlink %s", phy.hex())
            frame = lorawan.parse_downlink(
                phy, device.dev_addr, device.nwk_skey, device.app_skey, self.state.fcnt_down
            )
            self.state.saw_fcnt_down(frame.fcnt)
            attempt.mac_commands.extend(frame.mac_commands)
            for command in frame.mac_commands:
                log.info("MAC command (not answered): %s", command)
            if not frame.ack:
                raise ValueError("ACK bit is not set")
            window = receive_window(self.config, tmst, txpk)
        except ValueError as error:
            log.info("downlink not accepted: %s", error)
            attempt.rejected.append(str(error))
            return
        if not attempt.acked:
            attempt.acked = True
            attempt.window = window
            attempt.warnings = radio_warnings(self.config, case, window, txpk)
