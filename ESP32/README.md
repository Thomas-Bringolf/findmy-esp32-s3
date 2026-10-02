# ESP32-S3 Find My firmware (adapted from OpenHaystack)

> **This tree is not stock OpenHaystack.** It advertises Apple's rotating
> Offline Finding frames (SK chain, keys derived on the ESP32), pairs over
> the UART console (`KEYS` / `PIN` / `WIPE`), and builds with **ESP-IDF 6.1**
> for the ESP32-S3. See [`../docs/firmware.md`](../docs/firmware.md) and
> [`../docs/uart-protocol.md`](../docs/uart-protocol.md) for what actually
> happens, and [`../Scripts/findmy-toolbox.py`](../Scripts/README.md) for the
> host tooling. The rest of this file is the upstream text.

This project contains a PoC firmware for Espressif ESP32 chips (like ESP32-WROOM or ESP32-WROVER, but _not_ ESP32-S2).
After flashing our firmware, the device sends out Bluetooth Low Energy advertisements such that it can be found by [Apple's Find My network](https://developer.apple.com/find-my/).

## Disclaimer

Note that the firmware is just a proof-of-concept and currently only implements advertising a single static key. This means that **devices running this firmware are trackable** by other devices in proximity.

## Requirements

To change and rebuild the firmware, you need Espressif's IoT Development Framework (ESP-IDF).
Installation instructions for the latest version of the ESP-IDF can be found in [its documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/).
This repository builds it with ESP-IDF v6.1 (upstream was tested on 4.2).

For deploying the firmware, you need Python 3 on your path, either as `python3` (preferred) or as `python`, and the `venv` module needs to be available.

## Build

With the ESP-IDF on your `$PATH`, you can use `idf.py` to build the application from within this directory:

```bash
idf.py build
```

This will create the following files:

- `build/bootloader/bootloader.bin` -- The second stage bootloader
- `build/partition_table/partition-table.bin` -- The partition table
- `build/openhaystack.bin` -- The application itself

These files are required for the next step: Deploy the firmware.

## Deploy the Firmware

Flash with `idf.py` (there is no key to pass: keys are provisioned at run
time over the console, see `pair`):

```bash
idf.py -p /dev/ttyACM0 flash
```

The board resets into a **locked** console with the factory PIN
`00000000` (config mode). Pair it from the host:

```bash
cd ../Scripts
./findmy-toolbox.py pair --force
```
