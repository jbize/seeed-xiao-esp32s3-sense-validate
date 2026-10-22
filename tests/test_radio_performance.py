"""Checks for the radio performance parser and the good / bad / unclear report."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import radio_performance


def payload(size: int) -> bytes:
    return bytes(index & 0xFF for index in range(size))


def metric(wifi_us: int, rssi: int = -45) -> bytes:
    return f"metric wifi_us={wifi_us} rssi={rssi}\n".encode()


def stream(size: int, times: list[int]) -> bytes:
    body = f"radio_performance mac=44:BD:8D:EC:C0:84 bytes={size}\n".encode()
    block = payload(size)
    for wifi_us in times:
        body += block + metric(wifi_us)
    return body


def feed_all(data: bytes, chunk: int = 1) -> radio_performance.RadioAssembler:
    assembler = radio_performance.RadioAssembler()
    for start in range(0, len(data), chunk):
        assembler.feed(data[start : start + chunk])
    return assembler


class ParserTests(unittest.TestCase):
    def test_payload_bytes_are_not_read_as_text(self) -> None:
        # The payload itself contains a newline and the word metric.
        size = 8
        body = b"radio_performance mac=44:BD:8D:EC:C0:84 bytes=8\n"
        body += b"metric\n!" + metric(28_000)
        body += b"\nradio x" + metric(30_000, rssi=-49)
        assembler = feed_all(body)
        self.assertFalse(assembler.broken)
        self.assertEqual(assembler.board["mac"], "44:BD:8D:EC:C0:84")
        self.assertEqual(len(assembler.sends), 2)
        self.assertEqual(assembler.sends[0].wifi_us, 28_000)
        self.assertEqual(assembler.sends[1].rssi, -49)

    def test_ready_url(self) -> None:
        self.assertEqual(radio_performance.ready_url("radio performance\nready http://192.168.1.95\n"), "http://192.168.1.95")
        self.assertIsNone(radio_performance.ready_url("wifi failed\n"))

    def test_ten_seconds_is_the_default_and_thirty_is_accepted(self) -> None:
        self.assertEqual(radio_performance.parse_args([]).seconds, 10.0)
        self.assertEqual(radio_performance.parse_args(["--seconds", "30"]).seconds, 30.0)


class ReportTests(unittest.TestCase):
    def report_from(self, times: list[int], elapsed: float = 10.0, closed_early: bool = False, broken: bool = False) -> radio_performance.Report:
        assembler = feed_all(stream(20 * 1024, times))
        return radio_performance.build_report(assembler.sends, assembler.board, elapsed, 10.0, closed_early, broken or assembler.broken)

    def test_fast_sends_are_good(self) -> None:
        report = self.report_from([28_000] * 300 + [41_000])
        self.assertEqual(report.verdict, radio_performance.Verdict.GOOD)
        self.assertIn("Result: GOOD", report.text)
        self.assertIn("Wi-Fi: 28 ms fastest, 28 ms average, 41 ms slowest", report.text)
        self.assertIn("20.0 KB, from internal memory", report.text)
        self.assertIn("camera module was not used", report.text)
        self.assertIn("out the antenna", report.text)

    def test_stalls_fail_the_average_even_when_some_sends_are_fast(self) -> None:
        report = self.report_from([27_000] * 8 + [1_000_000] * 7)
        self.assertEqual(report.verdict, radio_performance.Verdict.BAD)
        self.assertEqual(report.part, "ESP32-S3 Wi-Fi or the antenna parts on the Sense board")
        self.assertIn("27 ms fastest", report.text)
        self.assertIn("1000 ms slowest", report.text)
        self.assertIn("-45 dBm", report.text)

    def test_slow_average_names_the_radio_or_the_antenna_parts(self) -> None:
        report = self.report_from([98_000] * 12)
        self.assertEqual(report.verdict, radio_performance.Verdict.BAD)
        self.assertIn("98 ms", report.text)
        self.assertIn("internal memory", report.text)

    def test_the_gap_is_unclear(self) -> None:
        report = self.report_from([60_000] * 20)
        self.assertEqual(report.verdict, radio_performance.Verdict.UNCLEAR)
        self.assertIn("between 50 ms and 80 ms", report.text)

    def test_a_short_run_is_not_good(self) -> None:
        report = self.report_from([28_000] * 30, elapsed=1.0, closed_early=True)
        self.assertEqual(report.verdict, radio_performance.Verdict.BAD)
        self.assertIn("closed after", report.text)

    def test_no_send_is_bad(self) -> None:
        report = radio_performance.build_report([], {"mac": "44:BD:8D:EC:C0:84", "bytes": "20480"}, 10.0, 10.0, False, False)
        self.assertEqual(report.verdict, radio_performance.Verdict.BAD)
        self.assertIn("No send completed", report.text)

    def test_a_trailing_send_without_a_time_is_dropped(self) -> None:
        assembler = feed_all(stream(64, [28_000] * 20) + payload(64))
        report = radio_performance.build_report(assembler.sends, assembler.board, 10.0, 10.0, False, assembler.broken)
        self.assertEqual(report.verdict, radio_performance.Verdict.GOOD)
        self.assertIn("Sends: 20", report.text)

    def test_a_broken_step_is_unclear(self) -> None:
        report = self.report_from([28_000] * 5, broken=True)
        self.assertEqual(report.verdict, radio_performance.Verdict.UNCLEAR)
        self.assertIn("out of step", report.text)


if __name__ == "__main__":
    unittest.main()
