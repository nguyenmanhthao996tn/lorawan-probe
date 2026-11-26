import json
import os
import signal

import pytest
from helpers import CONTROL, FSK, FakeServer, config_text

from lorawan_probe import cli


def run(tmp_path, server, cases=CONTROL + FSK, extra=()):
    path = tmp_path / "config.toml"
    path.write_text(config_text(server.port, cases))
    report = tmp_path / "report.json"
    code = cli.main(["-c", str(path), "--json", str(report), *extra])
    results = json.loads(report.read_text()) if report.exists() else []
    return code, {result["name"]: result for result in results}


def only_fsk(action):
    return lambda rxpk: action if rxpk["modu"] == "FSK" else "ack"


def test_all_pass(tmp_path):
    with FakeServer() as server:
        code, results = run(tmp_path, server)
    assert code == 0
    assert [r["verdict"] for r in results.values()] == ["PASS", "PASS"]
    assert all(not r["warnings"] for r in results.values())
    assert server.tx_acks[0] == {"txpk_ack": {"error": "NONE"}}


@pytest.mark.parametrize(
    "action, reason",
    [
        ("silent", "0/2 ACK"),
        ("bad_mic", "MIC mismatch"),
        ("no_ack_bit", "ACK bit"),
        ("late", "7.000 s after the uplink"),
    ],
)
def test_fsk_fails(tmp_path, action, reason):
    with FakeServer(only_fsk(action)) as server:
        code, results = run(tmp_path, server)
    assert code == 1
    assert results["control"]["verdict"] == "PASS"
    assert results["fsk"]["verdict"] == "FAIL"
    assert reason in results["fsk"]["detail"]
    assert len(results["fsk"]["attempts"]) == 2


def test_rx2_passes_without_warning(tmp_path):
    with FakeServer(only_fsk("rx2")) as server:
        code, results = run(tmp_path, server)
    assert code == 0
    assert results["fsk"]["detail"] == "2/2 ACK (RX2 2)"
    assert not results["fsk"]["warnings"]


def test_unexpected_radio_parameters_warn(tmp_path):
    with FakeServer(only_fsk("wrong_radio")) as server:
        code, results = run(tmp_path, server)
    assert code == 0
    assert len(results["fsk"]["warnings"]) == 2


@pytest.mark.parametrize("actions", [["silent", "ack", "ack"], ["ack", "ack", "silent"]])
def test_some_packets_lost_is_partial(tmp_path, actions):
    replies = iter(actions)
    control = FSK.replace("[[case]]", "[[case]]\ncontrol = true")
    with FakeServer(lambda rxpk: next(replies)) as server:
        code, results = run(tmp_path, server, control, ["--packets", "3"])
    assert code == 1
    assert results["fsk"]["verdict"] == "PARTIAL" and results["fsk"]["acked"] == 2
    assert [a["fcnt"] for a in results["fsk"]["attempts"]] == [0, 1, 2]
    assert len(server.uplinks) == 3


NEGATIVE = FSK.replace('"fsk"', '"neg"').replace(
    "868800000", '869000000\nexpect = "no_ack"'
)


def negative(action):
    return lambda rxpk: action if rxpk["freq"] == 869.0 else "ack"


@pytest.mark.parametrize(
    "action, verdict, code",
    [
        ("silent", "PASS", 0),
        ("ack", "FAIL", 1),
        ("bad_mic", "INCONCLUSIVE", 2),
        ("no_push_ack", "ERROR", 2),
    ],
)
def test_negative_case(tmp_path, action, verdict, code):
    with FakeServer(negative(action)) as server:
        got, results = run(tmp_path, server, NEGATIVE + CONTROL)
    assert (got, results["neg"]["verdict"]) == (code, verdict)
    # negative results are only final after the control has run again
    assert list(results) == ["control", "control-recheck", "neg"]
    assert [u["freq"] for u in server.uplinks] == [868.1, 868.1, 869.0, 869.0, 868.1, 868.1][
        : len(server.uplinks)
    ]
    assert results["control-recheck"]["recheck"]


def test_negative_case_with_one_missing_push_ack_is_inconclusive(tmp_path):
    actions = iter(["silent", "no_push_ack"])
    behaviour = lambda rxpk: next(actions) if rxpk["freq"] == 869.0 else "ack"
    with FakeServer(behaviour) as server:
        code, results = run(tmp_path, server, CONTROL + NEGATIVE)
    assert results["neg"]["verdict"] == "INCONCLUSIVE"


def test_negative_case_needs_a_successful_recheck(tmp_path):
    controls = iter(["ack", "ack", "silent", "silent"])
    behaviour = lambda rxpk: "silent" if rxpk["freq"] == 869.0 else next(controls)
    with FakeServer(behaviour) as server:
        code, results = run(tmp_path, server, CONTROL + NEGATIVE)
    assert code == 2
    assert results["neg"]["verdict"] == "INCONCLUSIVE"
    assert "re-check" in results["neg"]["detail"]


def test_negative_case_without_control_is_inconclusive(tmp_path):
    with FakeServer(lambda rxpk: "silent") as server:
        code, results = run(tmp_path, server, NEGATIVE)
    assert results["neg"]["verdict"] == "INCONCLUSIVE"


def test_interrupt_still_reports_finished_cases(tmp_path, capsys):
    def behaviour(rxpk):
        if rxpk["freq"] == 868.8:
            os.kill(os.getpid(), signal.SIGINT)
        return "silent" if rxpk["freq"] == 869.0 else "ack"

    with FakeServer(behaviour) as server:
        code, results = run(tmp_path, server, CONTROL + FSK + NEGATIVE)
    assert code == 2
    assert list(results) == ["control"]
    output = capsys.readouterr()
    assert "interrupted" in output.err and "Totals: 1 PASS" in output.out


def test_interrupt_before_recheck_keeps_negative_results(tmp_path):
    seen = []

    def behaviour(rxpk):
        seen.append(rxpk["freq"])
        if seen.count(868.1) == 3:  # first packet of the re-check
            os.kill(os.getpid(), signal.SIGINT)
        return "silent" if rxpk["freq"] == 869.0 else "ack"

    with FakeServer(behaviour) as server:
        code, results = run(tmp_path, server, CONTROL + NEGATIVE)
    assert code == 2
    assert results["neg"]["verdict"] == "INCONCLUSIVE"


def test_failed_control_makes_others_inconclusive(tmp_path):
    with FakeServer(lambda rxpk: "silent") as server:
        # listed after the case under test, but must run first
        code, results = run(tmp_path, server, FSK + CONTROL)
    assert code == 2
    assert list(results) == ["control", "fsk"]
    assert results["control"]["verdict"] == "FAIL"
    assert results["fsk"]["verdict"] == "INCONCLUSIVE"


def test_silence_without_control_is_inconclusive(tmp_path):
    with FakeServer(lambda rxpk: "silent") as server:
        code, results = run(tmp_path, server, FSK)
    assert code == 2
    assert results["fsk"]["verdict"] == "INCONCLUSIVE"


def test_missing_push_ack_is_error(tmp_path):
    with FakeServer(only_fsk("no_push_ack")) as server:
        code, results = run(tmp_path, server)
    assert code == 2
    assert results["fsk"]["verdict"] == "ERROR"


def test_no_pull_ack_stops_the_run(tmp_path, capsys):
    with FakeServer(pull_ack=False) as server:
        code, results = run(tmp_path, server)
    assert code == 2 and not results and not server.uplinks
    assert "no PULL_ACK" in capsys.readouterr().err


def test_frame_counter_persists_and_can_be_overridden(tmp_path):
    with FakeServer() as server:
        run(tmp_path, server)
        _, second = run(tmp_path, server)
        _, third = run(tmp_path, server, extra=["--fcnt", "100"])
    assert second["control"]["attempts"][0]["fcnt"] == 4
    assert third["control"]["attempts"][0]["fcnt"] == 100


def test_case_filter_keeps_controls(tmp_path):
    other = FSK.replace('"fsk"', '"other"')
    with FakeServer() as server:
        _, results = run(tmp_path, server, CONTROL + FSK + other, ["--case", "fsk"])
    assert list(results) == ["control", "fsk"]


def test_corrupt_state_file_is_not_reset(tmp_path, capsys):
    (tmp_path / "config.state.json").write_text("garbage")
    with FakeServer() as server:
        code, _ = run(tmp_path, server)
    assert code == 2 and not server.uplinks
    assert "cannot read" in capsys.readouterr().err
