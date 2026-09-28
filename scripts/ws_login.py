#!/usr/bin/env python3
"""Interactive Wealthsimple login. Stores the session in the macOS Keychain.

Run this once per person (and again only if a session is revoked/expires):

    uv run python scripts/ws_login.py                   # single account
    uv run python scripts/ws_login.py --profile me      # label this login "me"
    uv run python scripts/ws_login.py --profile wife    # second account

With --profile the label is recorded in data/profiles.json so ws_export.py
and ws_summarize.py can be pointed at it, and that profile's files go under
data/<profile>/. Registering a label for an email that already has a stored
session does not ask for the password again.

The session is requested read-only; the stored token cannot place trades.
The person whose account it is should be the one typing the password + 2FA.
"""

import argparse
import sys
from getpass import getpass

import keyring
from ws_profiles import KEYRING_SERVICE, PROFILE_RE, load_profiles, save_profile

from ws_api import (
    LoginFailedException,
    OTPRequiredException,
    WealthsimpleAPI,
    WSAPISession,
)


def persist(session_json: str, username: str) -> None:
    keyring.set_password(f"{KEYRING_SERVICE}.{username}", "session", session_json)


def interactive_login(username: str) -> None:
    # getpass, not input(): input() echoes the password to the terminal and
    # leaves it in scrollback.
    password = getpass("Password: ")
    otp_answer = None
    while True:
        try:
            WealthsimpleAPI.login(
                username,
                password,
                otp_answer,
                persist_session_fct=persist,
                scope=WealthsimpleAPI.SCOPE_READ_ONLY,
            )
            return
        except OTPRequiredException:
            # Wealthsimple may also have just sent an SMS code, depending on
            # how 2FA is configured on the account.
            otp_answer = input("2FA code: ").strip()
        except LoginFailedException:
            # Not printing the exception: its __str__ embeds the raw API
            # response body.
            print("Login failed (bad password or expired 2FA code).", file=sys.stderr)
            password = getpass("Password: ")
            otp_answer = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="short label for this login (e.g. me, wife)")
    parser.add_argument("--username", help="WS account email (prompted if omitted)")
    args = parser.parse_args()

    if args.profile and not PROFILE_RE.match(args.profile):
        print("Profile names: letters, digits, - and _ only.", file=sys.stderr)
        return 1

    username = args.username or load_profiles().get(args.profile or "")
    if not username:
        username = input("Wealthsimple username (email): ").strip()
    if not username:
        print("No username given.", file=sys.stderr)
        return 1

    existing = keyring.get_password(f"{KEYRING_SERVICE}.{username}", "session")
    if existing:
        prompt = "A session is already stored for this email. Log in again? [y/N] "
        if input(prompt).strip().lower() == "y":
            interactive_login(username)
        else:
            print("Keeping existing session.")
    else:
        interactive_login(username)

    stored = keyring.get_password(f"{KEYRING_SERVICE}.{username}", "session")
    if not stored:
        print("Login reported success but no session was stored.", file=sys.stderr)
        return 1

    # Prove the stored session actually works for API calls.
    ws = WealthsimpleAPI.from_token(WSAPISession.from_json(stored), persist, username)
    accounts = ws.get_accounts()

    if args.profile:
        save_profile(args.profile, username)
        print(f"\nProfile '{args.profile}' -> {username} (data/{args.profile}/).")
    print(f"Session saved to Keychain as '{KEYRING_SERVICE}.{username}'.")
    print(f"{len(accounts)} open account(s) visible:")
    for account in accounts:
        print(f"  - {account['description']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
