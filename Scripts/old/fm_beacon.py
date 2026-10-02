#!/usr/bin/env python3
"""Helpers matching what the ESP32 firmware actually puts on the air.

The firmware can only advertise a NimBLE random static address, which
requires the top two bits of address byte 5 to be 11. findmy's
KeyPair.mac_address only sets those bits on byte 0, so for keys whose
byte 5 does not already have them the computed MAC differs from the one
the beacon broadcasts. Use beacon_mac() everywhere a captured MAC is
compared against a key.

The status byte of the Offline Finding frame is 0x01 while the firmware
runs its post-boot debug window and 0x00 afterwards, so payload compares
must ignore it.
"""
from findmy.keys import HasPublicKey

OF_TYPE = 0x12
OF_LEN = 0x19


def beacon_mac(key: HasPublicKey) -> str:
    """MAC the beacon broadcasts, in the order bleak/BlueZ display it.

    The firmware's address bytes are b0..b5; BLE stacks print them reversed
    (b5..b0), so the result is reversed here too.
    """
    b = key.adv_key_bytes
    addr = (b[0] | 0xC0, b[1], b[2], b[3], b[4], (b[5] & 0x3F) | 0xC0)
    return ":".join(f"{x:02X}" for x in reversed(addr))


def payload_matches(mfr: bytes, key: HasPublicKey) -> bool:
    """True if a captured Apple manufacturer payload advertises this key."""
    if mfr is None or len(mfr) < 4:
        return False
    if mfr[0] != OF_TYPE or mfr[1] != OF_LEN:
        return False
    expected = key.of_data(status=0, hint=0)
    if len(mfr) != len(expected):
        return False
    # mfr[2] is the status byte, which the firmware varies
    return mfr[:2] == expected[:2] and mfr[3:] == expected[3:]
