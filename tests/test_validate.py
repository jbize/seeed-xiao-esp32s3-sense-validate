"""Checks for the stream parser and the good / bad / unclear report."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import validate


def jpeg(width: int, height: int, filler: bytes = b"") -> bytes:
    """A tiny baseline JPEG with a real start-of-frame marker."""
    sof = (
        b"\xff\xc0\x00\x0b\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + filler + b"\xff\xd9"


def metric(sensor_us: int, copy_us: int, wifi_us: int, rssi: int = -48) -> bytes:
    return f"metric sensor_us={sensor_us} copy_us={copy_us} wifi_us={wifi_us} rssi={rssi}\n".encode()


BOARD = (
    "board mac=68:EE:8F:5C:27:E4\n"
    "board rssi=-48\n"
    "board clock_mhz=21\n"
    "board frame=XGA\n"
    "board quality=30\n"
    "board brightness=1\n"
    "board saturation=-2\n"
    "board mirror=0\n"
    "board flip=1\n"
    "board night=0\n"
).encode()


def feed_all(payload: bytes, chunk: int = 1) -> validate.StreamAssembler:
    assembler = validate.StreamAssembler()
    for start in range(0, len(payload), chunk):
        assembler.feed(payload[start : start + chunk])
    return assembler


def pictures(count: int, sensor_us: int, copy_us: int, wifi_us: int, width: int = 1024, height: int = 768) -> list[validate.Picture]:
    body = jpeg(width, height, b"\nboard fake=1\n")
    return [
        validate.Picture(data=body, sensor_us=sensor_us, copy_us=copy_us, wifi_us=wifi_us, rssi=-48)
        for _ in range(count)
    ]


class StreamTests(unittest.TestCase):
    def test_byte_at_a_time_keeps_jpeg_newlines_out_of_the_board_lines(self) -> None:
        payload = b"HTTP/1.1 200 OK\r\n\r\n" + BOARD
        payload += b"--frame\r\n" + jpeg(1024, 768, b"\nboard fake=1\n\xff") + metric(4000, 800, 28000)
        payload += b"--frame\r\n" + jpeg(1024, 768) + metric(5000, 900, 30000, rssi=-50)
        assembler = feed_all(payload)
        self.assertEqual(assembler.board["mac"], "68:EE:8F:5C:27:E4")
        self.assertNotIn("fake", assembler.board)
        self.assertEqual(len(assembler.pictures), 2)
        self.assertEqual(validate.jpeg_dimensions(assembler.pictures[0].data), (1024, 768))
        self.assertEqual(assembler.pictures[0].wifi_us, 28000)
        self.assertEqual(assembler.pictures[1].rssi, -50)

    def test_copy_failure_is_a_picture_with_no_jpeg(self) -> None:
        assembler = feed_all(BOARD + metric(1000, -1, 0))
        self.assertEqual(len(assembler.pictures), 1)
        self.assertEqual(assembler.pictures[0].data, b"")
        self.assertEqual(assembler.pictures[0].copy_us, -1)

    def test_ready_url(self) -> None:
        self.assertEqual(validate.ready_url("validator\nready http://192.168.1.97\n"), "http://192.168.1.97")
        self.assertIsNone(validate.ready_url("wifi failed\n"))
        self.assertEqual(validate.stream_url("http://192.168.1.97/"), "http://192.168.1.97/stream")
        self.assertEqual(validate.stream_url("http://192.168.1.97/stream"), "http://192.168.1.97/stream")
        self.assertEqual(validate.stream_url("192.168.1.97"), "http://192.168.1.97/stream")

    def test_ten_seconds_is_the_default_and_thirty_is_accepted(self) -> None:
        self.assertEqual(validate.parse_args([]).seconds, 10.0)
        self.assertEqual(validate.parse_args(["--seconds", "30"]).seconds, 30.0)


class ReportTests(unittest.TestCase):
    board = {
        "mac": "68:EE:8F:5C:27:E4",
        "rssi": "-48",
        "clock_mhz": "21",
        "quality": "30",
        "brightness": "1",
        "saturation": "-2",
        "mirror": "0",
        "flip": "1",
        "night": "0",
    }

    def report(self, frames: list[validate.Picture], elapsed: float = 10.0, closed_early: bool = False) -> validate.Report:
        return validate.build_report(frames, self.board, elapsed, 10.0, closed_early)

    def test_good_board(self) -> None:
        # 300 frames, one of them slower, median still the fast send.
        frames = pictures(299, 4000, 1000, 28000)
        frames.append(validate.Picture(data=frames[0].data, sensor_us=4000, copy_us=1000, wifi_us=41000, rssi=-48))
        report = self.report(frames)
        self.assertEqual(report.verdict, validate.Verdict.GOOD)
        self.assertIn("Result: GOOD", report.text)
        self.assertIn("every JPEG measured 1024×768", report.text)
        self.assertIn("30.0 frames/second", report.text)
        self.assertIn("Sensor: 4 ms median", report.text)
        self.assertIn("Copy: 1 ms median", report.text)
        self.assertIn("Wi-Fi: 28 ms fastest, 28 ms average, 41 ms slowest", report.text)
        self.assertIn("The OV3660 built the JPEG", report.text)
        self.assertIn("internal memory", report.text)
        self.assertIn("out the antenna", report.text)

    def test_stalls_fail_the_average_even_when_the_middle_send_is_fast(self) -> None:
        frames = pictures(8, 2000, 1000, 27_000)
        frames.extend(pictures(7, 2000, 1000, 1_000_000))
        report = self.report(frames)
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertIn("ESP32-S3 Wi-Fi or the antenna parts", report.text)
        self.assertIn("27 ms fastest", report.text)
        self.assertIn("1000 ms slowest", report.text)

    def test_slow_wifi_names_the_radio_or_the_antenna_parts(self) -> None:
        report = self.report(pictures(40, 2000, 1500, 98_000))
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertIn("ESP32-S3 Wi-Fi or the antenna parts", report.text)
        self.assertIn("98 ms", report.text)
        self.assertIn("-48 dBm", report.text)

    def test_slow_copy_names_picture_memory(self) -> None:
        report = self.report(pictures(100, 4000, 40_000, 20_000))
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertEqual(report.part, "picture memory in the ESP32-S3 package")

    def test_slow_sensor_names_the_ov3660_or_the_connector(self) -> None:
        report = self.report(pictures(40, 200_000, 1000, 20_000))
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertEqual(report.part, "OV3660 or the connector")

    def test_worst_stage_is_named_when_two_are_past_their_limits(self) -> None:
        # Copy is 40/15 = 2.7 times its limit. Wi-Fi is 98/80 = 1.2 times its limit.
        report = self.report(pictures(40, 4000, 40_000, 98_000))
        self.assertEqual(report.part, "picture memory in the ESP32-S3 package")
        self.assertIn("Also past its limit: ESP32-S3 Wi-Fi", report.text)

    def test_wrong_size_does_not_name_a_part(self) -> None:
        report = self.report(pictures(20, 4000, 1000, 20_000, width=640, height=480))
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertEqual(report.part, "")
        self.assertIn("640×480", report.text)
        self.assertIn("does not name a part", report.text)

    def test_low_rate_with_fast_stages_stays_bad_without_a_part(self) -> None:
        report = self.report(pictures(40, 20_000, 1000, 40_000))
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertEqual(report.part, "")
        self.assertIn("None of the three stages crossed its own limit", report.text)

    def test_gap_between_the_good_and_bad_lines_is_unclear(self) -> None:
        report = self.report(pictures(90, 20_000, 1000, 60_000))
        self.assertEqual(report.verdict, validate.Verdict.UNCLEAR)
        self.assertIn("between the good and bad lines", report.text)

    def test_a_short_stream_is_not_good(self) -> None:
        report = self.report(pictures(30, 4000, 1000, 20_000), elapsed=1.0, closed_early=True)
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertIn("closed after", report.text)

    def test_a_trailing_jpeg_without_times_is_dropped(self) -> None:
        frames = pictures(120, 4000, 1000, 28_000)
        frames.append(validate.Picture(data=frames[0].data))
        report = self.report(frames)
        self.assertEqual(report.verdict, validate.Verdict.GOOD)
        self.assertIn("Frames: 120", report.text)

    def test_no_jpeg_is_bad(self) -> None:
        report = self.report([])
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertIn("No JPEG arrived", report.text)

    def test_allocation_failure_names_internal_memory(self) -> None:
        report = self.report([validate.Picture(data=b"", sensor_us=1000, copy_us=-1, wifi_us=0, rssi=-48)])
        self.assertEqual(report.verdict, validate.Verdict.BAD)
        self.assertEqual(report.part, "internal memory in the ESP32-S3")


if __name__ == "__main__":
    unittest.main()
