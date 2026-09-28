"""Profile registry shared by ws_login / ws_export / ws_summarize.

A profile is a short label ("me", "wife") for one Wealthsimple login. The
session itself lives in the macOS Keychain keyed by the login email, exactly
as before; the registry only maps label -> email so the other scripts can
take `--profile wife` instead of an email, and so each profile gets its own
folder under data/ (data/wife/ws-snapshot-<date>.json, ...).

Running without --profile keeps the original single-account behaviour and the
original flat layout in data/.
"""

import json
import os
import re
import subprocess
from pathlib import Path

KEYRING_SERVICE = "wealthsimple.api"
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
PROFILES_PATH = DATA_DIR / "profiles.json"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def load_profiles() -> dict[str, str]:
    """label -> email. Empty dict when no registry exists yet."""
    if not PROFILES_PATH.exists():
        return {}
    try:
        data = json.loads(PROFILES_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def save_profile(label: str, username: str) -> None:
    if not PROFILE_RE.match(label):
        raise ValueError(f"invalid profile name {label!r} (letters, digits, - and _ only)")
    profiles = load_profiles()
    profiles[label] = username
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_PATH.write_text(json.dumps(profiles, indent=2, sort_keys=True) + "\n")
    os.chmod(PROFILES_PATH, 0o600)


def data_dir(profile: str | None) -> Path:
    """Where a profile's files live. None -> legacy flat data/ layout."""
    return DATA_DIR / profile if profile else DATA_DIR


def keychain_usernames() -> list[str]:
    """Emails that have a stored session, from Keychain metadata only."""
    try:
        dump = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(set(re.findall(
        rf'"svce"<blob>="{re.escape(KEYRING_SERVICE)}\.([^"]+)"', dump)))


def resolve_username(profile: str | None, username: str | None) -> tuple[str | None, str]:
    """Pick the login email for a run.

    Returns (email or None, reason). Order: --profile, --username,
    $WS_USERNAME, the only registered profile, the only Keychain session.
    """
    if profile:
        email = load_profiles().get(profile)
        if not email:
            known = ", ".join(sorted(load_profiles())) or "none registered"
            return None, f"unknown profile {profile!r} (known: {known})"
        return email, f"profile {profile}"
    if username:
        return username, "--username"
    if os.environ.get("WS_USERNAME"):
        return os.environ["WS_USERNAME"], "$WS_USERNAME"
    profiles = load_profiles()
    if len(profiles) == 1:
        return next(iter(profiles.values())), "only registered profile"
    if len(profiles) > 1:
        return None, ("several profiles registered (" + ", ".join(sorted(profiles))
                      + "); pass --profile NAME or --all")
    found = keychain_usernames()
    if len(found) == 1:
        return found[0], "only Keychain session"
    if len(found) > 1:
        return None, "several Keychain sessions; pass --username or register profiles"
    return None, "no stored session; run scripts/ws_login.py first"
