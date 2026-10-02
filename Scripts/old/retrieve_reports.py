#!/usr/bin/env python3
"""
Retrieve Find My location reports for a given public key.
Uses the `findmy` library with 2FA authentication.

Usage:
    python3 retrieve_reports.py <apple_id> [--watch SECONDS]

    Keys are read from ../KeyGen/keys/{private_0,public_0}.key (same files
    the firmware is built from). --watch retries every SECONDS until a
    report shows up.
"""

import asyncio
import sys
import base64
from pathlib import Path
from findmy import AsyncAppleAccount, FixedRollingKeyPairAccessory, LocalAnisetteProvider
from findmy.keys import KeyPair
from findmy.reports import LoginState
from findmy.errors import InvalidCredentialsError, UnauthorizedError

# Keys are read from KeyGen/keys (same files the firmware is built with)
SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
STATE_DIR = SCRIPTS_DIR / "state"
KEYS_DIR = ROOT / "KeyGen" / "keys"
PRIVATE_KEY_FILE = KEYS_DIR / "private_0.key"
PUBLIC_KEY_FILE = KEYS_DIR / "public_0.key"

# Saved session so you only do 2FA once
SESSION_FILE = STATE_DIR / "account_state.json"


async def get_reports(apple_id, private_key_b64: str, public_key_b64: str = None, watch: int = 0):
    """Fetch and decrypt location reports for the given key."""

    # Decode private key
    private_key = base64.b64decode(private_key_b64)
    if len(private_key) != 28:
        print(f"Error: Private key must be 28 bytes, got {len(private_key)}")
        return

    # Create keypair from private key (P-224)
    keypair = KeyPair(private_key)

    # Public key X (28 bytes) - the value the beacon advertises
    x_coord = keypair.adv_key_bytes
    print(f"Public key X (28 bytes): {x_coord.hex()}")

    # Cross-check against the key file the firmware was built with
    if public_key_b64:
        file_pub = base64.b64decode(public_key_b64)
        print(f"Key file  X (28 bytes): {file_pub[:28].hex()}")
        if file_pub[:28] != x_coord:
            print("ERROR: private key does NOT match public key file!")
            print("       The beacon advertises a key we cannot decrypt.")
            return
        print("OK: private key matches the advertised public key")
    print(f"Lookup key (sha256 of X): {keypair.hashed_adv_key_b64}")

    # Create accessory object
    accessory = FixedRollingKeyPairAccessory(private_keys=[private_key], identifier="esp32-s3-tracker")
    
    # Login with Apple ID (restore saved session if available)
    account = None
    if SESSION_FILE.exists():
        try:
            account = AsyncAppleAccount.from_json(str(SESSION_FILE))
            if account.login_state == LoginState.LOGGED_IN:
                print(f"Restored saved session for {account.account_name} (no 2FA needed)")
            else:
                print(f"Saved session state: {account.login_state}, re-login required")
                account = None
        except Exception as e:
            print(f"Could not restore session: {e}")
            account = None

    if account is None:
        if not apple_id:
            apple_id = input("\nApple ID: ").strip()
        print(f"\nLogging in as {apple_id}...")
        # Read password from stdin (works in pipes)
        print("Apple ID password: ", end="", flush=True)
        password = sys.stdin.readline().strip()

        # Create anisette provider for authentication
        anisette = LocalAnisetteProvider()
        account = AsyncAppleAccount(anisette)

        try:
            # Start login - this will prompt for password and 2FA
            login_state = await account.login(apple_id, password)
            print(f"Login state: {login_state}")

            # Handle 2FA if required
            if login_state == LoginState.REQUIRE_2FA:
                print("2FA required. Getting available methods...")
                methods = await account.get_2fa_methods()
                if not methods:
                    print("No 2FA methods available!")
                    return

                print("Available 2FA methods:")
                for i, method in enumerate(methods):
                    if hasattr(method, 'phone_number'):
                        print(f"  {i}: SMS to {method.phone_number}")
                    else:
                        print(f"  {i}: Trusted device push")

                # Use first method (prefer trusted device)
                method = methods[0]
                print(f"Using method: {type(method).__name__}")

                # Request the 2FA challenge
                print("Sending 2FA challenge...")
                await method.request()
                print("Check your trusted device or SMS for the code.")

                # Get code from user
                code = input("Enter 2FA code: ").strip()

                # Submit the code
                print("Submitting 2FA code...")
                login_state = await method.submit(code)
                print(f"Login state after 2FA: {login_state}")

                if login_state != LoginState.LOGGED_IN:
                    print(f"2FA failed. State: {login_state}")
                    return
                else:
                    print("2FA successful!")

            if login_state != LoginState.LOGGED_IN:
                print(f"Unexpected login state: {login_state}")
                return

            print("Login successful!")

            # Persist session to skip 2FA next time
            try:
                account.to_json(str(SESSION_FILE))
                SESSION_FILE.chmod(0o600)
                print(f"Session saved to {SESSION_FILE}")
            except Exception as e:
                print(f"Could not save session: {e}")

        except InvalidCredentialsError:
            print("Error: Invalid Apple ID or password")
            return
        except UnauthorizedError:
            print("Error: 2FA required or failed")
            return
        except Exception as e:
            print(f"Login error: {e}")
            import traceback
            traceback.print_exc()
            return
    
    # Fetch reports, optionally polling until Apple has any data
    attempt = 0
    try:
        while True:
            attempt += 1
            print(f"\nFetching location reports (attempt {attempt})...")

            # Static key -> queried as primaryIds (the direct lookup path)
            reports = await account.fetch_location_history(keypair)
            print(f"Primary-key lookup: {len(reports)} report(s)")

            if not reports:
                # Fallback: accessory path queries the key as secondaryIds
                reports = await account.fetch_location_history(accessory)
                print(f"Accessory (secondary-key) lookup: {len(reports)} report(s)")

            if reports:
                print("\n" + "=" * 70)
                for i, report in enumerate(reports):
                    print(f"\nReport #{i+1}:")
                    print(f"  Time:       {report.timestamp}")
                    print(f"  Latitude:   {report.latitude}")
                    print(f"  Longitude:  {report.longitude}")
                    print(f"  Accuracy:   {report.horizontal_accuracy}m")
                    print(f"  Confidence: {report.confidence}%")
                    print(f"  Status:     {report.status}")
                print("\n" + "=" * 70)
                return

            if not watch:
                print("\nNo reports found.")
                print("Apple accepted the query (HTTP 200) but has no location for this key.")
                print("An Apple device must hear the beacon first - keep it powered and")
                print("near a locked iPhone/iPad, then retry (or use --watch 600).")
                return

            print(f"No reports yet; retrying in {watch}s (Ctrl-C to stop)")
            await asyncio.sleep(watch)

    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print(f"Error fetching reports: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            await account.close()
        except Exception:
            pass


def main():
    if not PRIVATE_KEY_FILE.exists() or not PUBLIC_KEY_FILE.exists():
        print(f"Missing keys in {KEYS_DIR} - run KeyGen/gen_keys.sh first")
        sys.exit(1)

    private_key_b64 = PRIVATE_KEY_FILE.read_text().strip()
    public_key_b64 = PUBLIC_KEY_FILE.read_text().strip()

    args = sys.argv[1:]
    watch = 0
    if "--watch" in args:
        i = args.index("--watch")
        try:
            watch = int(args[i + 1])
            del args[i:i + 2]
        except (IndexError, ValueError):
            print("--watch requires a number of seconds, e.g. --watch 600")
            sys.exit(1)

    apple_id = args[0] if args else None
    if len(args) > 1:
        private_key_b64 = args[1]
    if len(args) > 2:
        public_key_b64 = args[2]

    asyncio.run(get_reports(apple_id, private_key_b64, public_key_b64, watch))


if __name__ == "__main__":
    main()
