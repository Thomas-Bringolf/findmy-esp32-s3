#!/bin/bash
# Scan for Find My beacons using bluetoothctl (no extra deps)
# Run with: sudo ./scan_findmy_btctl.sh

DURATION=${1:-15}

echo "Scanning for Find My beacons for ${DURATION}s using bluetoothctl..."
echo "============================================================"

# Start scan in background
bluetoothctl scan on > /dev/null 2>&1 &
SCAN_PID=$!

# Wait for scan duration
sleep "$DURATION"

# Stop scan
kill $SCAN_PID 2>/dev/null
bluetoothctl scan off > /dev/null 2>&1

# Get device list and check for Apple manufacturer data
echo ""
echo "Devices found:"
bluetoothctl devices | while read -r _ MAC NAME; do
    # Get detailed info for each device
    INFO=$(bluetoothctl info "$MAC" 2>/dev/null)
    if echo "$INFO" | grep -qi "manufacturer.*004c\|manufacturer.*4c 00"; then
        echo "🎯 FOUND Find My beacon: $MAC $NAME"
        echo "$INFO" | grep -A2 -B2 -i "manufacturer\|rssi\|tx power"
        echo ""
    fi
done

echo "============================================================"
echo "Done. If nothing found, try: sudo btmon (in another terminal)"