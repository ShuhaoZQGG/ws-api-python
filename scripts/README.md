# Local portfolio pipeline

Fork-local helpers. Not part of the upstream `ws_api` package — keeping them
here means `git pull upstream main` won't conflict.

```
ws_profiles.py   shared helper          ->  data/profiles.json (label -> login email)
ws_login.py      one-time, interactive  ->  session in macOS Keychain
ws_export.py     non-interactive        ->  data/ws-snapshot-<date>.json   (~1.8 MB, raw)
ws_summarize.py  offline                ->  data/ws-summary-<date>.json    (~44 KB, agent-readable)
                                            data/ws-positions-<date>.csv
                                            data/ws-trades-<date>.csv
                                            data/ws-card-<date>.csv
```

## Usage

```bash
uv run python scripts/ws_login.py                    # once (needs a TTY: password + 2FA)
uv run python scripts/ws_export.py                   # refresh data
uv run python scripts/ws_summarize.py                # rebuild summary (no API calls)
```

Useful flags: `ws_export.py --activity-months 24`, `--username you@example.com`,
`--currency USD`, `--out path.json`.

## Credit-card detail (original currency, FX rate, fees)

The activity feed only carries the CAD-converted `amount` for card purchases;
`counterPartyCurrencyAmount` / `fxRate` are always null there. The web app
gets the original amount from a separate per-transaction query,
`creditCardActivity(id)`, keyed by the activity's `externalCanonicalId`
(`card-activity-...`). `ws_export.py` calls it once per `CREDIT_CARD` line
and stores the result as `cardDetail` on the activity: `originalAmount`,
`originalCurrency`, `foreignExchangeRate`, `isForeign`, `fees[]`,
`authorizedAt` / `settledAt`. Pass `--no-card-details` to skip the extra
calls. `ws_summarize.py` flattens these into `ws-card-<date>.csv` and a
`card_spend` block (foreign purchases by currency).

Not covered: `SPEND` lines from the Cash account's prepaid card have no
detail endpoint that I could find, so they stay CAD-only. The query was
found by probing (introspection is disabled), so new fields may exist.

## Several people (profiles)

Each Wealthsimple login is one *profile*: a short label mapped to an email in
`data/profiles.json`. Sessions stay in the Keychain keyed by email; the label
only picks the session and the output folder, `data/<profile>/`.

```bash
uv run python scripts/ws_login.py --profile me        # existing session: just registers the label
uv run python scripts/ws_login.py --profile wife      # she types her password + 2FA
uv run python scripts/ws_export.py --all              # data/me/..., data/wife/...
uv run python scripts/ws_summarize.py --all
uv run python scripts/ws_export.py --profile wife     # one person only
```

Without `--profile` the scripts behave as before and use the flat `data/`
layout. When exactly one profile is registered it is used automatically;
with several, `--profile` or `--all` is required. The symbol cache in
`data/.security-cache/` is shared, since it is keyed by security id.

## Design notes

- The session is requested with `SCOPE_READ_ONLY` (`invest.read trade.read
  tax.read`). The stored token cannot place trades.
- Only `ws_login.py` and `ws_export.py` touch credentials. Analysis runs
  against files, so an agent never needs Keychain access.
- API exceptions are never printed raw: `WSApiException.__str__` splices in the
  full response body, which here means holdings and account numbers.
- `data/` is gitignored and written mode `0600`.
- `data/.security-cache/` caches security-id → symbol lookups so repeat
  exports don't re-hit the API.

## Classification

`ws_summarize.py` tags every position with an `asset_class`:

- `cash_equivalent` — HISA / money-market / T-bill ETFs (`CASH`, `UCSH.U`,
  `PSA`, `CBIL`, `ZMMK`, …). Cash by economic substance, so they feed
  `totals.cash_and_equivalents` and are excluded from concentration ranking.
  **Add new tickers to `CASH_EQUIVALENT_TICKERS` in `ws_summarize.py`** — that
  constant is the single source of truth; the skill defers to it.
- `option` — `sec-o-*` securities. Negative quantity means written/short;
  one contract is 100 shares.
- `security` — everything else. `pct_of_securities` is computed against these
  only, so cash equivalents and short options can't distort the denominator.

Securities also get a curated `sector` / `industry` / `themes`, rolled up into
`sector_allocation`, `industry_allocation`, and `theme_allocation` (weights
over securities only). Wealthsimple has **no sector field** — its
`securityGroups` mix feature flags with overlapping marketing themes and leave
many securities untagged — so `SECTOR_MAP` and `THEME_MAP` in
`ws_summarize.py` are the source of truth. Anything unmapped lands in
`unclassified_symbols` rather than being guessed at. Broad ETFs sit in
`Diversified Funds` with no look-through; single-sector ETFs get their sector.

`totals` decomposes net worth into `securities_market_value` +
`cash_equivalents_market_value` + `cash_on_hand` + `net_option_market_value` +
`managed_not_itemised`, which sum back to `net_liquidation_value` (within
rounding). Each account also carries its own `positions_market_value` /
`unitemised_value` / `unitemised_kind` reconciliation.

## Consuming from an agent

The `wealthsimple-analysis` skill (`~/.claude/skills/wealthsimple-analysis/`)
reads `ws-summary-*.json` by default and only opens the trades CSV when the
question needs it. Traps it encodes, all found the hard way against real data:

- Positions are **per-account**, so one symbol can appear as several rows —
  aggregate by symbol before ranking.
- HISA ETFs are cash, not equity. Reporting them as a top holding is an
  artifact.
- Managed/robo accounts return **zero positions**, so `NLV − positions` is not
  cash — it's mostly managed holdings. Hence the per-account reconciliation.
- The identity-level total includes the corporate chequing account and the
  credit card; subtract them for personal net worth.
- USD-denominated accounts report a CAD-converted `netLiquidationValue`
  (upstream documents this); trust only their balances and positions.
- Institutional transfers post as **intents at full value when initiated** and
  sit on the feed while in flight. `pending_transfers` are not in any balance.
- `DIY_BUY` carries `amountSign: "positive"`, so naive flow math counts every
  purchase as a deposit. Use `monthly_external_flow`, never raw activities.
- Everything else is already CAD — no FX conversion anywhere.
