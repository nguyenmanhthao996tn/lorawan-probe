import re
import time

from helpers import CONTROL, FakeServer, config_text

from lorawan_probe import cli
from lorawan_probe.gateway import Gateway

EUI = bytes(8)


def run(tmp_path, server, *extra):
    path = tmp_path / "config.toml"
    path.write_text(config_text(server.port, CONTROL))
    return cli.main(["-c", str(path), *extra])


def test_status_is_sent_on_connect_and_before_exit(tmp_path, capsys):
    with FakeServer() as server:
        assert run(tmp_path, server) == 0
    first, last = server.statuses[0], server.statuses[-1]
    assert len(server.statuses) == 2
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d GMT", first.pop("time"))
    assert first == {
        "rxnb": 0, "rxok": 0, "rxfw": 0, "ackr": 0.0, "dwnb": 0, "txnb": 0,
    }
    last.pop("time")
    assert last == {
        "rxnb": 2, "rxok": 2, "rxfw": 2, "ackr": 100.0, "dwnb": 2, "txnb": 2,
    }
    output = capsys.readouterr()
    assert output.out.count("PUSH_ACK received") == 2
    assert "no status message was acknowledged" not in output.out


def test_unacknowledged_status_is_reported(tmp_path, capsys):
    with FakeServer(ack_status=False) as server:
        assert run(tmp_path, server) == 0
    output = capsys.readouterr().out
    assert output.count("no PUSH_ACK") == 2
    assert "no status message was acknowledged" in output


def test_status_repeats_and_counts_each_period():
    with FakeServer(lambda rxpk: "no_push_ack") as server:
        with Gateway(
            "127.0.0.1", server.port, EUI, 10, stat_interval_s=0.2, ack_wait_s=0.1
        ) as gateway:
            gateway.connect(1)
            gateway.push({"tmst": 0})
            gateway.poll(time.monotonic() + 0.5)
    assert len(server.statuses) >= 3
    assert totals(server.statuses) == [(1, 0.0)]  # counted once, in one period


def test_no_status_before_the_server_answers(tmp_path):
    with FakeServer(pull_ack=False) as server:
        assert run(tmp_path, server) == 2
    assert not server.statuses


def totals(statuses):
    return [(s["rxnb"], s["ackr"]) for s in statuses if s["rxnb"]]


def test_uplink_awaiting_its_reply_is_not_reported_as_unacknowledged():
    def slow(rxpk):
        time.sleep(0.4)  # several status intervals pass before the PUSH_ACK
        return "silent"

    with FakeServer(slow) as server:
        with Gateway("127.0.0.1", server.port, EUI, 10, stat_interval_s=0.1) as gateway:
            gateway.connect(1)
            gateway.push({"tmst": 0})
            gateway.poll(time.monotonic() + 0.8)
    assert totals(server.statuses) == [(1, 100.0)]


def test_final_status_waits_for_an_outstanding_reply():
    def slow(rxpk):
        time.sleep(0.2)
        return "silent"

    with FakeServer(slow) as server:
        with Gateway("127.0.0.1", server.port, EUI, 10) as gateway:
            gateway.connect(1)
            gateway.push({"tmst": 0})
            gateway.finish(1)
    assert totals(server.statuses) == [(1, 100.0)]
