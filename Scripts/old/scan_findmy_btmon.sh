#!/bin/bash
# Raw BLE packet capture with btmon (kernel-level, shows everything)
# Run with: sudo ./scan_findmy_btmon.sh

DURATION=${1:-15}

echo "Starting btmon for ${DURATION}s..."
echo "This shows ALL raw HCI packets - look for 'Company: Apple (0x004c)'"
echo "============================================================"

sudo timeout "$DURATION" btmon | grep -A10 -B2 -i "apple\|004c\|4c 00" | head -100

echo "============================================================"
echo "Done. Run without grep to see all packets: sudo btmon"