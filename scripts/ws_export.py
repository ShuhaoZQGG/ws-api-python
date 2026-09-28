#!/usr/bin/env python3
"""Export a Wealthsimple portfolio snapshot to JSON for offline/agent analysis.

    uv run python scripts/ws_export.py                  # single account
    uv run python scripts/ws_export.py --profile wife   # one registered profile
    uv run python scripts/ws_export.py --all            # every registered profile

Reads the session saved by scripts/ws_login.py from the Keychain, pulls a
read-only snapshot, and writes it to data/ws-snapshot-<date>.json, or to
data/<profile>/ws-snapshot-<date>.json when a profile is used.

Nothing here prints balances to stdout, and API exceptions are never printed
raw -- WSApiException.__str__ embeds the full response body, which for this
API means holdings and account numbers.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import keyring
from ws_profiles import (
    DATA_DIR,
    KEYRING_SERVICE,
    data_dir,
    load_profiles,
    resolve_username,
)

from ws_api import WealthsimpleAPI, WSApiException, WSAPISession

SCHEMA_VERSION = 1


def make_security_cache(cache_dir: Path):
    """On-disk cache so symbol lookups don't re-hit the API every run."""
    cache_dir.mkdir(parents=True, exist_ok=True)

    def path_for(security_id: str) -> Path:
        return cache_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', security_id)}.json"

    def getter(security_id: str):
        path = path_for(security_id)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def setter(security_id: str, market_data):
        path_for(security_id).write_text(json.dumps(market_data))
        return market_data

    return getter, setter


def symbol_of(ws, security_id: str) -> str | None:
    """Resolve a WS security id to EXCHANGE:SYMBOL, or None if unavailable."""
    if not security_id or security_id in ("sec-c-cad", "sec-c-usd"):
        return None
    try:
        data = ws.get_security_market_data(security_id)
    except WSApiException:
        return None
    stock = (data or {}).get("stock") or {}
    if stock.get("symbol"):
        exchange = stock.get("primaryExchange")
        return f"{exchange}:{stock['symbol']}" if exchange else stock["symbol"]
    return None


def guarded(label: str, fn, errors: list):
    """Run a fetch, recording failures in the snapshot instead of crashing."""
    try:
        return fn()
    except WSApiException as exc:
        # args[0] only: str(exc) would splice in the full response body.
        errors.append({"step": label, "error": exc.args[0] if exc.args else "failed"})
    except Exception as exc:  # noqa: BLE001 - one bad section shouldn't kill the run
        errors.append({"step": label, "error": f"{type(exc).__name__}"})
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--profile", help="registered profile label (see ws_login.py)")
    who.add_argument("--all", action="store_true",
                     help="export every profile in data/profiles.json")
    who.add_argument("--username", help="WS account email (auto-detected if omitted)")
    parser.add_argument("--out", help="output path (default: <data dir>/ws-snapshot-<date>.json; "
                                      "not allowed with --all)")
    parser.add_argument("--activity-months", type=int, default=12,
                        help="months of transaction history to pull (default: 12)")
    parser.add_argument("--currency", default="CAD", choices=["CAD", "USD"])
    parser.add_argument("--no-card-details", action="store_true",
                        help="skip the per-transaction credit-card detail calls "
                             "(original currency, FX rate, fees)")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.all:
        if args.out:
            parser.error("--out cannot be combined with --all")
        profiles = load_profiles()
        if not profiles:
            print("No profiles registered. Run scripts/ws_login.py --profile NAME first.",
                  file=sys.stderr)
            return 1
        failures = 0
        for label, email in sorted(profiles.items()):
            print(f"=== {label} ===", file=sys.stderr)
            failures += export_one(email, data_dir(label), args) != 0
        return 1 if failures else 0

    username, reason = resolve_username(args.profile, args.username)
    if not username:
        print(f"Could not determine username: {reason}.", file=sys.stderr)
        return 1
    return export_one(username, data_dir(args.profile), args)


def export_one(username: str, out_dir: Path, args: argparse.Namespace) -> int:
    stored = keyring.get_password(f"{KEYRING_SERVICE}.{username}", "session")
    if not stored:
        print(f"No stored session for {username}. Run scripts/ws_login.py first.",
              file=sys.stderr)
        return 1

    def persist(session_json: str, uname: str) -> None:
        keyring.set_password(f"{KEYRING_SERVICE}.{uname}", "session", session_json)

    try:
        ws = WealthsimpleAPI.from_token(WSAPISession.from_json(stored), persist, username)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open session ({type(exc).__name__}). "
              f"Re-run scripts/ws_login.py.", file=sys.stderr)
        return 1

    # The symbol cache is keyed by security id, not by person -- share it.
    getter, setter = make_security_cache(DATA_DIR / ".security-cache")
    ws.set_security_market_data_cache(getter, setter)

    currency = args.currency
    errors: list = []
    now = datetime.now()
    start = now - timedelta(days=31 * args.activity_months)

    print("Fetching accounts...", file=sys.stderr)
    accounts = guarded("accounts", lambda: ws.get_accounts(), errors) or []
    account_ids = [a["id"] for a in accounts]
    tradeable = [a for a in accounts if "credit-card" not in a["id"]]

    print(f"Fetching positions across {len(accounts)} account(s)...", file=sys.stderr)
    positions = guarded(
        "positions", lambda: ws.get_identity_positions(None, currency), errors) or []
    for position in positions:
        security_id = ((position.get("security") or {}).get("id"))
        position["symbol"] = symbol_of(ws, security_id)

    print("Fetching balances and P&L per account...", file=sys.stderr)
    per_account = {}
    for account in tradeable:
        account_id = account["id"]
        per_account[account_id] = {
            "balances": guarded(
                f"balances[{account_id}]",
                lambda aid=account_id: ws.get_account_balances(aid), errors),
            "unrealized_pnl": guarded(
                f"unrealized_pnl[{account_id}]",
                lambda aid=account_id: ws.get_account_unrealized_pnl(aid, currency),
                errors),
        }
    for account in accounts:
        if "credit-card" in account["id"]:
            per_account[account["id"]] = {
                "credit_card": guarded(
                    f"credit_card[{account['id']}]",
                    lambda aid=account["id"]: ws.get_creditcard_account(aid), errors),
            }

    print(f"Fetching {args.activity_months} months of activity...", file=sys.stderr)
    activities = guarded(
        "activities",
        lambda: ws.get_activities(account_ids, how_many=100, start_date=start,
                                  load_all=True),
        errors) or []

    # The feed only carries the CAD-converted amount for card purchases. The
    # per-transaction detail query adds the original currency/amount, the FX
    # rate applied and any fees -- one call per card line, so it is optional.
    card_lines = [a for a in activities
                  if a.get("type") == "CREDIT_CARD" and a.get("externalCanonicalId")]
    if card_lines and not args.no_card_details:
        print(f"Fetching detail for {len(card_lines)} credit-card transaction(s)...",
              file=sys.stderr)
        for act in card_lines:
            act["cardDetail"] = guarded(
                f"card_detail[{act['externalCanonicalId']}]",
                lambda aid=act["externalCanonicalId"]: ws.get_creditcard_activity(aid),
                errors)

    print("Fetching returns, dividends, history...", file=sys.stderr)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "currency": currency,
        "source": "wealthsimple-graphql-api (read-only scope)",
        "accounts": accounts,
        "per_account": per_account,
        "positions": positions,
        "current_financials": guarded(
            "current_financials",
            lambda: ws.get_identity_current_financials(currency, account_ids), errors),
        "realized_returns": guarded(
            "realized_returns",
            lambda: ws.get_identity_realized_returns(currency, account_ids, first=100),
            errors),
        "dividends": guarded(
            "dividends",
            lambda: ws.get_dividends(currency, account_ids,
                                     include_issuing_security_breakdown=True), errors),
        "historical_financials": guarded(
            "historical_financials",
            lambda: ws.get_identity_historical_financials(
                account_ids, currency, start_date=start), errors) or [],
        "activities": activities,
        "fetch_errors": errors,
    }

    out_path = Path(args.out) if args.out else (
        out_dir / f"ws-snapshot-{now:%Y-%m-%d}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    os.chmod(out_path, 0o600)

    print(f"\nWrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB)", file=sys.stderr)
    print(f"  {len(accounts)} accounts, {len(positions)} positions, "
          f"{len(activities)} activities", file=sys.stderr)
    if errors:
        print(f"  {len(errors)} section(s) failed: "
              f"{', '.join(e['step'] for e in errors)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
