#!/usr/bin/env python3
"""Report fixed positions to an APRS-IS server, once, then exit.

Written for stations that use Dire Wolf purely as an APRS-IS object beaconer:
no soundcard, no TNC, no RF. Run it from cron; it connects, logs in, sends the
beacons described by its configuration file, and exits.

Reports what it is doing on stderr. Under cron, pass -s so that a routine run
mails nothing; errors are still reported either way, and set the exit status:

    0   everything sent
    1   network or send failure
    2   cannot run: bad usage, invalid configuration, or no TOML parser
"""

from __future__ import annotations

import argparse
import math
import random
import signal
import socket
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        # Parsing TOML is the only thing here that needs more than Python 3.7.
        # apt/dnf are listed before pip on purpose: PEP 668 makes distributions
        # mark the system Python as externally managed, so a plain
        # "pip install" into it fails on anything current.
        _v = sys.version_info
        sys.stderr.write(
            f"aprs-reporter: this Python ({_v.major}.{_v.minor}) has no TOML parser.\n"
            "tomllib is built in from Python 3.11. On an older one, install the\n"
            "tomli backport and this script will pick it up automatically:\n"
            "\n"
            "  Debian/Ubuntu    sudo apt install python3-tomli\n"
            "  most other distros package it as python3-tomli too\n"
            "  otherwise        pip install tomli, inside a virtualenv\n"
            "\n"
            "A system-wide 'pip install' is refused by PEP 668 on current\n"
            "distributions; use the package manager or a virtualenv.\n"
        )
        raise SystemExit(2)

VERSION = "1.0.0"

DEFAULT_PORT = 14580

# APZ is the experimental range, which is where software without a registered
# APRS device identifier belongs. See github.com/aprsorg/aprs-deviceid.
DEFAULT_TOCALL = "APZ001"

# The longest line an APRS-IS server accepts, including the TNC2 header.
MAX_FRAME = 512

# Comment length the APRS spec recommends staying within. Advisory only.
COMMENT_GUIDANCE = 43

METRES_PER_FOOT = 0.3048


class ConfigError(Exception):
    """One or more problems with the configuration file.

    Carries every problem found rather than just the first, because a
    hand-edited config usually has more than one thing wrong with it.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(problems))


class SendError(Exception):
    """The beacons could not be delivered."""


class LoginRejected(SendError):
    """The server refused the callsign and passcode."""


# --------------------------------------------------------------------------
# APRS-IS passcode
# --------------------------------------------------------------------------


def passcode_for(callsign: str) -> int:
    """Return the APRS-IS passcode for a callsign.

    The passcode is a published hash of the callsign, so there is exactly one
    valid value for any given call. That makes it worth checking locally: a
    wrong passcode produces an 'unverified' login, and an unverified login
    accepts every packet you send it and quietly discards the lot.
    """
    call = callsign.split("-")[0].upper()
    code = 0x73E2
    for i, ch in enumerate(call):
        code ^= ord(ch) << (8 if i % 2 == 0 else 0)
    return code & 0x7FFF


# --------------------------------------------------------------------------
# Position and PHG encoding - pure functions, no I/O
# --------------------------------------------------------------------------


def _round_half_up(value: float) -> int:
    """Round halves away from zero.

    Python's built-in round() rounds halves to even, which would make
    neighbouring coordinates round in different directions. Values reaching
    here are already non-negative.
    """
    return int(math.floor(value + 0.5))


def _degrees_minutes(deg: float) -> tuple[int, float]:
    """Split non-negative decimal degrees into degrees and rounded minutes.

    The rounding happens here rather than in a format specifier so that
    minutes reaching 60.00 carry into the degrees, instead of producing an
    impossible string like '5660.00N'.
    """
    d = int(deg)
    hundredths = _round_half_up((deg - d) * 6000)
    if hundredths >= 6000:
        d += 1
        hundredths -= 6000
    return d, hundredths / 100


def format_lat(deg: float) -> str:
    """Format decimal degrees as APRS DDMM.MMh: 56.9282 -> '5655.69N'."""
    hemi = "N" if deg >= 0 else "S"
    d, m = _degrees_minutes(abs(deg))
    return f"{d:02d}{m:05.2f}{hemi}"


def format_lon(deg: float) -> str:
    """Format decimal degrees as APRS DDDMM.MMh: 24.1674 -> '02410.04E'."""
    hemi = "E" if deg >= 0 else "W"
    d, m = _degrees_minutes(abs(deg))
    return f"{d:03d}{m:05.2f}{hemi}"


# North is 360 rather than 0, because 0 means 'omnidirectional'.
COMPASS = {"NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315, "N": 360}


def directivity(direction: str | int | None) -> int:
    """Return the PHG directivity digit for a compass point or bearing.

    Raises ValueError for anything the four-digit PHG encoding cannot express.
    """
    if direction is None:
        return 0
    text = str(direction).strip().upper()
    if text in ("", "0", "OMNI"):
        return 0
    if text in COMPASS:
        return COMPASS[text] // 45
    try:
        degrees = int(text)
    except ValueError:
        raise ValueError(
            f"{direction!r} is neither a compass point (N, NE, E, ...) nor a bearing in degrees"
        ) from None
    if not 0 < degrees <= 360:
        raise ValueError(f"{degrees} is out of range; use 1-360 degrees, or omit for omnidirectional")
    if degrees % 45:
        raise ValueError(f"{degrees} cannot be encoded; PHG directivity has only 45-degree steps")
    return degrees // 45


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def phg(power: int = 0, height_ft: float = 0.0, gain: int = 0,
        direction: str | int | None = None) -> str:
    """Build the seven-character PHGphgd data extension.

    power is watts and gain is dB, both as the operator entered them. height
    is the one exception: PHG is defined in feet, so a config giving metres
    has already been converted before it reaches here.

    Each figure is squeezed into one digit, so the encoding is coarse by
    design: power is stored as its square root and height as a power of two.
    10 W becomes digit 3, which decodes back to 9 W. That is the format, not a
    rounding bug, and it is what Dire Wolf transmits for the same inputs.
    """
    p = _clamp(_round_half_up(math.sqrt(power)), 0, 9) if power > 0 else 0
    h = _clamp(_round_half_up(math.log2(height_ft / 10)), 0, 9) if height_ft > 0 else 0
    g = _clamp(int(gain), 0, 9)
    d = directivity(direction)
    return f"PHG{p}{h}{g}{d}"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class Obj:
    """One object beaconed on another station's behalf."""

    name: str
    lat: float
    lon: float
    symbol: str = "/r"
    power: int = 0  # watts, as entered
    # Always feet, because that is what PHG encodes. The config may give
    # metres; _resolve_height() converts on the way in.
    height_ft: float = 0.0
    gain: int = 0  # dB, as entered
    direction: str | int | None = None
    comment: str = ""
    has_phg: bool = False

    def extension(self) -> str:
        if not self.has_phg:
            return ""
        return phg(self.power, self.height_ft, self.gain, self.direction)


@dataclass
class Station:
    """This station's own position."""

    lat: float
    lon: float
    symbol: str = "/-"
    comment: str = ""


@dataclass
class Config:
    server: str
    port: int
    callsign: str
    passcode: str
    tocall: str = DEFAULT_TOCALL
    station: Station | None = None
    objects: list[Obj] = field(default_factory=list)


APRSIS_KEYS = {"server", "port", "callsign", "passcode", "tocall"}
STATION_KEYS = {"lat", "lon", "symbol", "comment"}
OBJECT_KEYS = {"name", "lat", "lon", "symbol", "power", "height_m", "height_ft",
               "gain", "dir", "comment"}

# Not a valid key. Listed so that _resolve_height can ask for the unit,
# instead of the generic "unknown key" path firing first and saying less.
REJECTED_HEIGHT_KEYS = {"height"}


def _check_unknown_keys(table: dict, allowed: set[str], where: str, problems: list[str]) -> None:
    """Reject keys nobody recognises.

    A typo'd key in a format nothing validates silently changes what goes on
    the air - 'powre = 10' would drop the PHG extension and say nothing.
    """
    for key in table:
        if key not in allowed:
            suggestion = _closest(key, allowed)
            hint = f"; did you mean {suggestion!r}?" if suggestion else ""
            problems.append(f"{where}: unknown key {key!r}{hint}")


def _closest(word: str, candidates: set[str]) -> str | None:
    """Return the candidate sharing the most leading characters, if any."""
    best, best_score = None, 0
    for candidate in candidates:
        score = len(_common_prefix(word.lower(), candidate.lower()))
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 3 else None


def _common_prefix(a: str, b: str) -> str:
    out = []
    for x, y in zip(a, b):
        if x != y:
            break
        out.append(x)
    return "".join(out)


def _number(table: dict, key: str, where: str, problems: list[str],
            required: bool = False, default: float = 0.0) -> float:
    value = table.get(key)
    if value is None:
        if required:
            problems.append(f"{where}: {key} is required")
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(f"{where}: {key} must be a number, not {type(value).__name__}")
        return default
    return float(value)


def _text(table: dict, key: str, where: str, problems: list[str],
          required: bool = False, default: str = "") -> str:
    value = table.get(key)
    if value is None:
        if required:
            problems.append(f"{where}: {key} is required")
        return default
    if not isinstance(value, str):
        problems.append(f"{where}: {key} must be a string, not {type(value).__name__}")
        return default
    return value


def _coords(table: dict, where: str, problems: list[str]) -> tuple[float, float]:
    lat = _number(table, "lat", where, problems, required=True)
    lon = _number(table, "lon", where, problems, required=True)
    if "lat" in table and not -90 <= lat <= 90:
        problems.append(f"{where}: lat {lat} is out of range (-90 to 90)")
    if "lon" in table and not -180 <= lon <= 180:
        problems.append(f"{where}: lon {lon} is out of range (-180 to 180)")
    return lat, lon


def _symbol(table: dict, where: str, problems: list[str], default: str) -> str:
    symbol = _text(table, "symbol", where, problems, default=default)
    if len(symbol) != 2:
        problems.append(
            f"{where}: symbol {symbol!r} must be exactly two characters - "
            'a table character (/ or \\ or an overlay) then a symbol code, e.g. "/r"'
        )
        return default
    table_char, code = symbol[0], symbol[1]
    if table_char not in "/\\" and not table_char.isalnum():
        problems.append(
            f"{where}: symbol table {table_char!r} must be /, \\, or an overlay letter or digit"
        )
    if not 0x21 <= ord(code) <= 0x7E:
        problems.append(f"{where}: symbol code {code!r} is not a printable character")
    return symbol


def _resolve_height(table: dict, where: str, problems: list[str]) -> float:
    """Resolve the height to feet, which is the unit PHG encodes.

    Either unit may be given - height_m or height_ft - but the unit must be
    named. Dire Wolf's HEIGHT is feet with nothing to say so, which is how a
    config ends up reading 131 for a tower that is 40 metres tall. A bare
    'height' key would reopen exactly that ambiguity, so it is refused.
    """
    if "height" in table:
        problems.append(
            f"{where}: name the unit - use height_m for metres, or height_ft for feet"
        )
        return 0.0

    has_m, has_ft = "height_m" in table, "height_ft" in table
    if has_m and has_ft:
        problems.append(f"{where}: give height_m or height_ft, not both")
        return 0.0

    if has_m:
        metres = _number(table, "height_m", where, problems)
        if metres < 0:
            problems.append(f"{where}: height_m {metres} cannot be negative")
            return 0.0
        return metres / METRES_PER_FOOT

    if has_ft:
        feet = _number(table, "height_ft", where, problems)
        if feet < 0:
            problems.append(f"{where}: height_ft {feet} cannot be negative")
            return 0.0
        return feet

    return 0.0


def _parse_aprsis(table: dict, problems: list[str]) -> Config:
    where = "[aprsis]"
    _check_unknown_keys(table, APRSIS_KEYS, where, problems)

    server = _text(table, "server", where, problems, required=True)
    callsign = _text(table, "callsign", where, problems, required=True).upper()
    tocall = _text(table, "tocall", where, problems, default=DEFAULT_TOCALL).upper()

    port = int(_number(table, "port", where, problems, default=DEFAULT_PORT))
    if not 0 < port < 65536:
        problems.append(f"{where}: port {port} is out of range")
        port = DEFAULT_PORT

    # TOML allows the passcode as a bare number; accept either spelling.
    raw_passcode = table.get("passcode")
    if raw_passcode is None:
        problems.append(f"{where}: passcode is required")
        passcode = ""
    elif isinstance(raw_passcode, bool) or not isinstance(raw_passcode, (str, int)):
        problems.append(f"{where}: passcode must be a string or a number")
        passcode = ""
    else:
        passcode = str(raw_passcode).strip()

    if passcode == "-1":
        problems.append(
            f"{where}: passcode -1 is receive-only; this tool only transmits, "
            "so nothing would reach the network"
        )
    elif passcode and callsign:
        expected = passcode_for(callsign)
        if passcode != str(expected):
            problems.append(
                f"{where}: passcode {passcode} does not match callsign {callsign} "
                f"(expected {expected}). An incorrect passcode logs in 'unverified', "
                "which accepts every packet and discards all of them."
            )

    return Config(server=server, port=port, callsign=callsign, passcode=passcode, tocall=tocall)


def _parse_station(table: dict, problems: list[str]) -> Station:
    where = "[station]"
    _check_unknown_keys(table, STATION_KEYS, where, problems)
    lat, lon = _coords(table, where, problems)
    return Station(
        lat=lat,
        lon=lon,
        symbol=_symbol(table, where, problems, default="/-"),
        comment=_text(table, "comment", where, problems),
    )


def _parse_object(index: int, table: dict, problems: list[str]) -> Obj:
    name = _text(table, "name", f"object[{index}]", problems, required=True)
    where = f'object[{index}] "{name}"' if name else f"object[{index}]"

    _check_unknown_keys(table, OBJECT_KEYS | REJECTED_HEIGHT_KEYS, where, problems)

    if name and len(name) > 9:
        problems.append(f"{where}: name is {len(name)} characters; the APRS limit is 9")

    lat, lon = _coords(table, where, problems)

    power = int(_number(table, "power", where, problems))
    if power < 0:
        problems.append(f"{where}: power {power} cannot be negative")
        power = 0

    gain = int(_number(table, "gain", where, problems))
    if gain < 0:
        problems.append(f"{where}: gain {gain} cannot be negative")
        gain = 0

    height_ft = _resolve_height(table, where, problems)

    direction = table.get("dir")
    if direction is not None:
        try:
            directivity(direction)
        except ValueError as exc:
            problems.append(f"{where}: dir {exc}")
            direction = None

    return Obj(
        name=name,
        lat=lat,
        lon=lon,
        symbol=_symbol(table, where, problems, default="/r"),
        power=power,
        height_ft=height_ft,
        gain=gain,
        direction=direction,
        comment=_text(table, "comment", where, problems),
        has_phg=any(k in table for k in ("power", "height_m", "height_ft", "gain", "dir")),
    )


def parse_config(data: dict) -> Config:
    """Turn parsed TOML into a Config, reporting every problem at once."""
    problems: list[str] = []

    for key in data:
        if key not in ("aprsis", "station", "object"):
            problems.append(f"unknown top-level section {key!r}")

    aprsis = data.get("aprsis")
    if not isinstance(aprsis, dict):
        problems.append("missing [aprsis] section: nowhere to report to")
        raise ConfigError(problems)

    config = _parse_aprsis(aprsis, problems)

    station = data.get("station")
    if station is not None:
        if isinstance(station, dict):
            config.station = _parse_station(station, problems)
        else:
            problems.append("[station] must be a table")

    objects = data.get("object", [])
    if not isinstance(objects, list):
        problems.append("[[object]] entries must be tables")
        objects = []
    for index, table in enumerate(objects):
        if not isinstance(table, dict):
            problems.append(f"object[{index}] is not a table")
            continue
        config.objects.append(_parse_object(index, table, problems))

    seen: dict[str, int] = {}
    for index, obj in enumerate(config.objects):
        if obj.name in seen:
            problems.append(
                f'object[{index}] "{obj.name}": duplicate of object[{seen[obj.name]}]; '
                "the later one would overwrite the earlier on the map"
            )
        seen[obj.name] = index

    if config.station is None and not config.objects:
        problems.append("nothing to report: add a [station] block or at least one [[object]]")

    if problems:
        raise ConfigError(problems)
    return config


def load_config(path: str) -> Config:
    with open(path, "rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError([f"not valid TOML: {exc}"]) from None
    return parse_config(data)


# --------------------------------------------------------------------------
# Packet assembly
# --------------------------------------------------------------------------


def object_report(obj: Obj, now: datetime) -> str:
    """Build an APRS object report.

    Format: ';' + a nine-character name + '*' + DDHHMMz + position + optional
    seven-character extension + comment. The name is padded so that '*' always
    lands in the same column, and the extension abuts the symbol code with no
    separator - it is a fixed-width field, not a space-delimited token.
    """
    return (
        f";{obj.name:<9.9}*{now.strftime('%d%H%Mz')}"
        f"{format_lat(obj.lat)}{obj.symbol[0]}"
        f"{format_lon(obj.lon)}{obj.symbol[1]}"
        f"{obj.extension()}{obj.comment}"
    )


def position_report(station: Station) -> str:
    """Build a position report without a timestamp.

    '!' says this station cannot receive APRS messages, which is true: this
    tool never reads the feed.
    """
    return (
        f"!{format_lat(station.lat)}{station.symbol[0]}"
        f"{format_lon(station.lon)}{station.symbol[1]}"
        f"{station.comment}"
    )


def frame(source: str, tocall: str, info: str) -> str:
    """Wrap an information field in the TNC2 header APRS-IS expects.

    TCPIP* marks the packet as having entered the network over the internet
    rather than off the air.
    """
    return f"{source}>{tocall},TCPIP*:{info}"


def build_frames(config: Config, now: datetime) -> list[tuple[str, str]]:
    """Return (label, frame) for everything the config describes."""
    frames: list[tuple[str, str]] = []
    if config.station is not None:
        frames.append((
            f"station {config.callsign}",
            frame(config.callsign, config.tocall, position_report(config.station)),
        ))
    for obj in config.objects:
        frames.append((
            f"object {obj.name}",
            frame(config.callsign, config.tocall, object_report(obj, now)),
        ))
    return frames


def advisories(config: Config) -> list[str]:
    """Non-fatal observations, printed unless -s was given."""
    notes = []
    for obj in config.objects:
        if len(obj.comment) > COMMENT_GUIDANCE:
            notes.append(
                f'object "{obj.name}": comment is {len(obj.comment)} characters; the APRS '
                f"spec recommends {COMMENT_GUIDANCE}. It will still be sent."
            )
    if config.station is not None and len(config.station.comment) > COMMENT_GUIDANCE:
        notes.append(
            f"[station]: comment is {len(config.station.comment)} characters; the APRS "
            f"spec recommends {COMMENT_GUIDANCE}. It will still be sent."
        )
    return notes


# --------------------------------------------------------------------------
# APRS-IS
# --------------------------------------------------------------------------


def _login(sock: socket.socket, config: Config, log):
    """Log in, confirm the server verified us, and return the read side."""
    reader = sock.makefile("rb")

    banner = reader.readline().decode("utf-8", "replace").strip()
    if banner:
        log(f"server: {banner}")

    line = f"user {config.callsign} pass {config.passcode} vers aprs-reporter {VERSION}\r\n"
    sock.sendall(line.encode("utf-8"))

    while True:
        raw = reader.readline()
        if not raw:
            raise SendError("server closed the connection during login")
        response = raw.decode("utf-8", "replace").strip()
        if not response.startswith("#"):
            continue
        log(f"server: {response}")
        if "logresp" not in response:
            continue
        # Test for 'unverified' first: it contains 'verified' as a substring,
        # and getting this backwards means reporting success while the server
        # silently drops every packet.
        if "unverified" in response or "verified" not in response:
            raise LoginRejected(
                f"APRS-IS refused the login: {response} "
                "(check the callsign and passcode in [aprsis])"
            )
        return reader


def _drain_and_close(sock: socket.socket, reader, log, timeout: float = 2.0) -> None:
    """Half-close the connection and read until the server hangs up.

    A one-shot sender that calls close() straight after its last write can
    lose packets still sitting in the kernel send buffer, and exit successfully
    having transmitted nothing. Shutting down the write side flushes the data
    and tells the server we are done; draining the read side gives it a chance
    to answer before the socket disappears.
    """
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        return
    sock.settimeout(timeout)
    try:
        while True:
            raw = reader.readline()
            if not raw:
                return
            text = raw.decode("utf-8", "replace").strip()
            if text:
                log(f"server: {text}")
    except (TimeoutError, OSError):
        return


def send_frames(config: Config, frames: list[tuple[str, str]], timeout: float, log) -> str:
    """Connect, log in, send everything, and close cleanly.

    Returns the address actually used. Raises SendError on failure.

    A rotate hostname such as euro.aprs2.net resolves to many servers; trying
    them in random order spreads load and routes around a single sick one.
    That address list is the whole retry strategy - cron will run this again
    soon enough without a retry loop here.
    """
    try:
        addresses = socket.getaddrinfo(
            config.server, config.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError as exc:
        raise SendError(f"resolving {config.server}: {exc}") from None
    if not addresses:
        raise SendError(f"resolving {config.server}: no addresses")
    random.shuffle(addresses)

    last_error: Exception | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        host = sockaddr[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
        except OSError as exc:
            log(f"connect {host}: {exc}")
            last_error = exc
            sock.close()
            continue

        try:
            reader = _login(sock, config, log)
        except LoginRejected:
            # Every server in the rotate will reach the same conclusion.
            sock.close()
            raise
        except OSError as exc:
            log(f"login at {host}: {exc}")
            last_error = exc
            sock.close()
            continue

        # Logged in. A failure from here on is reported rather than retried
        # against another server: some packets may already have been
        # delivered, and re-sending the whole batch elsewhere would duplicate
        # them.
        try:
            for label, text in frames:
                encoded = text.encode("utf-8")
                if len(encoded) > MAX_FRAME:
                    raise SendError(
                        f"{label}: packet is {len(encoded)} bytes; APRS-IS accepts at most {MAX_FRAME}"
                    )
                sock.sendall(encoded + b"\r\n")
                log(f"sent {text}")
            _drain_and_close(sock, reader, log)
        except OSError as exc:
            raise SendError(f"sending to {host}: {exc}") from None
        finally:
            sock.close()

        return host

    raise SendError(f"could not reach {config.server}:{config.port}: {last_error}")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _install_alarm(seconds: int) -> None:
    """Guard the whole run, so a wedged socket cannot pile up cron processes."""
    if not hasattr(signal, "SIGALRM"):
        return

    def on_alarm(_signum, _frame):
        raise SendError(f"gave up after {seconds}s")

    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aprs-reporter",
        description="Send the beacons described by CONFIG to an APRS-IS server, once.",
        epilog=(
            "Progress goes to stderr; -s suppresses it so cron mails nothing on a\n"
            "routine run. Errors are reported either way.\n"
            "Exit status: 0 sent, 1 network or send failure, 2 cannot run.\n\n"
            "Examples:\n"
            "  aprs-reporter --dry-run aprs.toml    # print the packets, send nothing\n"
            "  aprs-reporter aprs.toml              # send, showing each packet\n"
            "  */10 * * * * /usr/local/bin/aprs-reporter -s /etc/aprs-reporter.toml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", metavar="CONFIG", help="path to the TOML configuration file")
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="print the packets that would be sent; open no socket",
    )
    parser.add_argument(
        "-s", "--silent", action="store_true",
        help="suppress progress and advisory output; errors are still reported",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="socket timeout in seconds (default: 30)",
    )
    parser.add_argument("--version", action="version", version=f"aprs-reporter {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def log(message: str) -> None:
        if not args.silent:
            print(message, file=sys.stderr)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"aprs-reporter: {args.config}:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"aprs-reporter: {exc}", file=sys.stderr)
        return 2

    frames = build_frames(config, datetime.now(timezone.utc))

    # Advisories go to stderr so that --dry-run's stdout stays pure packets.
    # Under cron, -s keeps them from being mailed every ten minutes forever.
    if not args.silent:
        for note in advisories(config):
            print(f"note: {note}", file=sys.stderr)

    if args.dry_run:
        for _label, text in frames:
            print(text)
        return 0

    _install_alarm(int(args.timeout * 2) + 10)
    try:
        host = send_frames(config, frames, args.timeout, log)
    except SendError as exc:
        print(f"aprs-reporter: {exc}", file=sys.stderr)
        return 1
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

    log(f"sent {len(frames)} packet(s) via {host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
