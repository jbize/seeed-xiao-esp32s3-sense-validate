#!/usr/bin/env python3
"""Read one 10-second picture stream and report whether the board is good.

The sketch measures three moves of each JPEG. This program checks that every
JPEG is 1024×768 and prints those three times in words a person who works
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
from urllib.parse import urlparse

WIDTH = 1024
HEIGHT = 768

# Lines from the boards already measured. A good board sent a 1024×768 JPEG
# in about 30 ms at about 30 frames/second. A bad board took 98 ms or more
# and stayed under 6 frames/second. The gap between those is UNCLEAR.
GOOD_FPS = 12.0
BAD_FPS = 6.0
GOOD_WIFI_US = 50_000
BAD_WIFI_US = 80_000
# A 20 KB copy from picture memory into internal memory is about 1 ms when
# that memory is healthy. 15 ms is already far past that.
BAD_COPY_US = 15_000
# Sensor time includes waiting for the OV3660 to finish the next JPEG.
# About 30 ms is one frame at 30 frames/second. 100 ms means the sensor or
# the connector is late.
BAD_SENSOR_US = 100_000

READY_URL = re.compile(r"ready (http://\d+\.\d+\.\d+\.\d+)")


class Verdict(Enum):
    GOOD = "GOOD"
    BAD = "BAD"
    UNCLEAR = "UNCLEAR"


@dataclass
class Picture:
    data: bytes
    sensor_us: int | None = None
    copy_us: int | None = None
    wifi_us: int | None = None
    rssi: int | None = None

    @property
    def timed(self) -> bool:
        return self.sensor_us is not None and self.copy_us is not None and self.wifi_us is not None


@dataclass
class StreamAssembler:
    """Pull JPEGs and timing lines out of the multipart stream.

    A JPEG may contain newline bytes. Text is parsed only before the next
    JPEG start, and a JPEG is removed only when its end marker is present.
    """

    board: dict[str, str] = field(default_factory=dict)
    pictures: list[Picture] = field(default_factory=list)
    _buf: bytes = b""

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        self._drain()

    def _drain(self) -> None:
        while self._buf:
            if self._buf.startswith(b"\xff\xd8"):
                end = self._buf.find(b"\xff\xd9")
                if end < 0:
                    return
                data = self._buf[: end + 2]
                self._buf = self._buf[end + 2 :]
                self.pictures.append(Picture(data=data))
                continue
            newline = self._buf.find(b"\n")
            marker = self._buf.find(b"\xff\xd8")
            if marker >= 0 and (newline < 0 or marker < newline):
                self._buf = self._buf[marker:]
                continue
            if newline < 0:
                if len(self._buf) > 4096:
                    self._buf = self._buf[-4:]
                return
            raw = self._buf[:newline]
            self._buf = self._buf[newline + 1 :]
            self._take_line(raw.decode("utf-8", "replace").strip("\r"))

    def _take_line(self, line: str) -> None:
        if line.startswith("board "):
            key, sep, value = line[6:].partition("=")
            if sep and key:
                self.board[key] = value
            return
        if not line.startswith("metric "):
            return
        fields: dict[str, int] = {}
        for token in line.split()[1:]:
            key, sep, value = token.partition("=")
            if not sep:
                continue
            try:
                fields[key] = int(value)
            except ValueError:
                return
        if self.pictures and not self.pictures[-1].timed and self.pictures[-1].data:
            picture = self.pictures[-1]
        else:
            picture = Picture(data=b"")
            self.pictures.append(picture)
        picture.sensor_us = fields.get("sensor_us")
        picture.copy_us = fields.get("copy_us")
        picture.wifi_us = fields.get("wifi_us")
        picture.rssi = fields.get("rssi")


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read width and height from a baseline or progressive start-of-frame marker."""
    for marker in (b"\xff\xc0", b"\xff\xc2"):
        start = data.find(marker)
        if start >= 0 and start + 9 <= len(data):
            height = int.from_bytes(data[start + 5 : start + 7], "big")
            width = int.from_bytes(data[start + 7 : start + 9], "big")
            return width, height
    return None


def _median(values: list[int]) -> float:
    return float(statistics.median(values))


def _ms(us: float) -> str:
    ms = us / 1000.0
    if abs(ms - round(ms)) < 0.05:
        return f"{round(ms):.0f} ms"
    return f"{ms:.1f} ms"


def _kb(size: int) -> str:
    return f"{size / 1024:.1f}"


@dataclass
class Report:
    verdict: Verdict
    text: str
    part: str


def build_report(
    pictures: list[Picture],
    board: dict[str, str],
    elapsed_s: float,
    window_s: float,
    closed_early: bool,
) -> Report:
    jpegs = [picture for picture in pictures if picture.data]
    # The run stops on a clock, so the last JPEG can arrive without the
    # timing line that follows it. That picture is unfinished, not a fault.
    while jpegs and not jpegs[-1].timed:
        jpegs.pop()
    sizes = [jpeg_dimensions(picture.data) for picture in jpegs]
    timed = [picture for picture in jpegs if picture.timed and picture.copy_us is not None and picture.copy_us >= 0]
    copy_failures = [picture for picture in pictures if picture.copy_us is not None and picture.copy_us < 0]
    missing_times = [picture for picture in jpegs if not picture.timed]

    sensor = _median([picture.sensor_us for picture in timed if picture.sensor_us is not None]) if timed else None
    copy = _median([picture.copy_us for picture in timed if picture.copy_us is not None]) if timed else None
    wifi_values = [picture.wifi_us for picture in timed if picture.wifi_us is not None]
    wifi = float(statistics.mean(wifi_values)) if wifi_values else None
    fastest = min(wifi_values) if wifi_values else None
    slowest = max(wifi_values) if wifi_values else None
    rssi_values = [picture.rssi for picture in timed if picture.rssi is not None]
    if rssi_values:
        rssi = int(round(_median(rssi_values)))
    elif "rssi" in board:
        try:
            rssi = int(board["rssi"])
        except ValueError:
            rssi = None
    else:
        rssi = None

    observed = elapsed_s if elapsed_s > 0 else window_s
    rate_span = observed
    fps = (len(jpegs) / rate_span) if rate_span > 0 else 0.0
    byte_count = sum(len(picture.data) for picture in jpegs)
    throughput = (byte_count / rate_span / (1024 * 1024)) if rate_span > 0 else 0.0

    wrong = [(picture, size) for picture, size in zip(jpegs, sizes) if size != (WIDTH, HEIGHT)]
    part, verdict, reason = _judge(
        jpegs=jpegs,
        wrong=len(wrong),
        sample_size=wrong[0][1] if wrong else None,
        copy_failures=len(copy_failures),
        missing_times=len(missing_times),
        sensor=sensor,
        copy=copy,
        wifi=wifi,
        fps=fps,
        rssi=rssi,
        closed_early=closed_early,
        observed=observed,
        window_s=window_s,
    )
    text = _render(
        board=board,
        jpegs=jpegs,
        sizes=sizes,
        elapsed_s=observed,
        window_s=window_s,
        closed_early=closed_early,
        fps=fps,
        throughput=throughput,
        sensor=sensor,
        copy=copy,
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
    jpegs: list[Picture],
    wrong: int,
    sample_size: tuple[int, int] | None,
    copy_failures: int,
    missing_times: int,
    sensor: float | None,
    copy: float | None,
    wifi: float | None,
    fps: float,
    rssi: int | None,
    closed_early: bool,
    observed: float,
    window_s: float,
) -> tuple[str, Verdict, str]:
    if not jpegs and copy_failures == 0:
        return "", Verdict.BAD, "No JPEG arrived."
    if copy_failures:
        return (
            "internal memory in the ESP32-S3",
            Verdict.BAD,
            "The ESP32-S3 had no free internal memory to hold one JPEG.",
        )
    if wrong:
        if sample_size is None:
            arrived = "a size this program could not read"
        else:
            arrived = f"{sample_size[0]}×{sample_size[1]}"
        return (
            "",
            Verdict.BAD,
            f"A JPEG arrived at {arrived}. The picture under test is {WIDTH}×{HEIGHT}, so this run does not name a part.",
        )
    if missing_times or sensor is None or copy is None or wifi is None:
        return "", Verdict.UNCLEAR, "A JPEG arrived without its sensor, copy, and Wi-Fi times."

    failures: list[tuple[float, str, str]] = []
    if copy >= BAD_COPY_US:
        failures.append(
            (
                copy / BAD_COPY_US,
                "picture memory in the ESP32-S3 package",
                f"The copy from picture memory into internal memory was {_ms(copy)}.",
            )
        )
    if wifi >= BAD_WIFI_US:
        signal = f"{rssi} dBm" if rssi is not None else "not reported"
        failures.append(
            (
                wifi / BAD_WIFI_US,
                "ESP32-S3 Wi-Fi or the antenna parts on the Sense board",
                f"The Wi-Fi send was {_ms(wifi)}. The signal was {signal}.",
            )
        )
    if sensor >= BAD_SENSOR_US:
        failures.append(
            (
                sensor / BAD_SENSOR_US,
                "OV3660 or the connector",
                f"The JPEG was late coming from the sensor ({_ms(sensor)}).",
            )
        )
    if failures:
        _ratio, part, reason = max(failures, key=lambda item: item[0])
        if len(failures) == 1:
            if part.startswith("ESP32-S3 Wi-Fi"):
                reason += " The OV3660 delivered the JPEG quickly, and the copy into internal memory was quick."
            elif part.startswith("OV3660"):
                reason += " The copy and the Wi-Fi send were quick."
            else:
                reason += " The sensor and the Wi-Fi send were not the slow stage."
        else:
            others = [item[1] for item in failures if item[1] != part]
            reason = f"{reason} Also past its limit: {', '.join(others)}."
        return part, Verdict.BAD, reason

    if fps <= BAD_FPS:
        longest = _longest_stage(sensor, copy, wifi)
        return (
            "",
            Verdict.BAD,
            f"The rate was {fps:.1f} frames/second. None of the three stages crossed its own limit. "
            f"The longest stage was {longest}.",
        )
    if closed_early:
        return (
            "",
            Verdict.BAD,
            f"The stream closed after {observed:.1f} seconds of a {window_s:.0f} second run.",
        )
    if (
        fps >= GOOD_FPS
        and wifi <= GOOD_WIFI_US
        and copy < BAD_COPY_US
        and sensor < BAD_SENSOR_US
    ):
        return (
            "",
            Verdict.GOOD,
            f"The sensor, the copy, and the Wi-Fi send all stayed inside the limits on a verified {WIDTH}×{HEIGHT} picture.",
        )
    return (
        "",
        Verdict.UNCLEAR,
        f"The rate was {fps:.1f} frames/second and the Wi-Fi send was {_ms(wifi)}. "
        "That sits between the good and bad lines.",
    )


def _longest_stage(sensor: float, copy: float, wifi: float) -> str:
    stages = (
        (sensor, f"the sensor at {_ms(sensor)}"),
        (copy, f"the copy at {_ms(copy)}"),
        (wifi, f"Wi-Fi at {_ms(wifi)}"),
    )
    return max(stages, key=lambda item: item[0])[1]


def _on_off(value: str | None, on_is: str = "1") -> str:
    if value is None:
        return "not reported"
    return "on" if value == on_is else "off"


def _render(
    board: dict[str, str],
    jpegs: list[Picture],
    sizes: list[tuple[int, int] | None],
    elapsed_s: float,
    window_s: float,
    closed_early: bool,
    fps: float,
    throughput: float,
    sensor: float | None,
    copy: float | None,
    wifi: float | None,
    fastest: int | None,
    slowest: int | None,
    rssi: int | None,
    verdict: Verdict,
    part: str,
    reason: str,
) -> str:
    counts: dict[tuple[int, int] | None, int] = {}
    for size in sizes:
        counts[size] = counts.get(size, 0) + 1
    if counts == {(WIDTH, HEIGHT): len(jpegs)} and jpegs:
        frame_line = f"Frame size: XGA, and every JPEG measured {WIDTH}×{HEIGHT}"
    elif not jpegs:
        frame_line = "Frame size: XGA was requested, and no JPEG arrived"
    else:
        pieces = []
        for size, count in counts.items():
            label = "unreadable" if size is None else f"{size[0]}×{size[1]}"
            pieces.append(f"{label} ({count})")
        frame_line = "Frame size: XGA was requested, and the JPEGs measured " + ", ".join(pieces)

    if jpegs:
        lengths = [len(picture.data) for picture in jpegs]
        size_line = f"JPEG size: {_kb(round(sum(lengths) / len(lengths)))} KB average ({_kb(min(lengths))}–{_kb(max(lengths))} KB)"
    else:
        size_line = "JPEG size: none"

    if closed_early:
        transfer = f"Transfer, {elapsed_s:.1f} seconds of a {window_s:.0f} second run, one stream client"
    else:
        transfer = f"Transfer, {elapsed_s:.1f} seconds, one stream client"

    sensor_line = "Sensor: not measured" if sensor is None else f"Sensor: {_ms(sensor)} median"
    copy_line = "Copy: not measured" if copy is None else f"Copy: {_ms(copy)} median"
    if wifi is None or fastest is None or slowest is None:
        wifi_line = "Wi-Fi: not measured"
    else:
        wifi_line = f"Wi-Fi: {_ms(fastest)} fastest, {_ms(wifi)} average, {_ms(slowest)} slowest"

    result = f"Result: {verdict.value}"
    if part:
        result = f"{result} — {part}"

    lines = [
        "Seeed Studio XIAO ESP32S3 Sense — picture transfer",
        "",
        f"Board: {board.get('mac', 'not reported')}",
        f"Signal: {rssi} dBm" if rssi is not None else "Signal: not reported",
        "",
        "Picture",
        f"  Clock: {board.get('clock_mhz', 'not reported')} MHz",
        f"  {frame_line}",
        f"  JPEG quality: {board.get('quality', 'not reported')}",
        "  Brightness {brightness}, saturation {saturation}, mirror {mirror}, flip {flip}, night exposure {night}".format(
            brightness=board.get("brightness", "not reported"),
            saturation=board.get("saturation", "not reported"),
            mirror=_on_off(board.get("mirror")),
            flip=_on_off(board.get("flip")),
            night=_on_off(board.get("night")),
        ),
        "",
        transfer,
        f"  Frames: {len(jpegs)}",
        f"  Rate: {fps:.1f} frames/second",
        f"  {size_line}",
        f"  Throughput: {throughput:.2f} MB/s",
        f"  {sensor_line}",
        "    The OV3660 built the JPEG and the ESP32-S3 stored it in picture memory.",
        f"  {copy_line}",
        "    The ESP32-S3 processor copied that JPEG from picture memory into internal memory.",
        f"  {wifi_line}",
        "    The ESP32-S3 Wi-Fi block sent that internal copy out the antenna.",
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
            if "camera failed" in text:
                raise RuntimeError("The camera did not start.")
            if "wifi failed" in text:
                raise RuntimeError("The board could not join Wi-Fi.")
            found = ready_url(text)
            if found is not None:
                return found
        raise RuntimeError("The board did not print its address.")
    finally:
        os.close(fd)


def stream_url(base: str) -> str:
    trimmed = base.strip().rstrip("/")
    if "://" not in trimmed:
        trimmed = "http://" + trimmed
    if trimmed.endswith("/stream"):
        return trimmed
    return trimmed + "/stream"


def read_stream(base: str, seconds: float) -> tuple[StreamAssembler, float, bool]:
    """Be the only stream client for `seconds`. Returns the parse, the elapsed time, and whether the stream closed early."""
    url = urlparse(stream_url(base))
    if url.hostname is None:
        raise RuntimeError(f"No address in {base}")
    port = url.port or 80
    path = url.path or "/stream"
    sock = socket.create_connection((url.hostname, port), timeout=8)
    closed_early = False
    assembler = StreamAssembler()
    try:
        sock.settimeout(None)
        request = f"GET {path} HTTP/1.1\r\nHost: {url.hostname}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode())
        started = time.monotonic()
        deadline = started + seconds
        while time.monotonic() < deadline:
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
    parser = argparse.ArgumentParser(description="Judge a Seeed Studio XIAO ESP32S3 Sense by its picture transfer.")
    parser.add_argument("--url", help="Board address, such as http://192.168.1.97. Omit this to read the address from USB.")
    parser.add_argument("--port", default="/dev/ttyACM0", help="USB serial device used when --url is omitted.")
    parser.add_argument("--seconds", type=float, default=10.0, help="How long to read the stream. The default is 10. Use 30 for a longer run.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.seconds <= 0:
        print("The run length has to be greater than zero.", file=sys.stderr)
        return 3
    try:
        base = args.url or read_ready_url(args.port)
        assembler, elapsed, closed_early = read_stream(base, args.seconds)
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 3
    report = build_report(assembler.pictures, assembler.board, elapsed, args.seconds, closed_early)
    sys.stdout.write(report.text)
    return exit_code(report.verdict)


if __name__ == "__main__":
    raise SystemExit(main())
