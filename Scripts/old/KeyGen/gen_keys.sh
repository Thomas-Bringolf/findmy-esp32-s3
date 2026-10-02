#!/usr/bin/env bash
# Generate a P-224 keypair for the Find My beacon.
# private_N.key : 28-byte private scalar, base64
# public_N.key  : 56-byte uncompressed point (X||Y), base64
set -euo pipefail

OUT_DIR="${1:-keys}"
mkdir -p "$OUT_DIR"

idx=0
while [[ -f "$OUT_DIR/private_${idx}.key" || -f "$OUT_DIR/public_${idx}.key" ]]; do
    idx=$((idx + 1))
done

PRIV_KEY="$OUT_DIR/private_${idx}.key"
PUB_KEY="$OUT_DIR/public_${idx}.key"

python3 - "$PRIV_KEY" "$PUB_KEY" <<'PY'
import base64, sys
from cryptography.hazmat.primitives.asymmetric import ec

priv_path, pub_path = sys.argv[1], sys.argv[2]

key = ec.generate_private_key(ec.SECP224R1())
priv = key.private_numbers().private_value
pub = key.public_key().public_numbers()

priv_b64 = base64.b64encode(priv.to_bytes(28, "big")).decode()
pub_b64 = base64.b64encode(
    pub.x.to_bytes(28, "big") + pub.y.to_bytes(28, "big")
).decode()

with open(priv_path, "w") as f:
    f.write(priv_b64 + "\n")
with open(pub_path, "w") as f:
    f.write(pub_b64 + "\n")
PY

chmod 600 "$PRIV_KEY"
chmod 644 "$PUB_KEY"

# sanity: the private scalar must derive the public X
python3 - "$PRIV_KEY" "$PUB_KEY" <<'PY'
import base64, sys
from findmy.keys import KeyPair

priv = base64.b64decode(open(sys.argv[1]).read().strip())
pub = base64.b64decode(open(sys.argv[2]).read().strip())
derived = KeyPair(priv).adv_key_bytes
assert derived == pub[:28], "private/public mismatch!"
print("verified: private scalar derives public X")
PY

echo "Generated P-224 keypair:"
echo "  Private: $(cat "$PRIV_KEY")"
echo "  Public:  $(cat "$PUB_KEY")"
