#!/usr/bin/env python3
"""
fetch_once.py - one Apple Find My report request for keys captured off the air.

Usage:
    python3 old/fetch_once.py                    the two beacons captured 16:23
    python3 old/fetch_once.py <mac> <payload-hex> ...  any capture, in pairs
    python3 old/fetch_once.py --selftest         prove the key reconstruction

`findmy-toolbox.py scan` prints a BLE address and the 27 byte offline finding
payload (12 19 status key[6:28] key[0]>>6 hint).  The 28 byte advertisement
key behind such a capture is rebuilt as:

    key[6:28]  in the payload
    key[0]     low 6 bits in the address, top 2 bits are the high byte
    key[1:5]   in the address - our firmware advertises (key[0]|0b11) ||
               key[1..5] like Apple does, but captures taken before the
               byte-order fix carry that address reversed, so both orders
               are tried
    key[5]     masked to 6 bits by the pre-fix firmware's address rule, so
               all four values are tried

That is 5 candidate keys per capture.  All of them are hashed with sha256 and
asked for in ONE request; Apple answers with the id of every key it holds
reports for, so a hit names the winning candidate.  Reports arrive encrypted,
so only the cleartext header (timestamp, confidence) can be shown without the
accessory's private key.
"""

import argparse
import asyncio
import base64
import hashlib
import sys
from pathlib import Path

from findmy import (
    AsyncAppleAccount,
    KeyPair,
    LoginState,
    UnauthorizedError,
    UnhandledProtocolError,
)
from findmy.errors import EmptyResponseError

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SESSION_FILE = SCRIPTS_DIR / "state" / "account_state.json"

CAPTURED = [
    ("F6:8C:E1:60:DA:E9", "121900c5d2c4eea05bf95714fa294eb39170d4f7525a881fd40000"),
    ("D1:C6:2B:CB:9A:3B", "1219004d4a5a342931df74ab2646642a9e33d7e6ee19d0c18e0200"),
]


def parse_capture(payload_hex: str) -> tuple[int, bytes, int]:
    """(status, key[6:28], key[0]>>6) out of one scan payload hex string."""
    data = bytes.fromhex(payload_hex)
    if len(data) != 27 or data[0] != 0x12 or data[1] != 0x19:
        raise ValueError(
            f"payload is not 27 bytes starting 12 19: {len(data)} bytes")
    return data[2], data[3:25], data[25] & 0x03


def candidates(mac: str, suffix: bytes, high: int) -> dict[str, bytes]:
    """Candidate advertisement keys for one capture, keyed by a label."""
    addr = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(addr) != 6:
        raise ValueError(f"bad MAC address {mac!r}")
    out = {"apple": bytes([(high << 6) | (addr[0] & 0x3F), *addr[1:6], *suffix])}
    for b5_top in range(4):
        out[f"esp32 key5={b5_top:02b}"] = bytes(
            [(high << 6) | (addr[5] & 0x3F), *addr[4:0:-1],
             (addr[0] & 0x3F) | (b5_top << 6), *suffix])
    return out


def hashed_b64(key: bytes) -> str:
    """The id Apple knows a key by: base64(sha256(advertisement key))."""
    return base64.b64encode(hashlib.sha256(key).digest()).decode("ascii")


def selftest(rounds: int = 64) -> int:
    """Rebuild random keys from their own payload + address, both orders."""
    from cryptography.hazmat.primitives.asymmetric import ec

    for i in range(rounds):
        priv = ec.generate_private_key(ec.SECP224R1())
        key = KeyPair(priv.private_numbers().private_value.to_bytes(28, "big"))
        adv = key.adv_key_bytes
        status, suffix, high = parse_capture(key.of_data(status=0, hint=0).hex())
        firmware = bytes([adv[0] | 0xC0, *adv[1:5], (adv[5] & 0x3F) | 0xC0])
        esp_mac = ":".join(f"{b:02X}" for b in reversed(firmware))
        apple_mac = ":".join(
            f"{b:02X}" for b in bytes([adv[0] | 0xC0, *adv[1:6]]))
        hits = [f"{mac} / {label}"
                for mac in (esp_mac, apple_mac)
                for label, cand in candidates(mac, suffix, high).items()
                if cand == adv]
        if not hits:
            print(f"FAIL round {i}: key {adv.hex()}")
            print(f"  esp32 address {esp_mac}")
            print(f"  apple address {apple_mac}")
            return 1
        if hashed_b64(adv) != key.hashed_adv_key_b64:
            print(f"FAIL round {i}: sha256 mismatch")
            return 1
        _ = status
    print(f"PASS: {rounds}/{rounds} keys rebuilt from payload + address "
          f"(address orders and key[5] top bits covered)")
    return 0


async def fetch(pairs: list[tuple[str, str]]) -> int:
    """Ask Apple for reports under every candidate key of every capture."""
    entries: list[dict] = []
    for mac, payload_hex in pairs:
        status, suffix, high = parse_capture(payload_hex)
        print(f"capture {mac}: status=0x{status:02x} high={high:02b} "
              f"key[6:28]={suffix.hex()}")
        for label, key in candidates(mac, suffix, high).items():
            entries.append({"mac": mac, "label": label, "hashed": hashed_b64(key)})
    print(f"{len(pairs)} capture(s) -> {len(entries)} candidate key(s), "
          f"one request")
    for entry in entries:
        print(f"  {entry['mac']}  {entry['label']:<14} {entry['hashed']}")

    if not SESSION_FILE.exists():
        print(f"no saved session at {SESSION_FILE} - run 'apple-login' first")
        return 1
    account = AsyncAppleAccount.from_json(str(SESSION_FILE))
    if account.login_state != LoginState.LOGGED_IN:
        print(f"session state is {account.login_state.name} - run 'apple-login'")
        await account.close()
        return 1

    seen: dict = {}
    orig_post = account._http.post

    async def post(url, **kwargs):
        resp = await orig_post(url, **kwargs)
        seen["status"] = resp.status_code
        seen["body"] = resp.text()
        return resp

    account._http.post = post
    try:
        reports = await account.fetch_raw_reports(
            [([entry["hashed"] for entry in entries], [])])
    except EmptyResponseError:
        print(f"Apple sent an EMPTY body (http={seen.get('status')}) - the "
              "known empty response bug (FindMy.py #185), not a ban")
        await account.close()
        return 2
    except UnauthorizedError:
        print("Apple answered 401 - session expired, run 'apple-login'")
        await account.close()
        return 3
    except (UnhandledProtocolError, Exception) as exc:
        print(f"{type(exc).__name__}: {exc}")
        print(f"  http={seen.get('status')} "
              f"body={str(seen.get('body', ''))[:300]}")
        await account.close()
        return 1

    body = str(seen.get("body", ""))
    print(f"\nrequest: http={seen.get('status')} body_len={len(body)}")
    print(f"  body: {body[:400]}")
    by_id = {entry["hashed"]: entry for entry in entries}
    print(f"{len(reports)} report(s) for the last 7 days")
    for rep in reports:
        entry = by_id.get(rep.hashed_adv_key_b64, {})
        print(f"  {entry.get('mac', '?')}  {entry.get('label', '?')}  "
              f"{rep.timestamp}  confidence={rep.confidence}  "
              f"payload={len(rep.payload)}B (encrypted)")
    await account.close()

    if reports:
        hit = sorted({by_id[rep.hashed_adv_key_b64]["mac"]
                      for rep in reports if rep.hashed_adv_key_b64 in by_id})
        print(f"HIT: Apple holds reports for {', '.join(hit)} - the session "
              "is good")
        return 0
    print("0 reports for every candidate key - Apple accepted the request "
          "(HTTP 200, statusCode 200), so the session is good and nobody "
          "logged a location for these keys in the last 7 days")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="old/fetch_once.py",
        description="one Apple Find My report request for keys captured "
                    "with findmy-toolbox.py scan")
    ap.add_argument("--selftest", action="store_true",
                    help="rebuild random keys from payload + address and exit")
    ap.add_argument("capture", nargs="*", metavar="MAC PAYLOAD_HEX",
                    help="capture pairs (default: the two beacons from 16:23)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.capture:
        if len(args.capture) % 2:
            ap.error("give MAC address and payload hex in pairs")
        pairs = list(zip(args.capture[::2], args.capture[1::2]))
    else:
        pairs = CAPTURED
    return asyncio.run(fetch(pairs))


if __name__ == "__main__":
    sys.exit(main())
