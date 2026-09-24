# aprs-reporter

Send fixed positions to an APRS-IS server, once, then exit. Built to be run from cron.

Written to replace Dire Wolf on stations that use it purely as an APRS-IS object
beaconer — no soundcard, no TNC, no RF. Such a config gives itself away at a glance:
`ADEVICE null null`, `AGWPORT 0`, `KISSPORT 0`, and a handful of
`OBEACON ... sendto=IG` lines. An entire software modem stack held resident to open one
TCP socket every ten minutes.

This is one Python file with no dependencies. Nothing stays resident, and there is no
scheduler to get wrong — cron already solves the only timing problem there is.

## Requirements

**Python 3.11 or later, and nothing else.** Every import is standard library, so there
is no venv, no `requirements.txt` and no `pip` step on the target host.

Parsing TOML is the only thing that needs more than Python 3.7 — `tomllib` joined the
standard library in 3.11. On an older Python, install the `tomli` backport and the script
picks it up automatically:

```sh
sudo apt install python3-tomli      # Debian, Ubuntu; most distros package it as this
```

Use the package manager rather than `pip`. Current distributions mark the system Python
as externally managed ([PEP 668](https://peps.python.org/pep-0668/)), so a system-wide
`pip install` is refused. Inside a virtualenv, `pip install tomli` is fine.

If neither is available the script says so and exits 2, rather than failing with a
traceback.

## Install

```sh
install -m 755 aprs_reporter.py /usr/local/bin/aprs-reporter
install -m 600 aprs.toml.example /etc/aprs-reporter.toml
$EDITOR /etc/aprs-reporter.toml
```

Mode 600 on the config matters: it holds your APRS-IS passcode.

## Usage

```
aprs-reporter [options] CONFIG

  -n, --dry-run    print the packets that would be sent; open no socket
  -s, --silent     suppress progress and advisory output; errors still reported
      --timeout S  socket timeout in seconds (default: 30)
      --version
```

Check what would go on the air before anything does:

```sh
aprs-reporter --dry-run /etc/aprs-reporter.toml
```

```
N0CALL>APZ001,TCPIP*:;N0CALL-1 *241315z5222.44N/00453.38ErPHG5361Example repeater 145.750 -0.6 MHz
N0CALL>APZ001,TCPIP*:;N0CALL-2 *241315z5205.44N/00507.28ErPHG3300Example repeater 430.250 +1.6 MHz
N0CALL>APZ001,TCPIP*:;N0CALL-3 *241315z5155.46N/00428.66Er
```

Packets go to stdout and notes to stderr, so `--dry-run > packets.txt` captures the
packets alone.

### Exit status

| code | meaning |
|---|---|
| 0 | everything sent |
| 1 | network or send failure |
| 2 | cannot run: bad usage, an invalid configuration, or a missing TOML parser |

**By default it reports what it is doing**, on stderr — each packet sent, the server's
replies, and any advisories. Run it by hand and you see the whole exchange.

Cron mails whatever a job writes to stdout or stderr, so **add `-s` to the crontab
entry**. Errors are printed regardless of `-s`, so a silent run still mails you when
something is actually wrong.

## Cron

```cron
MAILTO=you@example.org
*/10 * * * * /usr/local/bin/aprs-reporter -s /etc/aprs-reporter.toml
```

**Note the `-s`.** Without it every run mails you its packet list. With it you hear only
about failures.

Ten minutes matches Dire Wolf's `every=10:00` and is a sensible cadence for fixed
objects. Cron fires every host on the exact minute with no jitter, which is fine at this
volume; the server-address shuffle spreads the load across the rotate anyway.

## Configuration

See [aprs.toml.example](aprs.toml.example) for a complete, commented file.

### `[aprsis]` — required

| key | required | meaning |
|---|---|---|
| `server` | yes | APRS-IS hostname. Rotates like `euro.aprs2.net` are preferred. |
| `port` | no | Default 14580. |
| `callsign` | yes | The login, and the source address of every packet. |
| `passcode` | yes | APRS-IS passcode for that callsign. String or bare number. |
| `tocall` | no | Identifies this software. Default `APZ001`. |

### `[station]` — optional

Your own position, sent as a position report. Omit the block to send nothing for
yourself. Keys: `lat`, `lon`, `symbol`, `comment`.

### `[[object]]` — one block per object

| key | required | meaning |
|---|---|---|
| `name` | yes | Object name, at most 9 characters. |
| `lat`, `lon` | yes | Decimal degrees. Negative is south / west. |
| `symbol` | no | Table character then symbol code, e.g. `"/r"`. Default `"/r"`. |
| `power` | no | **Watts.** |
| `height_m` / `height_ft` | no | Height above average terrain, in **metres** or **feet**. Give one, never both. |
| `gain` | no | Antenna gain in **dB**. |
| `dir` | no | Compass point or bearing. Omit for omnidirectional. |
| `comment` | no | Free text. |

Any one of `power`, `height_m`, `height_ft`, `gain` or `dir` present emits a PHG
extension.

**Power is always watts. Height may be either unit, but the key must name it** — a bare
`height` is refused. Dire Wolf's `HEIGHT` is feet with nothing saying so, and that
silence is the whole trap: `131` there means a 40 m tower, and a key called `height`
gives you no way to tell which you are looking at.

**Unknown keys are errors, not warnings.** In a format nothing validates, `powre = 10`
silently drops your PHG and you find out from aprs.fi. The script rejects the run and
suggests the key you probably meant.

## Migrating from direwolf.conf

| direwolf.conf | aprs.toml |
|---|---|
| `MYCALL`, `IGLOGIN` callsign | `callsign` |
| `IGSERVER host [port]` | `server`, `port` |
| `IGLOGIN` passcode | `passcode` |
| `OBEACON objname=` | `[[object]]` `name` |
| `lat=`, `long=` | `lat`, `lon` |
| `symbol=` | `symbol` |
| `POWER=` (watts) | `power` (watts) |
| `HEIGHT=` (feet, unmarked) | `height_ft` — copies across unchanged |
| `DIR=` | `dir` |
| `comment=` | `comment` |
| `PBEACON` | `[station]` |
| `delay=`, `every=` | the cron schedule |
| `sendto=IG` | implicit — there is nowhere else to send |
| `ADEVICE`, `MODEM`, `PTT`, ... | nothing; no hardware is used |

**Watch the height when you migrate.** Dire Wolf's `HEIGHT` is in feet with nothing
saying so, which is how the reference config ends up reading `131`, `49`, `82` for towers
that are 40 m, 15 m and 25 m. Put those numbers in `height_ft` and they carry over as
they stand; put them in `height_m` and you have claimed towers three times too tall.

| direwolf `HEIGHT` | `height_ft` | or `height_m` |
|---|---|---|
| 131 | 131 | 40 |
| 49 | 49 | 15 |
| 82 | 82 | 25 |

`POWER` needs no such care — Dire Wolf's is watts too.

`TBEACON` has no equivalent and never will — it needs a GPS receiver.

## How things are encoded

- **Object reports** are `;` + a 9-character padded name + `*` + a real `DDHHMMz` UTC
  timestamp + position + optional PHG + comment. The PHG extension abuts the symbol code
  with no separator; it is a fixed-width field, not a space-delimited token.
- **PHG is coarse by design.** Power is stored as its square root and height as a power
  of two, each in a single digit. 10 W transmits as 9 W, 40 m as 160 ft. That is the
  format working correctly, and it is what Dire Wolf sends for the same inputs.
- **The passcode is checked locally before connecting.** It is a published hash of the
  callsign, so there is exactly one valid value. A wrong one produces an *unverified*
  login, and an unverified login accepts every packet you send and discards the lot —
  a failure that otherwise looks exactly like success.
- **The connection is closed gracefully:** half-close, then drain. A one-shot sender that
  calls `close()` straight after its last write can lose packets still in the kernel send
  buffer and exit 0 having transmitted nothing.

`tocall` defaults to `APZ001`, in the experimental range where software without a
registered APRS device identifier belongs. If this ever gets published, register one at
[aprsorg/aprs-deviceid](https://github.com/aprsorg/aprs-deviceid).

## Tests

```sh
python3 -m unittest -v
```

No test dependencies either. The suite pins the exact bytes of every packet the example
config produces, so a change to what goes on the air has to be a deliberate one.

## Before you run it for real

**Stop Dire Wolf first.** Two programs beaconing the same object names means duplicate
objects with competing positions on the map.

Then send once and check the result:

```sh
aprs-reporter /etc/aprs-reporter.toml
```

and look for a fresh timestamp at `https://aprs.fi/#!call=` plus one of your object names.

Keep your real `aprs.toml` out of version control — it has the passcode in it. Commit
`aprs.toml.example` instead.
