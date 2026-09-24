"""Tests for aprs-reporter. Standard library only: python3 -m unittest -v"""

import io
import os
import tempfile
import unittest
from datetime import datetime, timezone

import aprs_reporter as ar

# All golden packets are built against this instant.
FIXED_TIME = datetime(2026, 9, 24, 13, 15, 42, tzinfo=timezone.utc)

EXAMPLE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aprs.toml.example")


class PasscodeTest(unittest.TestCase):
    def test_known_callsign(self):
        # A fixed vector, so any change to the hash shows up here. The
        # algorithm itself was confirmed during development by logging in to
        # a real APRS-IS server and getting back "verified" rather than
        # "unverified".
        self.assertEqual(ar.passcode_for("N0CALL"), 13023)

    def test_ssid_is_ignored(self):
        self.assertEqual(ar.passcode_for("N0CALL-10"), ar.passcode_for("N0CALL"))

    def test_case_is_ignored(self):
        self.assertEqual(ar.passcode_for("n0call"), ar.passcode_for("N0CALL"))

    def test_always_in_range(self):
        for call in ("N0CALL", "W1AW", "VK2ABC", "G0RDH", "JA1XYZ", "LA1B"):
            with self.subTest(call=call):
                self.assertTrue(0 <= ar.passcode_for(call) <= 0x7FFF)


class CoordinateTest(unittest.TestCase):
    def test_latitude(self):
        cases = [
            (56.9282, "5655.69N"),
            (24.1674, "2410.04N"),
            (0.0, "0000.00N"),
            (-33.8688, "3352.13S"),
            (-0.5, "0030.00S"),
            (90.0, "9000.00N"),
        ]
        for degrees, want in cases:
            with self.subTest(degrees=degrees):
                self.assertEqual(ar.format_lat(degrees), want)

    def test_longitude(self):
        cases = [
            (24.1674, "02410.04E"),
            (56.9282, "05655.69E"),
            (0.0, "00000.00E"),
            (-0.5, "00030.00W"),
            (179.9999, "17959.99E"),
            (-122.4194, "12225.16W"),
        ]
        for degrees, want in cases:
            with self.subTest(degrees=degrees):
                self.assertEqual(ar.format_lon(degrees), want)

    def test_minutes_carry_into_degrees(self):
        """59.999 minutes must become the next degree, not '5660.00N'."""
        self.assertEqual(ar.format_lat(56.999999), "5700.00N")
        self.assertEqual(ar.format_lat(-56.999999), "5700.00S")
        self.assertEqual(ar.format_lon(24.9999999), "02500.00E")
        self.assertEqual(ar.format_lon(-24.9999999), "02500.00W")

    def test_four_decimal_places(self):
        """The precision a hand-written config realistically carries."""
        positions = [
            (52.3740, 4.8897, "5222.44N", "00453.38E"),
            (52.0907, 5.1214, "5205.44N", "00507.28E"),
            (51.9244, 4.4777, "5155.46N", "00428.66E"),
            (57.0138, 24.9572, "5700.83N", "02457.43E"),
        ]
        for lat, lon, want_lat, want_lon in positions:
            with self.subTest(lat=lat):
                self.assertEqual(ar.format_lat(lat), want_lat)
                self.assertEqual(ar.format_lon(lon), want_lon)


class PHGTest(unittest.TestCase):
    def test_typical_repeater_sites(self):
        """Hand-computed. Power is stored as sqrt, height as log2 of feet.

        These are the values a real VHF/UHF repeater config produces, and
        they are what Dire Wolf emits for the same inputs - which is what
        makes them worth pinning.
        """
        cases = [
            (10, 40, "E", "PHG3402"),
            (15, 15, None, "PHG4200"),
            (20, 15, None, "PHG4200"),
            (20, 25, None, "PHG4300"),
        ]
        for power, height_m, direction, want in cases:
            with self.subTest(power=power, height_m=height_m):
                feet = height_m / ar.METRES_PER_FOOT
                self.assertEqual(ar.phg(power, feet, 0, direction), want)

    def test_exact_encodings(self):
        # Powers that are exact squares, heights that are exact powers of two.
        self.assertEqual(ar.phg(9, 10, 0, None), "PHG3000")
        self.assertEqual(ar.phg(25, 20, 0, None), "PHG5100")
        self.assertEqual(ar.phg(81, 40, 0, None), "PHG9200")
        self.assertEqual(ar.phg(0, 0, 0, None), "PHG0000")

    def test_digits_clamp(self):
        self.assertEqual(ar.phg(500, 10, 0, None), "PHG9000")
        self.assertEqual(ar.phg(10, 100000, 0, None), "PHG3900")
        self.assertEqual(ar.phg(10, 10, 15, None), "PHG3090")

    def test_height_below_ten_feet_clamps_to_zero(self):
        """log2 of a fraction is negative; the digit must not go below 0."""
        self.assertEqual(ar.phg(10, 3, 0, None), "PHG3000")

    def test_compass_points(self):
        cases = [
            ("N", 8), ("NE", 1), ("E", 2), ("SE", 3),
            ("S", 4), ("SW", 5), ("W", 6), ("NW", 7),
        ]
        for direction, digit in cases:
            with self.subTest(direction=direction):
                self.assertEqual(ar.directivity(direction), digit)

    def test_north_is_eight_not_zero(self):
        """0 means omnidirectional, so north has to be 360 degrees."""
        self.assertEqual(ar.directivity("N"), 8)
        self.assertEqual(ar.directivity("360"), 8)
        self.assertEqual(ar.directivity(None), 0)
        self.assertEqual(ar.directivity("omni"), 0)

    def test_case_and_whitespace_ignored(self):
        self.assertEqual(ar.directivity(" se "), 3)
        self.assertEqual(ar.directivity("Nw"), 7)

    def test_bearings_in_degrees(self):
        self.assertEqual(ar.directivity(270), 6)
        self.assertEqual(ar.directivity("45"), 1)

    def test_rejects_what_phg_cannot_express(self):
        for bad in ("NNE", "23", "400", "-45", "sideways", "0.5"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ar.directivity(bad)


class PacketTest(unittest.TestCase):
    def test_object_report_golden(self):
        """The exact bytes of a fully-specified object report.

        Note the PHG field abutting both the symbol code and the comment: it
        is a fixed seven characters, not a space-delimited token.
        """
        obj = ar.Obj(
            name="N0CALL-3", lat=52.3740, lon=4.8897, symbol="/r",
            power=10, height_ft=40 / ar.METRES_PER_FOOT, direction="E",
            comment="DMR BrandMeister 438.400 -7.6 MHz TG24706",
            has_phg=True,
        )
        want = (
            ";N0CALL-3 *241315z5222.44N/00453.38ErPHG3402"
            "DMR BrandMeister 438.400 -7.6 MHz TG24706"
        )
        self.assertEqual(ar.object_report(obj, FIXED_TIME), want)

    def test_object_name_is_padded_to_nine(self):
        """'*' must land in the same column whatever the name's length."""
        for name in ("A", "N0CALL-3", "N0CALL-11", "ABCDEFGHI"):
            with self.subTest(name=name):
                obj = ar.Obj(name=name, lat=56.9282, lon=24.1674)
                info = ar.object_report(obj, FIXED_TIME)
                self.assertEqual(info[10], "*", info)
                self.assertTrue(info.startswith(";" + name))

    def test_no_phg_without_phg_keys(self):
        obj = ar.Obj(name="X", lat=56.9282, lon=24.1674, comment="hi")
        self.assertEqual(
            ar.object_report(obj, FIXED_TIME),
            ";X        *241315z5655.69N/02410.04Erhi",
        )

    def test_position_report(self):
        station = ar.Station(lat=56.9282, lon=24.1674, symbol="/-", comment="N0CALL")
        self.assertEqual(ar.position_report(station), "!5655.69N/02410.04E-N0CALL")

    def test_frame(self):
        self.assertEqual(
            ar.frame("N0CALL", "APZ001", ";X        *241315z"),
            "N0CALL>APZ001,TCPIP*:;X        *241315z",
        )

    def test_full_golden_packet(self):
        config = ar.load_config(EXAMPLE_CONFIG)
        frames = ar.build_frames(config, FIXED_TIME)
        want = (
            "N0CALL>APZ001,TCPIP*:;N0CALL-1 *241315z5222.44N/00453.38ErPHG5361"
            "Example repeater 145.750 -0.6 MHz"
        )
        self.assertEqual(frames[0][1], want)


def write_config(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    handle.write(text)
    handle.close()
    return handle.name


class ExampleConfigTest(unittest.TestCase):
    """The shipped example must stay loadable and produce the right packets.

    It ships with placeholder credentials, so this also pins the property that
    the example carries no real callsign or passcode.
    """

    def setUp(self):
        self.config = ar.load_config(EXAMPLE_CONFIG)

    def test_aprsis_section(self):
        self.assertEqual(self.config.server, "euro.aprs2.net")
        self.assertEqual(self.config.port, 14580)
        self.assertEqual(self.config.callsign, "N0CALL")
        self.assertEqual(self.config.tocall, "APZ001")

    def test_ships_the_placeholder_callsign(self):
        """The example must never ship a real station's callsign."""
        self.assertEqual(self.config.callsign, "N0CALL")
        for obj in self.config.objects:
            with self.subTest(name=obj.name):
                self.assertTrue(obj.name.startswith("N0CALL-"), obj.name)

    def test_example_passcode_matches_its_callsign(self):
        """Otherwise the example would not even load, let alone work."""
        self.assertEqual(self.config.passcode, str(ar.passcode_for(self.config.callsign)))

    def test_objects(self):
        names = [obj.name for obj in self.config.objects]
        self.assertEqual(names, ["N0CALL-1", "N0CALL-2", "N0CALL-3"])

    def test_both_height_units_are_demonstrated(self):
        """30 m, then 100 ft, then an object with no height at all."""
        feet = [round(obj.height_ft) for obj in self.config.objects]
        self.assertEqual(feet, [98, 100, 0])

    def test_example_never_uses_a_bare_height_key(self):
        """Prose may mention it; no key may set an unnamed unit."""
        with open(EXAMPLE_CONFIG, encoding="utf-8") as handle:
            for line in handle:
                code = line.split("#", 1)[0].strip()
                self.assertFalse(
                    code.startswith("height ") or code.startswith("height="),
                    f"example assigns a height without naming the unit: {line!r}",
                )

    def test_all_packets(self):
        frames = ar.build_frames(self.config, FIXED_TIME)
        self.assertEqual(len(frames), 3)
        want = [
            ";N0CALL-1 *241315z5222.44N/00453.38ErPHG5361",
            ";N0CALL-2 *241315z5205.44N/00507.28ErPHG3300",
            ";N0CALL-3 *241315z5155.46N/00428.66Er",
        ]
        for (_label, text), prefix in zip(frames, want):
            with self.subTest(prefix=prefix):
                self.assertTrue(
                    text.startswith("N0CALL>APZ001,TCPIP*:" + prefix),
                    f"\n got {text}\nwant prefix {prefix}",
                )

    def test_object_without_antenna_keys_has_no_extension(self):
        """N0CALL-3 sets none of power/height/gain/dir, so no PHG."""
        self.assertEqual(self.config.objects[2].extension(), "")

    def test_every_packet_fits(self):
        for label, text in ar.build_frames(self.config, FIXED_TIME):
            with self.subTest(label=label):
                self.assertLessEqual(len(text.encode()), ar.MAX_FRAME)

    def test_example_comments_are_within_the_guidance(self):
        """The example should model good practice, so it raises no advisories."""
        self.assertEqual(ar.advisories(self.config), [])


VALID = """
[aprsis]
server = "euro.aprs2.net"
callsign = "N0CALL"
passcode = "13023"

[[object]]
name = "N0CALL-3"
lat = 56.9282
lon = 24.1674
symbol = "/r"
"""


class ConfigValidationTest(unittest.TestCase):
    def parse(self, text):
        import tomllib
        return ar.parse_config(tomllib.loads(text))

    def problems(self, text):
        with self.assertRaises(ar.ConfigError) as caught:
            self.parse(text)
        return "\n".join(caught.exception.problems)

    def test_valid_config_parses(self):
        config = self.parse(VALID)
        self.assertEqual(config.callsign, "N0CALL")
        self.assertEqual(config.port, ar.DEFAULT_PORT)
        self.assertEqual(config.tocall, ar.DEFAULT_TOCALL)
        self.assertIsNone(config.station)
        self.assertEqual(len(config.objects), 1)

    def test_wrong_passcode_is_caught_before_connecting(self):
        problems = self.problems(VALID.replace('"13023"', '"12345"'))
        self.assertIn("does not match callsign N0CALL", problems)
        self.assertIn("expected 13023", problems)

    def test_passcode_may_be_a_bare_number(self):
        config = self.parse(VALID.replace('passcode = "13023"', "passcode = 13023"))
        self.assertEqual(config.passcode, "13023")

    def test_receive_only_passcode_rejected(self):
        problems = self.problems(VALID.replace('"13023"', '"-1"'))
        self.assertIn("receive-only", problems)

    def test_unknown_key_is_an_error(self):
        problems = self.problems(VALID + "\npowre = 10\n")
        self.assertIn("unknown key 'powre'", problems)

    def test_unknown_key_suggests_the_real_one(self):
        problems = self.problems(VALID + "\ncommet = \"typo\"\n")
        self.assertIn("did you mean 'comment'", problems)

    def test_unknown_section_is_an_error(self):
        problems = self.problems(VALID + "\n[nonsense]\nx = 1\n")
        self.assertIn("unknown top-level section 'nonsense'", problems)

    def test_height_accepts_either_unit(self):
        """Internally always feet, because that is what PHG encodes."""
        metres = self.parse(VALID + "\nheight_m = 40\n")
        feet = self.parse(VALID + "\nheight_ft = 131\n")
        self.assertAlmostEqual(metres.objects[0].height_ft, 131.23, places=1)
        self.assertEqual(feet.objects[0].height_ft, 131.0)
        # 40 m and 131 ft are the same tower, so they must encode alike.
        self.assertEqual(metres.objects[0].extension(), feet.objects[0].extension())

    def test_both_height_units_rejected(self):
        problems = self.problems(VALID + "\nheight_m = 40\nheight_ft = 131\n")
        self.assertIn("not both", problems)

    def test_bare_height_rejected(self):
        """'height' alone is the ambiguity that naming the unit prevents."""
        problems = self.problems(VALID + "\nheight = 40\n")
        self.assertIn("name the unit", problems)

    def test_bare_height_does_not_also_say_unknown_key(self):
        """One mistake should produce one error, not two."""
        problems = self.problems(VALID + "\nheight = 40\n")
        self.assertNotIn("unknown key", problems)

    def test_negative_height_ft_rejected(self):
        self.assertIn("cannot be negative", self.problems(VALID + "\nheight_ft = -5\n"))

    def test_power_is_watts(self):
        """25 W is a perfect square, so it survives the encoding exactly."""
        config = self.parse(VALID + "\npower = 25\n")
        self.assertEqual(config.objects[0].power, 25)
        self.assertTrue(config.objects[0].extension().startswith("PHG5"))

    def test_negative_height_rejected(self):
        self.assertIn("cannot be negative", self.problems(VALID + "\nheight_m = -5\n"))

    def test_negative_power_rejected(self):
        self.assertIn("cannot be negative", self.problems(VALID + "\npower = -5\n"))

    def test_bad_direction_reported(self):
        problems = self.problems(VALID + '\ndir = "NNE"\n')
        self.assertIn("dir", problems)
        self.assertIn("NNE", problems)

    def test_object_name_too_long(self):
        problems = self.problems(VALID.replace('"N0CALL-3"', '"N0CALL-1234"'))
        self.assertIn("the APRS limit is 9", problems)

    def test_coordinates_out_of_range(self):
        problems = self.problems(VALID.replace("56.9282", "91.0"))
        self.assertIn("lat 91.0 is out of range", problems)

    def test_symbol_must_be_two_characters(self):
        problems = self.problems(VALID.replace('"/r"', '"r"'))
        self.assertIn("exactly two characters", problems)

    def test_duplicate_object_names(self):
        doubled = VALID + """
[[object]]
name = "N0CALL-3"
lat = 57.0
lon = 24.0
"""
        problems = self.problems(doubled)
        self.assertIn("duplicate", problems)

    def test_missing_required_keys(self):
        problems = self.problems("""
[aprsis]
server = "euro.aprs2.net"

[[object]]
name = "X"
""")
        self.assertIn("callsign is required", problems)
        self.assertIn("passcode is required", problems)
        self.assertIn("lat is required", problems)
        self.assertIn("lon is required", problems)

    def test_nothing_to_report(self):
        problems = self.problems("""
[aprsis]
server = "euro.aprs2.net"
callsign = "N0CALL"
passcode = "13023"
""")
        self.assertIn("nothing to report", problems)

    def test_missing_aprsis_section(self):
        problems = self.problems('[[object]]\nname = "X"\nlat = 1\nlon = 2\n')
        self.assertIn("missing [aprsis] section", problems)

    def test_all_problems_reported_at_once(self):
        """A hand-edited config usually has more than one thing wrong."""
        broken = """
[aprsis]
server = "euro.aprs2.net"
callsign = "N0CALL"
passcode = "99999"

[[object]]
name = "WAY-TOO-LONG-NAME"
lat = 91.0
lon = 24.1674
symbol = "x"
dir = "NNE"
height_m = 40
height_ft = 131
bogus = true
"""
        with self.assertRaises(ar.ConfigError) as caught:
            self.parse(broken)
        problems = caught.exception.problems
        self.assertGreaterEqual(len(problems), 7, problems)
        joined = "\n".join(problems)
        for expected in ("passcode", "APRS limit is 9", "out of range",
                         "exactly two characters", "NNE", "not both", "unknown key"):
            with self.subTest(expected=expected):
                self.assertIn(expected, joined)

    def test_station_block(self):
        config = self.parse(VALID + """
[station]
lat = 56.9282
lon = 24.1674
symbol = "/-"
comment = "N0CALL"
""")
        self.assertIsNotNone(config.station)
        frames = ar.build_frames(config, FIXED_TIME)
        # The station's own position comes first, then the objects.
        self.assertEqual(frames[0][1], "N0CALL>APZ001,TCPIP*:!5655.69N/02410.04E-N0CALL")
        self.assertEqual(len(frames), 2)

    def test_invalid_toml(self):
        path = write_config("[aprsis\nserver = ")
        try:
            with self.assertRaises(ar.ConfigError) as caught:
                ar.load_config(path)
            self.assertIn("not valid TOML", caught.exception.problems[0])
        finally:
            os.unlink(path)


class AdvisoryTest(unittest.TestCase):
    """Comments longer than the spec recommends are reported, never blocked."""

    def test_long_object_comment(self):
        config = ar.Config(server="x", port=1, callsign="N0CALL", passcode="13023")
        config.objects.append(ar.Obj(name="N0CALL-1", lat=52.0, lon=4.0, comment="x" * 64))
        notes = ar.advisories(config)
        self.assertEqual(len(notes), 1)
        self.assertIn("64 characters", notes[0])
        self.assertIn("still be sent", notes[0])

    def test_long_station_comment(self):
        config = ar.Config(server="x", port=1, callsign="N0CALL", passcode="13023")
        config.station = ar.Station(lat=52.0, lon=4.0, comment="y" * 50)
        notes = ar.advisories(config)
        self.assertEqual(len(notes), 1)
        self.assertIn("[station]", notes[0])

    def test_comment_at_the_limit_is_silent(self):
        config = ar.Config(server="x", port=1, callsign="N0CALL", passcode="13023")
        config.objects.append(
            ar.Obj(name="N0CALL-1", lat=52.0, lon=4.0, comment="x" * ar.COMMENT_GUIDANCE)
        )
        self.assertEqual(ar.advisories(config), [])


# A config whose comment exceeds the spec guidance, for the advisory tests.
ADVISORY_CONFIG = """
[aprsis]
server = "euro.aprs2.net"
callsign = "N0CALL"
passcode = "13023"

[[object]]
name = "N0CALL-1"
lat = 52.3740
lon = 4.8897
symbol = "/r"
comment = "%s"
""" % ("x" * 64)


class CommandLineTest(unittest.TestCase):
    def run_main(self, argv):
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ar.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_prints_packets_and_succeeds(self):
        code, out, _err = self.run_main(["--dry-run", EXAMPLE_CONFIG])
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertTrue(line.startswith("N0CALL>APZ001,TCPIP*:;N0CALL-"), line)

    def test_clean_config_dry_run_is_note_free(self):
        _code, _out, err = self.run_main(["--dry-run", EXAMPLE_CONFIG])
        self.assertEqual(err, "")

    def test_dry_run_opens_no_socket(self):
        """--dry-run must work with networking removed entirely."""
        import socket as socket_module
        saved = socket_module.socket
        socket_module.socket = None  # any use would raise TypeError
        try:
            code, _out, _err = self.run_main(["--dry-run", EXAMPLE_CONFIG])
        finally:
            socket_module.socket = saved
        self.assertEqual(code, 0)

    def test_bad_config_exits_two(self):
        path = write_config(VALID.replace('"13023"', '"12345"'))
        try:
            code, out, err = self.run_main([path])
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertIn("does not match callsign", err)
        finally:
            os.unlink(path)

    def test_missing_file_exits_two(self):
        code, _out, err = self.run_main(["/nonexistent/aprs.toml"])
        self.assertEqual(code, 2)
        self.assertIn("No such file", err)

    def test_packets_and_notes_go_to_different_streams(self):
        """'--dry-run > packets.txt' must capture packets and nothing else."""
        path = write_config(ADVISORY_CONFIG)
        try:
            code, out, err = self.run_main(["--dry-run", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        self.assertIn("note:", err)
        self.assertNotIn("note:", out)
        for line in out.strip().splitlines():
            self.assertTrue(line.startswith("N0CALL>"), line)

    def test_advisories_shown_by_default(self):
        """Verbose is the default; notes need no flag to appear."""
        path = write_config(ADVISORY_CONFIG)
        try:
            code, _out, err = self.run_main(["--dry-run", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        self.assertIn("note:", err)

    def test_silent_suppresses_advisories(self):
        path = write_config(ADVISORY_CONFIG)
        try:
            code, out, err = self.run_main(["--dry-run", "-s", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        # stdout is unaffected: --dry-run exists to print packets.
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_silent_does_not_suppress_errors(self):
        """-s quiets progress, never failures. Cron must still hear about them."""
        path = write_config(VALID.replace('"13023"', '"12345"'))
        try:
            code, out, err = self.run_main(["-s", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("does not match callsign", err)

    def test_verbose_flag_is_gone(self):
        """-v was replaced by -s; it must fail loudly, not be ignored."""
        with self.assertRaises(SystemExit):
            self.run_main(["--dry-run", "-v", EXAMPLE_CONFIG])


if __name__ == "__main__":
    unittest.main()
