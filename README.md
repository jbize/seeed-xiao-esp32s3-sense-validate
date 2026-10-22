# Seeed Studio XIAO ESP32S3 Sense — picture transfer rate check

This checks one Sense board. It takes a fixed picture, sends that picture over Wi-Fi for 10 seconds, and prints a report that ends in `GOOD`, `BAD`, or `UNCLEAR`.

I had purchased a batch of Sense boards for a security camera project and could not understand why my results were so poor.  I had assumed it was my own sketch code, but eventually determined the issue to be hardware. Several of these boards were too slow to use. A good board delivers about 30 JPEGs a second at this picture. The slow ones managed about 2 to 4, and one of them took hundreds of milliseconds to send a single JPEG. The picture itself was fine: the OV3660 produced a real 1024×768 JPEG, the signal was strong, and the same slowness showed up in Seeed's own camera sketch. That made the fault hard to place. It was not the sensor, and it was not a loose antenna in any obvious way.

This test times the three places a JPEG can stall.

1. The OV3660 and the connector, while the ESP32-S3 stores the JPEG in picture memory.
2. The copy from that picture memory into the ESP32-S3's internal memory.
3. The Wi-Fi send of that internal copy out the antenna.

On the slow boards, the first two steps were quick. The Wi-Fi send was the slow one, so the report names the radio path: the Wi-Fi block in the ESP32-S3, or the antenna parts on the Sense board. The timer cannot split those two. A different board can still fail at the sensor or at the copy, and the report names that part instead.

The picture is the one already used to compare these boards: a 21 MHz clock, XGA, JPEG quality 30, brightness 1, saturation -2, mirror off, flip on, and night exposure off. XGA here is 1024×768. The program reads that size from the JPEG itself.

## How to read the report

Each JPEG crosses the board in three steps. The report times each step and says who moved the bytes.

**Sensor.** The OV3660 on the daughterboard builds one JPEG and pushes those bytes across the connector. The ESP32-S3 on the Sense board writes them into picture memory. Picture memory is the 8 MB of PSRAM inside the ESP32-S3 package. It is not on the daughterboard. This time includes waiting for the OV3660 to finish the next JPEG. At 30 frames a second that wait is about 30 ms. On a board whose Wi-Fi is slow, the next JPEG is often already sitting in picture memory, so this time is short.

**Copy.** The ESP32-S3 processor copies that JPEG from picture memory into internal memory. Internal memory is the SRAM inside the ESP32-S3. The Wi-Fi block can transmit only from internal memory. A healthy copy of one of these JPEGs is about 1 ms.

**Wi-Fi.** The Wi-Fi block in the ESP32-S3 sends that internal copy out the antenna. The signal line is how strongly this board hears the network. A strong signal with a slow send points at the ESP32-S3 Wi-Fi block or the antenna parts on the Sense board between that chip and the antenna. The timer cannot split those two.

```
Seeed Studio XIAO ESP32S3 Sense — picture transfer

Board: 68:EE:8F:5C:27:E4
Signal: -48 dBm

Picture
  Clock: 21 MHz
  Frame size: XGA, and every JPEG measured 1024×768
  JPEG quality: 30
  Brightness 1, saturation -2, mirror off, flip on, night exposure off

Transfer, 10.0 seconds, one stream client
  Frames: 300
  Rate: 30.0 frames/second
  JPEG size: 18.2 KB average (18.2–18.2 KB)
  Throughput: 0.53 MB/s
  Sensor: 4 ms median
    The OV3660 built the JPEG and the ESP32-S3 stored it in picture memory.
  Copy: 1 ms median
    The ESP32-S3 processor copied that JPEG from picture memory into internal memory.
  Wi-Fi: 28 ms fastest, 28 ms average, 41 ms slowest
    The ESP32-S3 Wi-Fi block sent that internal copy out the antenna.

Result: GOOD
  The sensor, the copy, and the Wi-Fi send all stayed inside the limits on a verified 1024×768 picture.
```

`GOOD` means every JPEG measured 1024×768, the rate was at least 12 frames a second, the average Wi-Fi send was 50 ms or less, the median copy was under 15 ms, and the median sensor time was under 100 ms.

`BAD` names the part when one step is past its limit:

| What was slow | Limit | The part the report names |
| --- | --- | --- |
| Copy | 15 ms or more | Picture memory in the ESP32-S3 package |
| Wi-Fi send, the average | 80 ms or more | The ESP32-S3 Wi-Fi block, or the antenna parts on the Sense board |
| Sensor | 100 ms or more | The OV3660, or the connector |
| Rate | 6 frames a second or less, with no step past its own limit | No single part. The report says which step took the longest. |

A JPEG that is not 1024×768 is `BAD` as well. That run does not name a part, because it was not the picture under test. If the ESP32-S3 has no free internal memory for one JPEG, the result is `BAD` and names internal memory.

`UNCLEAR` is the gap between those lines. The boards already measured did not land there: a good board was about 30 frames a second with a send near 30 ms, and a bad board was under 6 frames a second with a send of 98 ms or more.

The sensor and copy lines are the middle picture of the run. One odd picture does not move them. The Wi-Fi line is different, because a stall is the fault these boards showed. Fastest is the quickest send, average is the send time across the whole run and the number the verdict uses, and slowest is the worst send.

## Run it

Copy the example and set the Wi-Fi name and password:

```
cp include/secrets.h.example include/secrets.h
```

`include/secrets.h` is not committed. Flash the sketch, then run the report from this directory. The program restarts the board on USB, waits until it prints its address, and is the only stream client for 10 seconds.

```
pio run -t upload
python3 validate.py
python3 validate.py --seconds 30
```

Ten seconds is the default. `--seconds 30` keeps the same picture and reads the stream for thirty seconds. Pass `--url http://192.168.1.97` to skip the restart and talk to a board that is already up. The exit code is 0 for `GOOD`, 1 for `BAD`, 2 for `UNCLEAR`, and 3 when the board cannot be reached.

```
python3 -m unittest discover
```

That checks the parser and the report without a board.

## Before a board goes back

The Wi-Fi password is compiled into the flashed image. Erase the whole chip before that board goes back in a package. The microSD card is not part of the chip erase.

```
esptool.py --chip esp32s3 --port /dev/ttyACM0 erase_flash
```
