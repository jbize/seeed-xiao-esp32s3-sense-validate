#!/usr/bin/env python3
"""Radio performance test. Send a fixed buffer from internal memory and report the Wi-Fi time.

The sketch does not start the camera. This program checks that each send is
the announced size and prints the Wi-Fi time in words a person who works
with the Seeed Studio XIAO ESP32S3 Sense can read.
"""

from __future__ import annotations

import argparse
import os
import re
import select
import socket
import statistics
import sys
import termios
import time
from dataclasses import dataclass, field
from enum import Enum

# The same send lines as the picture test. A good board sent about 20 KB in
# about 30 ms. A bad board averaged 80 ms or more. The gap is UNCLEAR.
GOOD_WIFI_US = 50_000
BAD_WIFI_US = 80_000

READY_URL = re.compile(r"ready (http://\d+\.\d+\.\d+\.\d+)")


class Verdict(Enum):
    GOOD = "GOOD"
    BAD = "BAD"
    UNCLEAR = "UNCLEAR"


@dataclass
class Send:
    size: int
    wifi_us: int | None = None
    rssi: int | None = None

    @property
    def timed(self) -> bool:
        return self.wifi_us is not None


@dataclass
class RadioAssembler:
    """Pull fixed-size sends and the timing line that follows each one.

    The payload is a raw byte count announced by the first line. Those bytes
    are never scanned for text, so a payload cannot look like a timing line.
    """

    board: dict[str, str] = field(default_factory=dict)
    sends: list[Send] = field(default_factory=list)
    broken: bool = False
    _buf: bytes = b""
    _need: int | None = None
    _awaiting_metric: bool = False

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        self._drain()

    def _drain(self) -> None:
        while self._buf and not self.broken:
            if self._need is None:
                newline = self._buf.find(b"\n")
                if newline < 0:
                    return
                line = self._buf[:newline].decode("utf-8", "replace").strip("\r")
                self._buf = self._buf[newline + 1 :]
                self._take_header(line)
                continue
            if not self._awaiting_metric:
                if len(self._buf) < self._need:
                    return
                self.sends.append(Send(size=self._need))
                self._buf = self._buf[self._need :]
                self._awaiting_metric = True
                continue
            newline = self._buf.find(b"\n")
            if newline < 0:
                return
            line = self._buf[:newline].decode("utf-8", "replace").strip("\r")
            self._buf = self._buf[newline + 1 :]
            self._take_metric(line)
            self._awaiting_metric = False

    def _take_header(self, line: str) -> None:
        if not line.startswith("radio_performance "):
            self.broken = True
            return
        fields: dict[str, str] = {}
        for token in line.split()[1:]:
            key, sep, value = token.partition("=")
            if sep and key:
                fields[key] = value
        try:
            need = int(fields["bytes"])
        except (KeyError, ValueError):
            self.broken = True
            return
        if need <= 0:
            self.broken = True
            return
        self.board["mac"] = fields.get("mac", "")
        self.board["bytes"] = str(need)
        self._need = need

    def _take_metric(self, line: str) -> None:
        if not line.startswith("metric ") or not self.sends:
            self.broken = True
            return
        fields: dict[str, int] = {}
        for token in line.split()[1:]:
            key, sep, value = token.partition("=")
            if not sep:
                continue
            try:
                fields[key] = int(value)
            except ValueError:
                self.broken = True
                return
        if "wifi_us" not in fields:
            self.broken = True
            return
        send = self.sends[-1]
        send.wifi_us = fields["wifi_us"]
        send.rssi = fields.get("rssi")


def _median(values: list[int]) -> float:
    return float(statistics.median(values))


def _ms(us: float) -> str:
    ms = us / 1000.0
    if abs(ms - round(ms)) < 0.05:
        return f"{round(ms):.0f} ms"
    return f"{ms:.1f} ms"


@dataclass
class Report:
    verdict: Verdict
    text: str
    part: str


def build_report(
    sends: list[Send],
    board: dict[str, str],
    elapsed_s: float,
    window_s: float,
    closed_early: bool,
    broken: bool,
) -> Report:
    finished = list(sends)
    # The run stops on a clock, so the last payload can arrive without the
    # timing line that follows it. That send is unfinished, not a fault.
    while finished and not finished[-1].timed:
        finished.pop()

    wifi_values = [send.wifi_us for send in finished if send.wifi_us is not None]
    wifi = float(statistics.mean(wifi_values)) if wifi_values else None
    fastest = min(wifi_values) if wifi_values else None
    slowest = max(wifi_values) if wifi_values else None
    rssi_values = [send.rssi for send in finished if send.rssi is not None]
    rssi = int(round(_median(rssi_values))) if rssi_values else None

    observed = elapsed_s if elapsed_s > 0 else window_s
    rate = (len(finished) / observed) if observed > 0 else 0.0
    byte_count = sum(send.size for send in finished)
    throughput = (byte_count / observed / (1024 * 1024)) if observed > 0 else 0.0
    try:
        payload = int(board["bytes"]) if "bytes" in board else (finished[0].size if finished else 0)
    except ValueError:
        payload = 0

    part, verdict, reason = _judge(
        sends=finished,
        wifi=wifi,
        rssi=rssi,
        broken=broken,
        closed_early=closed_early,
        observed=observed,
        window_s=window_s,
    )
    text = _render(
        board=board,
        count=len(finished),
        payload=payload,
        elapsed_s=observed,
        window_s=window_s,
        closed_early=closed_early,
        rate=rate,
        throughput=throughput,
        wifi=wifi,
        fastest=fastest,
        slowest=slowest,
        rssi=rssi,
        verdict=verdict,
        part=part,
        reason=reason,
    )
    return Report(verdict=verdict, text=text, part=part)


def _judge(
    sends: list[Send],
    wifi: float | None,
    rssi: int | None,
    broken: bool,
    closed_early: bool,
    observed: float,
    window_s: float,
) -> tuple[str, Verdict, str]:
    if broken:
        return "", Verdict.UNCLEAR, "A send and its timing line fell out of step."
    if not sends or wifi is None:
        return "", Verdict.BAD, "No send completed."
    if wifi >= BAD_WIFI_US:
        signal = f"{rssi} dBm" if rssi is not None else "not reported"
        return (
            "ESP32-S3 Wi-Fi or the antenna parts on the Sense board",
            Verdict.BAD,
            f"The Wi-Fi send was {_ms(wifi)}. The signal was {signal}. "
            "The buffer was already in internal memory, and the camera module was not used.",
        )
    if closed_early:
        return (
            "",
            Verdict.BAD,
            f"The connection closed after {observed:.1f} seconds of a {window_s:.0f} second run.",
        )
    if wifi <= GOOD_WIFI_US:
        return (
            "",
            Verdict.GOOD,
            "The Wi-Fi send stayed at or under 50 ms. "
            "The buffer was already in internal memory, and the camera module was not used.",
        )
    return (
        "",
        Verdict.UNCLEAR,
        f"The Wi-Fi send was {_ms(wifi)}. That sits between 50 ms and 80 ms.",
    )


def _render(
    board: dict[str, str],
    count: int,
    payload: int,
    elapsed_s: float,
    window_s: float,
    closed_early: bool,
    rate: float,
    throughput: float,
    wifi: float | None,
    fastest: int | None,
    slowest: int | None,
    rssi: int | None,
    verdict: Verdict,
    part: str,
    reason: str,
) -> str:
    if closed_early:
        transfer = f"Transfer, {elapsed_s:.1f} seconds of a {window_s:.0f} second run, one client"
    else:
        transfer = f"Transfer, {elapsed_s:.1f} seconds, one client"
    if wifi is None or fastest is None or slowest is None:
        wifi_line = "Wi-Fi: not measured"
    else:
        wifi_line = f"Wi-Fi: {_ms(fastest)} fastest, {_ms(wifi)} average, {_ms(slowest)} slowest"
    result = f"Result: {verdict.value}"
    if part:
        result = f"{result} — {part}"
    payload_kb = f"{payload / 1024:.1f}" if payload else "not reported"
    lines = [
        "Seeed Studio XIAO ESP32S3 Sense — radio performance",
        "",
        f"Board: {board.get('mac') or 'not reported'}",
        f"Signal: {rssi} dBm" if rssi is not None else "Signal: not reported",
        "",
        "Send",
        f"  Payload: {payload_kb} KB, from internal memory",
        "  The camera module is not used.",
        "",
        transfer,
        f"  Sends: {count}",
        f"  Rate: {rate:.1f} sends/second",
        f"  Throughput: {throughput:.2f} MB/s",
        f"  {wifi_line}",
        "    The ESP32-S3 Wi-Fi block sent that buffer out the antenna.",
        "",
        result,
        f"  {reason}",
    ]
    return "\n".join(lines) + "\n"


def ready_url(text: str) -> str | None:
    match = READY_URL.search(text)
    if match is None:
        return None
    return match.group(1)


def _set_dtr(fd: int, asserted: bool) -> None:
    import fcntl

    flags = int.from_bytes(fcntl.ioctl(fd, termios.TIOCMGET, b"\x00\x00\x00\x00"), "little")
    if asserted:
        flags |= termios.TIOCM_DTR
    else:
        flags &= ~termios.TIOCM_DTR
    fcntl.ioctl(fd, termios.TIOCMSET, flags.to_bytes(4, "little"))


def read_ready_url(port: str, timeout_s: float = 40.0) -> str:
    """Open the USB port, restart the board, and return the address it prints."""
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attributes = termios.tcgetattr(fd)
        attributes[0] = 0
        attributes[1] = 0
        attributes[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attributes[3] = 0
        attributes[4] = termios.B115200
        attributes[5] = termios.B115200
        attributes[6][termios.VMIN] = 0
        attributes[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
        _set_dtr(fd, False)
        time.sleep(0.05)
        _set_dtr(fd, True)
        time.sleep(0.15)
        termios.tcflush(fd, termios.TCIFLUSH)
        deadline = time.monotonic() + timeout_s
        buf = b""
        while time.monotonic() < deadline:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            text = buf.decode("utf-8", "replace")
            if "radio performance failed" in text:
                raise RuntimeError("The ESP32-S3 had no free internal memory for the buffer.")
            if "wifi failed" in text:
                raise RuntimeError("The board could not join Wi-Fi.")
            found = ready_url(text)
            if found is not None:
                return found
        raise RuntimeError("The board did not print its address.")
    finally:
        os.close(fd)


def read_sends(base: str, seconds: float) -> tuple[RadioAssembler, float, bool]:
    """Be the only client for `seconds`. Returns the parse, the elapsed time, and whether the connection closed early."""
    host = base.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if ":" in host:
        name, port_text = host.rsplit(":", 1)
        port = int(port_text)
        host = name
    else:
        port = 80
    sock = socket.create_connection((host, port), timeout=8)
    assembler = RadioAssembler()
    closed_early = False
    try:
        sock.settimeout(None)
        header_deadline = time.monotonic() + 5
        while assembler._need is None and not assembler.broken and time.monotonic() < header_deadline:
            ready, _, _ = select.select([sock], [], [], 0.5)
            if not ready:
                continue
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("The board closed the connection before the first send.")
            assembler.feed(chunk)
        if assembler._need is None:
            raise RuntimeError("The board did not announce the payload size.")
        started = time.monotonic()
        deadline = started + seconds
        while time.monotonic() < deadline and not assembler.broken:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([sock], [], [], min(0.5, remaining))
            if not ready:
                continue
            chunk = sock.recv(65536)
            if not chunk:
                closed_early = True
                break
            assembler.feed(chunk)
        elapsed = time.monotonic() - started
    finally:
        sock.close()
    if elapsed + 0.5 < seconds:
        closed_early = True
    return assembler, elapsed, closed_early


def exit_code(verdict: Verdict) -> int:
    if verdict is Verdict.GOOD:
        return 0
    if verdict is Verdict.BAD:
        return 1
    return 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Radio performance test for a Seeed Studio XIAO ESP32S3 Sense, with the camera module unplugged.")
    parser.add_argument("--url", help="Board address, such as http://192.168.1.97. Omit this to read the address from USB.")
    parser.add_argument("--port", default="/dev/ttyACM0", help="USB serial device used when --url is omitted.")
    parser.add_argument("--seconds", type=float, default=10.0, help="How long to read sends. The default is 10. Use 30 for a longer run.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.seconds <= 0:
        print("The run length has to be greater than zero.", file=sys.stderr)
        return 3
    try:
        base = args.url or read_ready_url(args.port)
        assembler, elapsed, closed_early = read_sends(base, args.seconds)
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 3
    report = build_report(assembler.sends, assembler.board, elapsed, args.seconds, closed_early, assembler.broken)
    sys.stdout.write(report.text)
    return exit_code(report.verdict)


if __name__ == "__main__":
    raise SystemExit(main())
