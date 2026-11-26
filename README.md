# lorawan-probe

Find out which LoRaWAN channels your network server really supports.

`lorawan-probe` emulates a gateway and an end-device in software, sends uplinks
on the channels you choose and checks that the server answers each one. No radio
hardware is needed.

```
lorawan-probe                                      network server
  emulated device  ──  confirmed uplink    ──▶     (Semtech UDP, e.g.
  emulated gateway ◀──  ACK downlink       ──      ChirpStack Gateway Bridge)
```

A packet counts as acknowledged only if the downlink has a valid MIC, has the
ACK bit set and is scheduled in the RX1 or RX2 window of that uplink.

## Features

- Semtech UDP packet forwarder protocol, including gateway status messages
- ABP device, LoRaWAN 1.0.x
- LoRa and FSK uplinks
- Regions: EU868 and AS923-1 to AS923-4
- Builds a full test plan from a ChirpStack v4 region file, including negative
  cases for channels the file does not allow
- Console grid and JSON report

Not supported: OTAA, LoRaWAN 1.1, class B and C, ADR, replies to MAC commands,
LR-FHSS (DR8 and above) and Basics Station.

> Results cover the network server and Gateway Bridge only. They say nothing
> about real gateway radio hardware or concentrator software. Only test servers
> you are allowed to use.

## Install

Requires Python 3.11 or newer.

```sh
git clone https://github.com/nguyenmanhthao996tn/lorawan-probe.git
cd lorawan-probe
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

## Quick start

### 1. Prepare the server

These steps are for ChirpStack v4:

1. Run Gateway Bridge with the Semtech UDP backend (UDP port 1700) and an MQTT
   topic prefix that matches your region.
2. Register a gateway. Use an EUI that no real gateway is using, otherwise the
   server may send downlinks to the wrong one.
3. Create a device profile (your region, LoRaWAN 1.0.x, ABP) and a device.
4. Activate the device and note its DevAddr and session keys.

### 2. Configure

```sh
cp config.example.toml config.toml
```

Edit the server address, gateway EUI, DevAddr, keys and region. Then list the
channels to test, one `[[case]]` each. Mark one channel you know works as the
**control**:

```toml
[[case]]
name = "control-lora"
control = true
frequency_hz = 868100000
modulation = "LORA"
sf = 7
bw_khz = 125

[[case]]
name = "fsk"
frequency_hz = 868800000
modulation = "FSK"
bitrate = 50000
```

`config.toml` holds session keys. It is ignored by git; keep it private.

### 3. Run

```sh
lorawan-probe -c config.toml
```

Example output, shortened, for a server without the FSK channel:

```
  case 1/2, packet 1/10: control-lora fcnt 0: ACK RX1
  ...
PASS         control-lora  868.100 MHz LORA SF7BW125  10/10 ACK (RX1 10) (control)
  ...
FAIL         fsk  868.800 MHz FSK 50000  0/10 ACK

MHz      DR0    DR1    DR2    DR3    DR4    DR5    DR6    DR7
868.1    .      .      .      .      .      10/10  .      .
868.8    .      .      .      .      .      .      .      0/10

Totals: 1 PASS, 1 FAIL
```

By default each case sends 10 packets with a 5 second pause between packets. Use
`--packets 1 --interval 1` for a quick check.

## Test a whole region

Copy the region file your ChirpStack server uses (for example
`region_as923_2.toml`) next to your configuration, set `[network] region` to
match, then preview and run:

```sh
lorawan-probe -c config.toml --chirpstack-region region_as923_2.toml --dry-run
lorawan-probe -c config.toml --chirpstack-region region_as923_2.toml --json report.json
```

- **Channels:** the region's default channels plus
  `regions.network.extra_channels`, each at every datarate it allows up to DR7.
  `regions.gateway.channels` is ignored.
- **Settings:** RX1, RX2 and dwell time values in the region file override
  `[network]`. Each change is printed.
- **Your cases:** a `[[case]]` in the configuration replaces the generated one on
  the same frequency and datarate. Without a control, the first default channel
  at DR5 becomes the control.
- **Negative cases:** an unlisted frequency, FSK on a LoRa channel, LoRa on an
  FSK channel and SF7BW250 on a LoRa channel, whenever the file does not allow
  them. The control runs again afterwards to show the link still works.

A large plan takes a while: 55 runs of 10 packets take about 53 minutes. The
dry run prints an estimate. Region files are ignored by git, as they may contain
broker credentials.

## Reference

### Command line

| Option | Meaning |
|---|---|
| `-c`, `--config FILE` | Configuration file (required) |
| `--chirpstack-region FILE` | Test every channel of a ChirpStack region file |
| `--dry-run` | List the cases and settings, send nothing |
| `--case NAME` | Run only this case (repeatable); control cases always run |
| `--packets N` | Packets per case |
| `--interval S` | Seconds to pause between packets |
| `--fcnt N` | Next uplink frame counter to use |
| `-v`, `--verbose` | Show frames and JSON |
| `--json FILE` | Write the results as JSON, updated after each case |

### Configuration

| Section | Key | Default | Meaning |
|---|---|---|---|
| `[server]` | `host` | required | Server address |
| | `port` | `1700` | Semtech UDP port |
| `[gateway]` | `eui` | required | Gateway EUI, 16 hex characters |
| | `keepalive_s` | `10` | Keepalive interval |
| | `stat_interval_s` | `30` | Status message interval |
| `[device]` | `dev_addr` | required | 8 hex characters |
| | `nwk_skey`, `app_skey` | required | 32 hex characters each |
| | `fport` | `1` | 1 to 223 |
| | `payload` | `"74657374"` | Application payload in hex |
| `[network]` | `region` | required | `EU868`, `AS923-1` to `AS923-4` |
| | `rx1_delay_s` | `1` | Must match the server |
| | `rx1_dr_offset` | `0` | Must match the server |
| | `rx2_frequency_hz`, `rx2_dr` | region default | RX2 window |
| | `downlink_dwell_time` | `false` | AS923 only |
| `[test]` | `timeout_s` | `5` | Wait for each ACK |
| | `packets` | `10` | Packets per case |
| | `interval_s` | `5` | Pause between packets |
| | `state_file` | `<config name>.state.json` | Frame counter storage |
| `[[case]]` | `name`, `frequency_hz`, `modulation` | required | `modulation` is `LORA` or `FSK` |
| | `sf`, `bw_khz` | required for LoRa | |
| | `coding_rate` | `"4/5"` | LoRa only |
| | `bitrate` | required for FSK | Bits per second |
| | `control` | `false` | Known-good channel, runs first |
| | `expect` | `"ack"` | `"no_ack"` makes it a negative case |
| | `rssi`, `chan`, `rfch` | `-50`, `0`, `0` | Reported radio metadata |
| | `snr` | `7.0` | LoRa only |

### Results

| Result | Case expecting an ACK | Negative case |
|---|---|---|
| `PASS` | Every packet acknowledged | No ACK, with a working control before and after |
| `PARTIAL` | Some packets acknowledged | Not used |
| `FAIL` | No packet acknowledged | The server acknowledged it |
| `INCONCLUSIVE` | Nothing acknowledged and the control failed, or there is no control and nothing came back | No working control, an unusable downlink or a missing `PUSH_ACK` |
| `ERROR` | No packet got a `PUSH_ACK` | No packet got a `PUSH_ACK` |

A warning under a result means a downlink used an unexpected frequency or
datarate, or the case does not fit the region's tables.

Exit code: `0` everything passed, `1` at least one `FAIL` or `PARTIAL`, `2` any
`ERROR` or `INCONCLUSIVE`, or an incomplete run. Ctrl-C still reports the
finished cases.

## Notes

**Frame counter.** The next uplink counter is saved beside the configuration
file. If you delete that file or reactivate the device, the server may reject
the uplinks. Use `--fcnt`, or disable frame counter validation for the test
device.

**Gateway status.** A status message is sent after connecting, at every
`stat_interval_s` and before exit. The server shows the gateway online only
while the tool runs, plus about two stats intervals. No location is reported.

**FSK on ChirpStack.** The server needs a network channel for it. In the region
file, add one under `extra_channels` and make sure `max_dr` under
`[regions.network]` is at least 7:

```toml
[[regions.network.extra_channels]]
frequency = 922700000    # use a frequency valid for your region
min_dr = 7
max_dr = 7
```

This setup has been tested with AS923-2 on ChirpStack v4.

## Troubleshooting

| Symptom | Check |
|---|---|
| `no PULL_ACK from the server` | Host, port, firewall (UDP) and that the Semtech UDP backend is running |
| `ERROR`, no `PUSH_ACK` | Packet loss between you and Gateway Bridge |
| Control case fails | Gateway registration, DevAddr and keys, region topic prefix, frame counter |
| `MIC mismatch` | `nwk_skey` differs from the server's |
| "scheduled ... s after the uplink" | `rx1_delay_s` differs from the server's |
| One channel fails | The server's channel list, and its log for that DevAddr |

A missing ACK does not say why. The server logs do.

## Development

```sh
pip install -e '.[dev]'
pytest
```

The tests run against a fake network server on localhost.

| Module | Role |
|---|---|
| `lorawan.py` | Frame building, parsing, encryption and MIC |
| `regions.py` | Regional parameters |
| `gateway.py` | Semtech UDP gateway |
| `runner.py` | Test execution and verdicts |
| `chirpstack.py` | Test plan from a region file |
| `config.py`, `state.py` | Configuration and frame counter |
| `report.py`, `cli.py` | Output and command line |

## Acknowledgements

- Frame cryptography and regional parameters follow
  [LoRaMac-node](https://github.com/Lora-net/LoRaMac-node).
- The gateway protocol follows
  [packet_forwarder](https://github.com/Lora-net/packet_forwarder).
- The uplink test vector comes from
  [lora-packet](https://github.com/anthonykirby/lora-packet).

## License

[MIT](LICENSE)
