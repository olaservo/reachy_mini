# Arduino NFC reader firmware (Pro Micro + PN532)

Firmware for the optional NFC reader accessory, implementing the serial line protocol expected by `src/reachy_mini/nfc/reader.py` (the module docstring there is the authoritative protocol spec). Upstream's prototype used an Arduino Nano; this sketch targets the SparkFun Pro Micro DEV-12640 (ATmega32U4, 5V/16 MHz, micro-B USB) but should run on any 32U4 or classic AVR board with I2C pins.

## Hardware

- SparkFun Pro Micro 5V/16 MHz (or compatible)
- PN532 NFC module (Elechouse V3 style) in **I2C mode** — set the mode DIP switches per the board's silkscreen (Elechouse V3: SET0=ON/1, SET1=OFF/0; verify against your board, don't trust this note)
- NTAG213 sticker tags

Wiring (I2C only; IRQ and RESET are not needed):

| PN532 | Pro Micro |
|---|---|
| VCC | VCC |
| GND | GND |
| SDA | 2 (SDA) |
| SCL | 3 (SCL) |

## Build & flash

1. Arduino IDE → Boards Manager → install **SparkFun AVR Boards** (add `https://raw.githubusercontent.com/sparkfun/Arduino_Boards/main/IDE_Board_Manager/package_sparkfun_index.json` to Additional Board Manager URLs first).
2. Select **SparkFun Pro Micro**, processor **ATmega32U4 (5V, 16 MHz)**. Flashing the 3.3V/8 MHz variant makes the board look dead — recover by double-tapping reset (RST→GND twice) to enter the bootloader (8 s window; the COM port changes while in bootloader).
3. Library Manager → install **Adafruit PN532** (≥ 1.3.0; pulls in Adafruit BusIO). The BusIO-based I2C path polls the ready status over the bus, which is why IRQ/RESET need not be wired.
4. Open `arduino_nfc/arduino_nfc.ino`, flash. Expect the COM port to drop and re-enumerate on reset (bootloader and sketch enumerate with different PIDs: `1B4F:9205` vs `1B4F:9206`).

## Bench test

Open the IDE serial monitor (newline line ending; the 115200 baud setting is cosmetic on native USB CDC):

- On connect you should see `READY` (or `NFC_ERROR:NOT_FOUND` repeating every 2 s if the PN532 isn't answering — recheck wiring and DIP switches).
- Present a blank NTAG213 → `EMPTY:<uid>`; remove it → `NO_TAG` after ~2 s.
- Send `WRITE:HELLO` → `WRITE_PENDING`, present a tag → `WRITE_OK` then `READ:<uid>:HELLO`.
- The tag is a standard NDEF Text record, so any phone NFC app can read/write it for debugging.

Then test through the `NfcReader` service (from the repo root, in its venv):

```
python examples/arduino_nfc/reader_bench.py            # auto-detect
python examples/arduino_nfc/reader_bench.py COM5       # explicit port
```

Auto-detection matches USB vendor ids `0x1A86` (CH340/Nano) and `0x1B4F` (SparkFun), excluding the Reachy motor controller's PID. If your board enumerates with a different VID (clones do), pass the port explicitly here, or `--nfc-port` on the daemon.

## Tag format

An NDEF Text record (UTF-8, language `en`) written from page 4 of the NTAG213's user memory, standard TLV framing, so tags interoperate with phones. Reading walks the TLV structure (skipping the factory lock-control TLV on fresh tags) and reports the first non-empty Text record; anything else — empty NDEF TLV, no NDEF TLV, non-Text record — reports as `EMPTY`. Writes overwrite from page 4 and are verified by read-back.
