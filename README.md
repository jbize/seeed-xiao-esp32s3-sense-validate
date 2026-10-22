# Seeed Studio XIAO ESP32S3 Sense — picture transfer and radio performance

This repository has two checks for one Sense board. The picture transfer check takes a fixed picture, sends it over Wi-Fi for 10 seconds, and times the sensor, the copy, and the Wi-Fi send. The radio performance test leaves the camera module unplugged and times only the Wi-Fi send. Each report ends in `GOOD`, `BAD`, or `UNCLEAR`.

I had purchased a batch of Sense boards for a security camera project and could not understand why my results were so poor. I had assumed it was my own sketch code, but eventually determined the issue to be hardware. Several of these boards were too slow to use. A good board delivers about 30 JPEGs a second at this picture. The slow ones managed about 2 to 4, and one of them took hundreds of milliseconds to send a single JPEG. The picture itself was fine: the OV3660 produced a real 1024×768 JPEG, the signal was strong, and the same slowness showed up in Seeed's own camera sketch. That made the fault hard to place. It was not the sensor, and it was not a loose antenna in any obvious way.

This test times the three places a JPEG can stall.

1. The OV3660 and the connector, while the ESP32-S3 stores the JPEG in picture memory.
2. The copy from that picture memory into the ESP32-S3's internal memory.
3. The Wi-Fi send of that internal copy out the antenna.

On the slow boards, the first two steps were quick. The Wi-Fi send was the slow one, so the report names the radio path: the Wi-Fi block in the ESP32-S3, or the antenna parts on the Sense board. The timer cannot split those two. A different board can still fail at the sensor or at the copy, and the report names that part instead. The radio performance test is that third step on its own, with the camera module unplugged.

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

## Run the picture test

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

That checks both reports without a board.

## Radio performance test

Unplug the camera module first. This sketch is separate from the picture test. It never starts the camera, and it does not use picture memory. It puts one 20 KB buffer in the ESP32-S3's internal memory, the SRAM the Wi-Fi block can transmit from, and sends that same buffer for 10 seconds. The timer covers only that write. 20 KB is about the size of the JPEGs in the picture test, so the send times can be compared.

Both sketches use `include/secrets.h`. Flash this one by name, then run its report from this directory. The program restarts the board on USB, waits until it prints its address, and is the only client.

```
pio run -e radio_performance -t upload
python3 radio_performance.py
python3 radio_performance.py --seconds 30
```

Ten seconds is the default. Pass `--url http://192.168.1.95` to skip the restart and talk to a board that is already up. The exit codes are the same as the picture test.

`GOOD` means the average send was 50 ms or less across the whole run. `BAD` means the average was 80 ms or more, and the report names the ESP32-S3 Wi-Fi block or the antenna parts on the Sense board. A run with no completed send, or a connection that closes early, is `BAD` as well. `UNCLEAR` is the gap, an average between 50 ms and 80 ms.

The verdict uses the average. Fastest is the quickest send, and it shows that the radio can still get one buffer out quickly. Slowest is the worst stall. One slow send does not decide the result by itself.

This is a board that had already failed the picture test on the Wi-Fi send. With the camera module removed, the radio performance test failed the same way. The signal was strong, a few sends were quick, and the average was still about half a second.

```
Seeed Studio XIAO ESP32S3 Sense — radio performance

Board: 44:BD:8D:EC:C0:84
Signal: -42 dBm

Send
  Payload: 20.0 KB, from internal memory
  The camera module is not used.

Transfer, 10.0 seconds, one client
  Sends: 17
  Rate: 1.7 sends/second
  Throughput: 0.03 MB/s
  Wi-Fi: 16.2 ms fastest, 542.3 ms average, 3332.9 ms slowest
    The ESP32-S3 Wi-Fi block sent that buffer out the antenna.

Result: BAD — ESP32-S3 Wi-Fi or the antenna parts on the Sense board
  The Wi-Fi send was 542.3 ms. The signal was -42 dBm. The buffer was already in internal memory, and the camera module was not used.
```

## Before a board goes back

The Wi-Fi password is compiled into whichever sketch was flashed. Erase the whole chip before that board goes back in a package. The microSD card is not part of the chip erase.

```
esptool.py --chip esp32s3 --port /dev/ttyACM0 erase_flash
```
