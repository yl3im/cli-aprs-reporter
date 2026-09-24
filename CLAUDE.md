# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A one-shot CLI that reads a TOML config, connects to an APRS-IS server, transmits the
beacons the config describes, and exits. Run from cron. It replaces Dire Wolf on stations
using it purely as an APRS-IS object beaconer — no soundcard, no TNC, no RF, no receiving.

Two source files: `aprs_reporter.py` (the whole tool) and `test_aprs_reporter.py`.

## Commands

```sh
python3 -m unittest                              # whole suite (72 tests, <1s)
python3 -m unittest -v                           # with test names
python3 -m unittest test_aprs_reporter.PHGTest   # one class
python3 -m unittest test_aprs_reporter.PHGTest.test_compass_points   # one test

./aprs_reporter.py --dry-run aprs.toml.example   # encode and print, open no socket
./aprs_reporter.py --dry-run -s aprs.toml.example  # packets only, no commentary
```

There is no build, no linter config, no CI. `--dry-run` is the main development loop:
it exercises config loading and the whole encoder without touching the network.

To verify the no-dependency claim, run under `-I -S` so `site-packages` leaves `sys.path`:

```sh
python3 -I -S -m unittest
python3 -I -S aprs_reporter.py --dry-run aprs.toml.example
```

## Hard constraints

**Standard library only.** No third-party imports in either file — that includes tests,
which is why they use `unittest` rather than pytest. This is the reason Python was chosen
over Go for this job: one file, `install -m 755`, nothing to install on a repeater host.
Adding a dependency defeats the point. The single conditional import of `tomli` is a
backport shim for Python < 3.11 and is not a dependency on 3.11+.

**Python 3.11+ is the floor, and only because of `tomllib`.** Everything else is
3.7-clean. Do not introduce `match`, walrus, `removeprefix`, or `dict |` without a reason —
they would raise the floor for nothing.

## Architecture

`aprs_reporter.py` is sectioned by comment banners, in dependency order:

1. **Passcode** — `passcode_for()`, the published APRS-IS callsign hash.
2. **Encoding** — `format_lat/format_lon`, `directivity`, `phg`. Pure functions, no I/O.
3. **Configuration** — `Obj`/`Station`/`Config` dataclasses, `parse_config`, `load_config`.
4. **Packet assembly** — `object_report`, `position_report`, `frame`, `build_frames`.
5. **APRS-IS** — `_login`, `_drain_and_close`, `send_frames`.
6. **CLI** — `build_parser`, `main`.

Sections 1–4 are pure and fully testable offline; that is what makes `--dry-run` and the
golden tests possible. Only section 5 touches a socket.

### Validation accumulates, it does not short-circuit

Every `_parse_*` helper takes a `problems: list[str]` and appends to it rather than
raising. `parse_config` raises one `ConfigError` carrying all of them at the end. A
hand-edited config usually has more than one thing wrong, and reporting them one per run
is miserable. Preserve this when adding validation — append, don't raise.

Each message is prefixed with its location (`object[2] "N0CALL-3": ...`) via the `where`
string that every helper receives.

### Unknown keys are errors

`_check_unknown_keys` rejects anything outside `OBJECT_KEYS` / `STATION_KEYS` /
`APRSIS_KEYS`, with a prefix-match spelling suggestion from `_closest`. In a config format
nothing validates, a typo like `powre = 10` would silently drop the PHG extension and you
would learn about it from aprs.fi. `REJECTED_HEIGHT_KEYS` holds keys that are recognised
*only* to produce a better message than "unknown key".

### Units

`power` is watts and `gain` is dB, exactly as entered. Height is the exception: the config
takes `height_m` **or** `height_ft` (never both, and never a bare `height`), and
`_resolve_height` converts to feet on the way in because **PHG encodes feet**.
`Obj.height_ft` therefore always holds feet regardless of what the config said.

## APRS details that are easy to get wrong

These are all pinned by tests. If a test here fails, suspect the change, not the test.

- **The PHG extension abuts** the symbol code and the comment with no separating spaces.
  It is a fixed 7-character field, not a space-delimited token.
- **PHG is lossy by design.** Power is stored as `round(sqrt(watts))`, height as
  `round(log2(feet/10))`; 10 W transmits as 9 W. That is the format working, not a bug.
- **Directivity: north is 8, not 0**, because 0 means omnidirectional.
- **Object names pad to exactly 9 characters** so `*` always lands in column 11.
- **Minutes rounding must carry into degrees.** `_degrees_minutes` rounds before
  formatting so 56.999999 yields `5700.00N`, never an impossible `5660.00N`. It uses
  `_round_half_up` rather than `round()` because banker's rounding would send neighbouring
  coordinates in different directions.
- Object reports carry a real `DDHHMMz` UTC timestamp generated at send time.

## Network behaviour that is not obvious

- **Test `unverified` before `verified`** in `_login` — one contains the other as a
  substring. Getting it backwards means reporting success while the server discards every
  packet, which is the single nastiest failure mode this tool has.
- **`passcode_for` runs as a pre-flight check** in `_parse_aprsis`, so a wrong passcode is
  caught before a socket opens rather than becoming a silent unverified login.
- **`_drain_and_close` half-closes then reads to EOF.** A one-shot sender that calls
  `close()` right after its last write can lose packets still in the kernel send buffer
  and exit 0 having transmitted nothing. A daemon never hits this; this program would.
- **The shuffled address list is the entire retry strategy.** `euro.aprs2.net` is a
  rotate. There is no timed retry loop because cron will run the job again. Retries stop
  once login succeeds — re-sending a partly-delivered batch to another server would
  duplicate objects.
- An overall `signal.alarm` guard stops a wedged socket accumulating cron processes.

## Output contract

Verbose by default, on stderr. `-s` suppresses progress and advisories but **never
errors**, so it is safe in a crontab. `--dry-run` writes packets to stdout and everything
else to stderr, so `--dry-run > packets.txt` captures packets alone.

Exit codes: `0` sent, `1` network or send failure, `2` cannot run (bad usage, invalid
config, or no TOML parser).

## Testing conventions

Golden tests pin the **exact bytes** of every packet the example config produces, against
a fixed `FIXED_TIME`. Changing them should be a deliberate, reviewed act — they are the
record of what goes on the air.

`ExampleConfigTest` loads `aprs.toml.example` directly, so edits to the example must keep
the tests in step. Note that its `passcode` must remain the true hash of its `callsign`
(`N0CALL` → `13023`) or the file will not load at all.

## Care when running against the live network

Anything without `--dry-run` transmits to the real APRS network under the configured
callsign. Use `--dry-run` for development. Before a real send, Dire Wolf must be stopped
if it is beaconing the same object names, or both sources compete on the map.

`aprs.toml` is gitignored because it holds the passcode; `aprs.toml.example` is the
committed one and must never carry a real callsign or passcode.
