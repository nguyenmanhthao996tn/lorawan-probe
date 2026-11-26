"""Command line entry point."""

import argparse
import dataclasses
import logging
import signal
import sys

from . import chirpstack, report
from . import config as configuration
from .gateway import Gateway, GatewayError
from .runner import Attempt, CaseResult, Runner, Verdict
from .state import State, StateError

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


def exit_code(results: list[CaseResult]) -> int:
    verdicts = {result.verdict for result in results}
    if verdicts & {Verdict.ERROR, Verdict.INCONCLUSIVE}:
        return EXIT_ERROR
    return EXIT_FAIL if verdicts & {Verdict.FAIL, Verdict.PARTIAL} else EXIT_PASS


def _fcnt(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("must fit in 32 bits")
    return value


def _packets(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _interval(text: str) -> float:
    value = float(text)
    if not 0 <= value < float("inf"):
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _describe(case: configuration.Case) -> str:
    return f"{case.frequency_hz / 1e6:.3f} MHz {case.datarate.modulation} {case.datarate.datr}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lorawan-probe",
        description="Test which LoRaWAN channels a network server supports.",
    )
    parser.add_argument("-c", "--config", required=True, help="TOML configuration file")
    parser.add_argument(
        "--chirpstack-region",
        metavar="FILE",
        help="test every channel of this ChirpStack region file",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list the cases and settings, send nothing"
    )
    parser.add_argument(
        "--case",
        action="append",
        metavar="NAME",
        help="run only this case (repeatable); control cases always run",
    )
    parser.add_argument("--packets", type=_packets, help="packets per case")
    parser.add_argument("--interval", type=_interval, help="seconds to pause between packets")
    parser.add_argument("--fcnt", type=_fcnt, help="next uplink frame counter to use")
    parser.add_argument("-v", "--verbose", action="store_true", help="show frames and JSON")
    parser.add_argument("--json", metavar="FILE", help="also write the report as JSON")
    return parser


def _prepare(args) -> tuple[configuration.Config, list[configuration.Case], list[str]]:
    config = configuration.load(args.config, require_cases=not args.chirpstack_region)
    cases, notices = list(config.cases), []
    if args.chirpstack_region:
        plan = chirpstack.build_plan(config, args.chirpstack_region)
        config, cases, notices = plan.config, plan.cases, plan.notices
    if args.case:
        unknown = set(args.case) - {case.name for case in cases}
        if unknown:
            raise configuration.ConfigError(f"unknown case(s): {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case.control or case.name in args.case]
    overrides = {"packets": args.packets, "interval_s": args.interval}
    test = dataclasses.replace(
        config.test, **{key: value for key, value in overrides.items() if value is not None}
    )
    return dataclasses.replace(config, test=test), cases, notices


def _dry_run(config: configuration.Config, cases: list[configuration.Case]):
    network, test = config.network, config.test
    print(
        f"server {config.server.host}:{config.server.port}, gateway "
        f"{config.gateway.eui.hex()}, DevAddr {config.device.dev_addr:08x}\n"
        f"region {network.region.name}, RX1 delay {network.rx1_delay_s} s offset "
        f"{network.rx1_dr_offset}, RX2 {network.rx2_frequency_hz / 1e6} MHz "
        f"DR{network.rx2_dr}, downlink dwell time {network.downlink_dwell_time}\n"
    )
    for case in cases:
        expect = "ACK" if case.expect_ack else "no ACK"
        role = " (control)" if case.control else ""
        print(f"{case.name:<28} {_describe(case):<30} expect {expect}{role}")
    negatives = any(not case.expect_ack for case in cases)
    rechecks = sum(case.control for case in cases) if negatives else 0
    runs = len(cases) + rechecks
    # An answered packet takes about half a second; others wait for the timeout
    waiting = sum(not case.expect_ack for case in cases) * test.timeout_s
    answering = (runs - sum(not case.expect_ack for case in cases)) * 0.5
    seconds = test.packets * (runs * test.interval_s + waiting + answering)
    print(
        f"\n{len(cases)} cases + {rechecks} control re-check = {runs} runs, "
        f"{runs * test.packets} packets, about {seconds / 60:.0f} min if all behave"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    runner = None
    failed = None
    try:
        config, cases, notices = _prepare(args)
        for notice in notices:
            print(f"note: {notice}")
        if args.dry_run:
            _dry_run(config, cases)
            return EXIT_PASS
        state = State(config.test.state_file, config.device.dev_addr, args.fcnt)
        # Stop cleanly on either signal, also when started in the background
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        packets = config.test.packets

        def packet_line(number: int, total: int, case, packet: int, attempt: Attempt):
            if attempt.acked:
                outcome = f"ACK {attempt.window}"
            elif not attempt.push_acked:
                outcome = "no PUSH_ACK"
            else:
                outcome = "no ACK"
            if attempt.rejected:
                outcome += f" ({'; '.join(attempt.rejected)})"
            print(
                f"  case {number}/{total}, packet {packet}/{packets}: {case.name} "
                f"fcnt {attempt.fcnt}: {outcome}",
                flush=True,
            )

        def show(result: CaseResult):
            expect = "" if result.expect_ack else " [expects no ACK]"
            role = " (control)" if result.control else ""
            print(
                f"{result.verdict:<12} {result.name}  {result.frequency_hz / 1e6:.3f} MHz "
                f"{result.modulation} {result.datr}  {result.detail}{expect}{role}"
            )
            for warning in result.warnings:
                print(f"{'':<12} warning: {warning}")
            sys.stdout.flush()
            if args.json:
                report.write_json(args.json, runner.results)

        statuses: list[bool] = []

        def status_line(stat: dict, acked: bool):
            statuses.append(acked)
            counters = " ".join(f"{k}={v}" for k, v in stat.items() if k != "time")
            outcome = "PUSH_ACK received" if acked else "no PUSH_ACK"
            print(f"  gateway status {stat['time']} ({counters}): {outcome}", flush=True)

        server = config.server
        with Gateway(
            server.host,
            server.port,
            config.gateway.eui,
            config.gateway.keepalive_s,
            config.gateway.stat_interval_s,
            status_line,
        ) as gateway:
            runner = Runner(config, gateway, state)
            try:
                runner.run(cases, show, packet_line)
            finally:
                gateway.finish(config.test.timeout_s)
                if statuses and not any(statuses):
                    print(
                        "warning: no status message was acknowledged, so the "
                        "server may show the gateway as offline"
                    )
    except (configuration.ConfigError, GatewayError, StateError, OSError) as error:
        failed = f"error: {error}"
    except KeyboardInterrupt:
        failed = "interrupted: the results are incomplete"

    # Finished cases are reported even if the run did not complete
    results = runner.results if runner else []
    if results:
        print("\n" + report.summary(results))
        if any(result.verdict in (Verdict.FAIL, Verdict.PARTIAL) for result in results):
            print("\nA missing ACK does not say why: check the network server logs.")
    if failed:
        print(failed, file=sys.stderr)
        return EXIT_ERROR
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
