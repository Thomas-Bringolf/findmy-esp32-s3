#!/usr/bin/env python3
"""
Scan for Find My (Apple) BLE advertisements.
Requires: pip install bleak
Run with: sudo python3 scan_findmy.py
"""
import asyncio
import sys
from bleak import BleakScanner

TARGET_COMPANY_ID = 0x004C  # Apple

def parse_findmy_payload(data: bytes) -> dict:
    """Parse Apple Offline Finding payload from manufacturer data value.
    
    bleak returns manufacturer data value starting at the type byte:
    12 19 <25 bytes payload...>
    """
    if len(data) < 2:
        return {"error": f"Too short ({len(data)} bytes)", "raw": data.hex(), "adv_type": None}
    
    adv_type = data[0]
    payload_len = data[1]
    
    if adv_type != 0x12:
        return {"error": f"Not Offline Finding (type=0x{adv_type:02x})", "raw": data.hex(), "adv_type": adv_type}
    
    if len(data) < 2 + payload_len:
        return {"error": f"Length mismatch (declared {payload_len}, have {len(data)-2})", "raw": data.hex(), "adv_type": adv_type}
    
    payload = data[2:2+payload_len]
    if len(payload) < 23:
        return {"error": f"Payload too short ({len(payload)} bytes)", "raw": data.hex(), "adv_type": adv_type}
    
    state = payload[0]
    public_key_x = payload[1:23]  # 22 bytes (X[6..27])
    key_bits = payload[23] if len(payload) > 23 else 0
    hint = payload[24] if len(payload) > 24 else 0
    
    return {
        "state": f"0x{state:02x}",
        "public_key_x_suffix": public_key_x.hex(),
        "key_high_bits": f"0x{key_bits:02x}",
        "hint": f"0x{hint:02x}",
        "raw_mfr_data": data.hex(),
        "adv_type": adv_type
    }

async def scan_findmy(duration: float = 15.0):
    print(f"Scanning for Find My beacons for {duration}s...")
    print("=" * 70)
    
    def callback(device, adv_data):
        mfr_data = adv_data.manufacturer_data
        if TARGET_COMPANY_ID in mfr_data:
            payload = mfr_data[TARGET_COMPANY_ID]
            parsed = parse_findmy_payload(payload)
            # Only print if IS an Offline Finding (adv_type == 0x12) AND valid payload
            if parsed.get("adv_type") == 0x12 and "error" not in parsed:
                rssi = adv_data.rssi if hasattr(adv_data, 'rssi') else 'N/A'
                print(f"\n🎯 FOUND Find My beacon!")
                print(f"   Address: {device.address}")
                print(f"   RSSI:    {rssi} dBm")
                print(f"   Name:    {device.name or 'Unknown'}")
                for k, v in parsed.items():
                    print(f"   {k}: {v}")
    
    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()
    print("\n" + "=" * 70)
    print("Scan complete.")

if __name__ == "__main__":
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    try:
        asyncio.run(scan_findmy(duration))
    except PermissionError:
        print("ERROR: Need root for BLE scanning. Run with: sudo python3 scan_findmy.py")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)