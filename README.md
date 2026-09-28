# Wealthfolio Connect Self-hosted

> A self-hosted companion server for the
> [Wealthfolio](https://github.com/wealthfolio/wealthfolio) **web edition**
> (the self-hosted Docker image that ships out of `apps/server`),
> for users who can't or won't use a hosted sync service.

> **Scope.** This server targets the **web edition** of Wealthfolio — the
> Docker image you self-host on your own box, configured via
> `CONNECT_AUTH_URL` / `CONNECT_AUTH_PUBLISHABLE_KEY` / `CONNECT_API_URL`.
> It is **not** intended for the Tauri desktop app, which talks to the same
> HTTP contract but seeds its session through the local OS keyring.

## 👉 Please use the official Wealthfolio Connect if you can

The [Wealthfolio](https://wealthfolio.app) web edition is built and
maintained by [@afadil](https://github.com/afadil) and contributors, who
have given the entire app away for free under AGPL-3.0. The
**Wealthfolio Connect** hosted service is how that work gets paid for.

If the official Connect fits your needs — **please subscribe to it**.
It's the right thing to do, it keeps the upstream project healthy, and
whatever you pay there is almost certainly less than the value
Wealthfolio gives you back. Open source survives on people choosing to
pay when they don't strictly have to.

This project exists for the (small) set of users for whom hosted Connect
is genuinely not an option:

- **Strict data-residency or compliance rules** that forbid sending
  broker credentials or holdings off-prem.
- **Regions / payment methods** the official Connect doesn't serve.
- **Hobbyist self-hosters** who run their own infra for everything as a
  matter of principle.

If none of those describe you, close this tab and go to
[wealthfolio.app](https://wealthfolio.app). 🙏

## What this is

A single Go binary that speaks the same HTTP contract the Wealthfolio
web edition already uses to talk to its sync backend, so your self-hosted
Wealthfolio web instance can point at a server you control instead of the
hosted Connect. Data is pulled directly from each broker by this binary —
no third-party data aggregator sits between you and the exchange:

- **Futu Securities** — TCP/protobuf to a local **Futu OpenD** daemon (`hurisheng/go-futu-api`)
- **Interactive Brokers** — socket protocol to a local **IB Gateway / TWS** (`scmhub/ibapi`)
- **Binance Spot** — REST API (`adshao/go-binance/v2`)
- **OKX CEX** — signed v5 REST API (HMAC-SHA256)
- **OKX Web3 / DEX** — signed v5 REST API for on-chain wallet aggregation
- **Bitget Spot** — signed v2 REST API
- **Hyperliquid** — public `/info` endpoint, wallet-address only (read-only)

All data is normalised into the Wealthfolio API shape and persisted in
PostgreSQL on your own infrastructure.

## Relationship with upstream Wealthfolio

This is an **independent, unaffiliated** project — not endorsed by,
sponsored by or supported by the Wealthfolio team. The HTTP contract this
server implements is part of the upstream open-source codebase, so this
is interoperation against a *published* protocol, not reverse
engineering. No upstream code is copied or linked into this binary.

"Wealthfolio" and "Wealthfolio Connect" are trademarks of their
respective owners and are used here solely to describe compatibility.

---

## Architecture

```
┌──────────────┐                ┌──────────────────────────────┐
│ Wealthfolio  │  HTTPS / JWT   │  wealthfolio-connect-open    │
│ Web edition  │ ──────────────▶│  (this repo, single Go bin)  │
│ (self-host)  │                │                              │
└──────────────┘                │                              │
                                │  ┌────────────────────────────┐  │
                                │  │ internal/interfaces/ (HTTP)│  │
                                │  ├────────────────────────────┤  │
                                │  │ internal/application/      │  │
                                │  ├────────────────────────────┤  │
                                │  │ internal/domain/ (entities)│  │
                                │  ├────────────────────────────┤  │
                                │  │ internal/infrastructure/   │  │
                                │  │  ├── persistence (PG)      │  │
                                │  │  ├── clients/              │  │
                                │  │  │   ├ futu, ibkr          │  │
                                │  │  │   ├ binance, okx        │  │
                                │  │  │   ├ bitget, hyperliquid │  │
                                │  │  │   └ cexcommon (shared)  │  │
                                │  │  └── auth (JWT)            │  │
                                │  └────────────────────────────┘  │
                                └────────┬─────────┬───────────┘
                                         │         │
                            ┌────────────▼──┐  ┌───▼────────────┐
                            │ PostgreSQL    │  │ Upstream       │
                            │ (single store)│  │ brokers/chains │
                            └───────────────┘  └────────────────┘
```

The architecture follows **Domain-Driven Design** (see [AGENTS.md](./AGENTS.md))
and uses **uber-go/fx** for dependency injection.

---

## Prerequisites

- **Go 1.22+**
- **PostgreSQL 14+**
- **Docker** (for containerised deployment)
- **Futu OpenD** running locally — only needed if you enable the Futu integration ([download](https://www.futunn.com/en/download/openAPI))
- **IB Gateway** or **TWS** running locally — only needed if you enable the IBKR integration
- (Optional) **mockgen** for regenerating gomock mocks: `go install go.uber.org/mock/mockgen@latest`
- (Optional) **golangci-lint** for linting: see [installation guide](https://golangci-lint.run/usage/install/).

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/your-org/wealthfolio-connect-open.git
cd wealthfolio-connect-open

# 2. Configure environment
export DATABASE_URL="postgres://user:pass@localhost:5432/wealthfolio?sslmode=disable"
export JWT_SECRET="change-me-to-a-long-random-string"
export CONNECT_AUTH_PUBLISHABLE_KEY="your-publishable-key"

# 3. Run (migrations execute automatically on startup)
go run ./cmd/server

# 4. Health check
curl http://localhost:8080/healthz
```

### Point the Wealthfolio web edition at this server

The Wealthfolio web container reads its Connect endpoints from environment
variables at runtime (the frontend pulls them through `get_connect_config`).
Set the following on the **Wealthfolio web** container — not on this one —
and restart it:

```bash
CONNECT_AUTH_URL=https://connect.your-domain.example         # base URL of THIS server
CONNECT_AUTH_PUBLISHABLE_KEY=your-publishable-key            # must match the value below
CONNECT_API_URL=https://connect.your-domain.example          # usually the same host
# Optional: only needed if you wire the OAuth-style callback flow.
# CONNECT_OAUTH_CALLBACK_URL=https://wealthfolio.your-domain.example/auth/callback
```

Then, in the Wealthfolio web UI, sign in to Connect with any email that is on
this server's `ALLOWED_EMAILS` allow-list and any 6-digit OTP (or `STATIC_OTP`
if configured). The session JWT and refresh token are persisted by the
Wealthfolio backend and reused across restarts — there is no separate "seed"
step, the OS-keyring dance only applies to the Tauri desktop build.

---

## Environment Variables

### Required

| Name                            | Description                                                                  |
| ------------------------------- | ---------------------------------------------------------------------------- |
| `DATABASE_URL`                  | PostgreSQL connection string (pgx format).                                   |
| `JWT_SECRET`                    | HS256 signing secret for access tokens.                                      |
| `CONNECT_AUTH_PUBLISHABLE_KEY`  | Expected `apikey` header on `/auth/v1/*`.                                    |
| `ALLOWED_EMAILS`                | Comma-separated email allow-list for the synthetic OTP login. See below.    |

### Optional — System

| Name                    | Default                   | Description                                                                                  |
| ----------------------- | ------------------------- | -------------------------------------------------------------------------------------------- |
| `SERVER_PORT`           | `8080`                    | HTTP listen port.                                                                            |
| `LOG_LEVEL`             | `info`                    | `debug` / `info` / `warn` / `error`.                                                         |
| `CORS_ORIGINS`          | `*`                       | Comma-separated list of allowed origins.                                                     |
| `SYNC_INTERVAL_MINUTES` | `240`                     | Periodic sync interval.                                                                      |
| `STATIC_TOKEN_MODE`     | `false`                   | If `true`, always returns the same JWT.                                                      |
| `TOKEN_TTL_SECONDS`     | `3600`                    | Access token lifetime.                                                                       |
| `STATIC_OTP`            | —                         | Optional fixed OTP code accepted by `/auth/v1/verify` in addition to any 6-digit numeric code. |

Every broker integration is **opt-in** — leave its credentials empty and the
corresponding client is silently skipped at startup. You only need to supply
the variables for the brokers you actually want to sync.

### Futu Securities (TCP to local OpenD)

Futu OpenD must already be running on the same host (or reachable network)
as this server. The server connects via TCP and signs trade requests with
your OpenD trading password.

| Name                 | Default      | Description                                                                |
| -------------------- | ------------ | -------------------------------------------------------------------------- |
| `FUTU_HOST`          | `127.0.0.1`  | OpenD host. Empty disables Futu.                                           |
| `FUTU_PORT`          | `11111`      | OpenD TCP port.                                                            |
| `FUTU_TRADE_PASSWORD`| —            | The trading password ("交易密码 / 交易密码 MD5") configured in OpenD.       |
| `FUTU_CONNECTION_ID` | `wealthfolio`| Logical connection identifier surfaced in the snapshot.                    |

### Interactive Brokers (socket to local IB Gateway / TWS)

Run **IB Gateway** (recommended) or **TWS** locally with API socket access
enabled. Allow this server's IP in the gateway's *Trusted IPs* list.

| Name              | Default     | Description                                                            |
| ----------------- | ----------- | ---------------------------------------------------------------------- |
| `IBKR_HOST`       | `127.0.0.1` | IB Gateway / TWS host. Empty disables IBKR.                            |
| `IBKR_PORT`       | `4001`      | `4001` for live IB Gateway, `4002` paper, `7496` TWS, `7497` TWS paper.|
| `IBKR_CLIENT_ID`  | `1`         | Any unique integer — must not clash with other API clients.            |
| `IBKR_ACCOUNT_ID` | —           | Optional account filter (e.g. `U1234567`); empty pulls every account.  |

> **Operational caveats.** IB Gateway is the weakest link in any IBKR
> integration: sessions die after ~24h of uptime, the daily reset window
> forces re-login, and Two-Factor Authentication can ask for a phone tap at
> any reconnect. The current client treats every `Fetch()` as a fresh
> attempt and surfaces the underlying error — it does not auto-restart the
> gateway. Recommended deployment hardening:
>
> - Run IB Gateway under a process supervisor (`systemd`, `supervisord`,
>   or `ibc` from [IbcAlpha/IBC](https://github.com/IbcAlpha/IBC)) that
>   restarts the daemon nightly before the daily reset.
> - Use IBC + a dedicated *paper* + *live* user pair when possible; paper
>   sessions do not enforce 2FA.
> - Monitor `/healthz` and the sync logs for repeated `IBKR_*` errors;
>   alert when consecutive failures exceed `SYNC_INTERVAL_MINUTES`.
> - If 2FA prompts become disruptive, evaluate IBKR's *Read-only login*
>   option, which suppresses the second factor for market-data + portfolio
>   queries.

### Binance Spot (REST)

Create a **read-only** API key (Spot account permissions are sufficient).
The client syncs Spot fills and completed Buy Crypto fiat payments, preserving the
fiat transaction currency (for example, PHP) while matching crypto assets to the
USD-quoted Binance holding identity; the read-only key is sufficient. For a
non-USD fiat purchase, the unit price is omitted so the official Wealthfolio
backend cannot mistake a PHP price for a USD quote. The fiat amount, currency,
quantity, fee and Binance source identity are preserved. A fee-bearing activity
with a positive amount and no USD unit price is marked for review; the fee is
retained. No FX rate or USD cost basis is invented. A basis is derived only
when complete USD trade history explains the current quantity.

| Name                 | Description       |
| -------------------- | ----------------- |
| `BINANCE_API_KEY`    | Binance API key.  |
| `BINANCE_API_SECRET` | Binance secret.   |

### OKX CEX (signed v5 REST)

Create a **read-only** API key on OKX (no trading / withdrawal permissions
required).

| Name             | Description           |
| ---------------- | --------------------- |
| `OKX_API_KEY`    | OKX API key.          |
| `OKX_API_SECRET` | OKX API secret.       |
| `OKX_PASSPHRASE` | OKX API passphrase.   |

### OKX Web3 / DEX (signed v5 REST + wallet list)

The Web3 client uses a separate set of OKX credentials with **DEX API**
permissions enabled, and aggregates balances across the wallets you list.

| Name                  | Description                                                                                              |
| --------------------- | -------------------------------------------------------------------------------------------------------- |
| `OKX_WEB3_API_KEY`    | OKX Web3 API key (DEX-enabled).                                                                          |
| `OKX_WEB3_API_SECRET` | OKX Web3 API secret.                                                                                     |
| `OKX_WEB3_PASSPHRASE` | OKX Web3 passphrase.                                                                                     |
| `DEFI_WALLETS`        | JSON array of wallets. Example: `[{"address":"0xabc...","chains":["1","56","42161"],"label":"main"}]`. |

`chains` are OKX chain indexes — see [OKX docs](https://www.okx.com/web3/build/docs/waas/dex-supported-chains)
(e.g. `1` = Ethereum, `56` = BSC, `42161` = Arbitrum, `137` = Polygon, `10` = Optimism, `8453` = Base).

### Bitget Spot (signed v2 REST)

| Name                | Description              |
| ------------------- | ------------------------ |
| `BITGET_API_KEY`    | Bitget API key.          |
| `BITGET_API_SECRET` | Bitget API secret.       |
| `BITGET_PASSPHRASE` | Bitget API passphrase.   |

### Hyperliquid (public `/info`, wallet-only)

No API key — Hyperliquid's `/info` endpoint is public. Just point it at the
wallet whose perpetuals + spot balances you want tracked.

| Name                 | Description                                  |
| -------------------- | -------------------------------------------- |
| `HYPERLIQUID_WALLET` | EVM-style wallet address (`0x…`). Read-only. |

---

## API Endpoints

| Method | Path                                                          | Description                                              |
| ------ | ------------------------------------------------------------- | -------------------------------------------------------- |
| POST   | `/auth/v1/otp`                                                | Request a magic-link OTP. No-op (no email sent).         |
| POST   | `/auth/v1/verify`                                             | Exchange `{email, token}` for an access + refresh token. |
| POST   | `/auth/v1/token?grant_type=refresh_token`                     | Exchange refresh token for JWT.                          |
| POST   | `/auth/v1/logout`                                             | Best-effort session invalidation.                        |
| GET    | `/auth/v1/user`                                               | Supabase-shaped current user.                            |
| GET    | `/api/v1/user/me`                                             | Current user + subscription info.                        |
| GET    | `/api/v1/subscription/plans`                                  | Available subscription plans. **No auth required.**      |
| GET    | `/api/v1/sync/brokerage/connections`                          | All broker connections.                                  |
| GET    | `/api/v1/sync/brokerage/accounts`                             | All broker accounts.                                     |
| PATCH  | `/api/v1/sync/brokerage/accounts/{id}`                        | Toggle `sync_enabled` for one account.                   |
| GET    | `/api/v1/sync/brokerage/accounts/{id}/activities`             | Paginated activities.                                    |
| GET    | `/api/v1/sync/brokerage/accounts/{id}/holdings`               | Latest holdings snapshot.                                |
| POST   | `/api/v1/connect/session`                                     | Seed/refresh the local sync session (see Quick Start).   |
| GET    | `/healthz`                                                    | Liveness + DB ping.                                      |
| GET    | `/readyz`                                                     | Readiness (post-migration).                              |

See [API.md](./API.md) for full request/response schemas.

---

## Authentication

This server impersonates the subset of Supabase Auth that Wealthfolio's
`wealthfolio-connect` feature talks to. There is **no real email provider,
no real OTP storage, and no per-user database**: gating is done by the
`apikey` header (`CONNECT_AUTH_PUBLISHABLE_KEY`) plus the `ALLOWED_EMAILS`
allow-list. The server is expected to be reachable only over a trusted
network (VPN, reverse proxy, IP allow-list, ...).

### Login flow

```
┌─ Wealthfolio web frontend ────────────────┐
│ 1. POST /auth/v1/otp   { email }          │ ── apikey-gated
│    server: email ∈ ALLOWED_EMAILS?        │     → 403 if not
│    server: 200, no email ever sent        │
│                                           │
│ 2. user types any 6-digit code (or the    │
│    configured STATIC_OTP)                 │
│                                           │
│ 3. POST /auth/v1/verify { email, token }  │ ── apikey-gated
│    server: validates code shape           │     → 400 otp_invalid
│    server: validates email allow-list     │     → 403 if not
│    server: signs JWT (sub = sha256(email) │
│            in UUID format) + refresh tk   │
│    → returns Supabase-shaped session JSON │
│                                           │
│ 4. session is persisted by the Wealthfolio│
│    web backend (encrypted via             │
│    WF_SECRET_KEY) and reused across       │
│    restarts.                              │
└───────────────────────────────────────────┘
```

### OTP policy

`/auth/v1/verify` accepts a token if **either** of the following holds:

- the token matches `^[0-9]{6}$` (any 6-digit numeric code), **or**
- `STATIC_OTP` is set and the token equals that value (constant-time
  comparison).

There is intentionally no per-email OTP storage, no expiry, no rate limit
on this endpoint — the apikey + allow-list are the gates that matter. If
you need stronger guarantees, put the server behind a reverse proxy that
rate-limits `/auth/v1/*`.

### Subject derivation

The JWT `sub` claim is `sha256(lowercased email)` reformatted as a UUID
v4 string. This keeps the raw email out of access logs / downstream
services while remaining stable across token refreshes for the same
address, and parses cleanly as a UUID — which `supabase-js` requires.

---

## Development

```bash
# Run tests with race detector and coverage
go test ./... -race -coverprofile=coverage.out
go tool cover -func=coverage.out | tail -1

# Lint
golangci-lint run

# Vet
go vet ./...

# Regenerate mocks
go generate ./...
```

Coverage threshold is **≥ 90%** — CI will fail below that.

---

## Maintenance: stale fiat-quoted crypto assets

Before the symbol fix that quotes the crypto leg of a fiat-funded purchase in
USD (`BTC/USD`) while preserving the fiat transaction amount (PHP), the
connector published the crypto leg quoted in the transaction fiat (`BTC/PHP`).
Wealthfolio therefore created a separate `CRYPTO:BTC/PHP` asset and cached
quotes, `quote_sync_state` and auto-generated taxonomy rows against it. On an
existing installation those rows can survive the switch to `BTC/USD` and keep
the stale asset visible.

`scripts/cleanup_php_crypto_assets.py` reports and removes safe stale rows from a
Wealthfolio SQLite database. It uses only the Python standard library, defaults
to a **dry run**, and refuses to touch anything it cannot prove is safe. It is
for historical PHP-quoted crypto assets and cross-currency broker quotes that
may already exist; current Connect mapping omits a non-USD unit price, preventing
new fiat prices from seeding quotes on USD crypto assets.

### Invocation

```bash
# 1. Stop the Wealthfolio server, then find its SQLite file, e.g.
#    <data-dir>/wealthfolio.db (see the app's data directory).

# 2. Inspect first (default is a dry run — no writes, opened read-only):
python scripts/cleanup_php_crypto_assets.py /path/to/wealthfolio.db

# 3. Apply (writes, backs up first, single transaction):
python scripts/cleanup_php_crypto_assets.py /path/to/wealthfolio.db --apply

# Machine-readable report:
python scripts/cleanup_php_crypto_assets.py /path/to/wealthfolio.db --json
```

Options: `--from-ccy` (stale quote currency, default `PHP`), `--to-ccy`
(replacement quote currency, default `USD`), `--apply`, `--json`.

The same currency options scope both sections. In particular, the quote repair
defaults to `PHP` → `USD`; it does not scan other fiat currencies by default.

`--apply` first copies the database and its `-wal` / `-shm` sidecars to
`<db>.bak-YYYYmmdd-HHMMSS`, then opens it for writing. If there is nothing to
delete, no backup is taken. Keep the backup file until you have verified the
result. Always stop the server first: SQLite writes from a running app and this
script must not interleave.

### What it deletes

An asset is deleted **only** when every one of the following holds. Anything
else is reported as `SKIP` with the reason; nothing is guessed.

Counterpart:

- it is a `CRYPTO` asset quoted in `--from-ccy` with `quote_mode = 'MARKET'`;
- exactly one **active** `CRYPTO` asset quoted in `--to-ccy` exists with the
  same `instrument_symbol` and `kind` (the counterpart), and the two `name`
  fields match (both empty, or identical).

No references (any one blocks):

- no `activities`, `lots`, `lot_disposals`, `snapshot_positions`,
  `holdings_snapshots.positions` JSON, `goal_plans` JSON, or
  `allocation_target_constraints` (`subject_type = 'asset'`) row;
- no unrecognised `asset_id` table references it (unknown tables fail closed);
- no `asset_logos` row (a custom, user-supplied logo override — never deleted);
- no `quotes` row with `source = 'MANUAL'` or a non-empty `notes` value (a
  manual price or a user annotation);
- no `asset_taxonomy_assignments` row whose `source` is outside the generated
  set (`AUTO`, `migrated`) — e.g. `manual` or `ai` counts as user work.

No user-owned asset fields:

- `assets.notes` empty, `is_active = 1`;
- `provider_config` is `NULL`, or a plain **built-in** provider configuration:
  keys limited to `preferred_provider` / `overrides`, `preferred_provider` in
  the built-in provider set (`YAHOO`, `ALPHA_VANTAGE`, `MARKETDATA_APP`,
  `METAL_PRICE_API`, `FINNHUB`, `BOERSE_FRANKFURT`, `US_TREASURY_CALC`,
  `OPENFIGI`), overrides keyed only by those providers with only
  `type`/`symbol`/`from`/`to` and a known override `type`. Anything else
  (`CUSTOM_SCRAPER`, `custom_provider_id`, a custom provider code, unknown
  keys) is a user-configured source and blocks.
- `metadata` is `NULL` or contains only auto-written keys (`legacy`,
  `identifiers`, provider-profile enrichment such as `sectors`, `countries`,
  `marketCap`, …, and instrument specs such as `option`/`bond`). Any other key
  is user data and blocks.

No device-sync involvement (any one blocks every candidate):

- `app_settings.sync_enabled` is explicitly truthy, or a `trusted` row exists in
  `sync_device_config`, or `sync_engine_state` has run (`last_push_at` /
  `last_pull_at`), or `sync_outbox` is non-empty;
- additionally, `sync_entity_metadata` / `sync_outbox` / `sync_applied_events`
  must have no rows for the asset, its logo, its quotes, or its generated
  assignments. A raw `DELETE FROM assets` does not emit the outbox delete
  event, so deleting a synced asset would diverge across devices — use the
  Wealthfolio UI for those.

It then deletes the asset plus its provider `quotes`, `quote_sync_state` and
generated `asset_taxonomy_assignments` rows. `asset_logos` and manual/noted
quotes are never touched. Re-running is safe: once cleaned, the script reports
no eligible deletions and takes no backup.

### Existing PHP broker quotes on USD crypto assets

Wealthfolio Connect previously wrote broker quotes in the activity's
transaction currency. A Binance fiat `BUY` in PHP could therefore leave a
`BROKER` quote with `currency = 'PHP'` attached to the USD-quoted crypto asset.
Those rows can be selected as previous-day prices even though the asset is
quoted in USD.

The second report section considers only rows satisfying **all** of these:

- quote source is `BROKER`, quote currency matches `--from-ccy` (default PHP),
  and it differs from the asset's `--to-ccy` quote currency (default USD);
- asset is active, `CRYPTO`, `MARKET`, and quoted in `--to-ccy`;
- the same asset and quote day have an effective `BUY` activity
  (`activity_type_override` when set, otherwise `activity_type`) whose
  `source_system` is `BINANCE` and transaction `currency` is `--from-ccy`;
- quote has no non-empty user note, device-sync is inactive, and no sync
  metadata/outbox/applied-event row references the quote or matching activity.

Eligible rows are reported as `DELETE_QUOTE`. A cross-currency `BROKER` quote
without the matching Binance activity, with a note, or with a sync reference is
reported as `SKIP`. Manual quotes (`source = 'MANUAL'`), real USD quotes,
quotes on other assets, and activities are never deleted or updated. Applying
this section deletes **only the quote row**; it preserves the USD asset and its
snapshot positions.

### Transaction and failure guarantees

- All writes run in a single `BEGIN IMMEDIATE` transaction with
  `PRAGMA foreign_keys = ON`; `PRAGMA foreign_key_check` is verified before
  `COMMIT`, and any error rolls the whole transaction back.
- Planning happens **inside** that transaction and each deletion's eligibility
  is re-checked under the lock (references, user-owned fields, device-sync,
  asset counterpart, or the exact Binance activity/day match). A row that
  became unsafe between preview and deletion is refused rather than half-deleted.
- The database is copied **before** it is opened for writing, so even a crash
  during the transaction leaves a consistent pre-run backup.
- The script fails closed on an incompatible schema: it aborts if a required
  table/column is missing (including `assets.notes` / `provider_config` /
  `metadata`), if the file is not a plain SQLite database, or if an `asset_id`
  table it does not recognise references the asset.

### Safety preconditions and limitations

- Requires a plain (unencrypted) SQLite database; refuses `.wfbackup`/encrypted
  files.
- `sync_enabled` defaults to "on" in the Wealthfolio app when the setting row
  is absent; the script only treats an **explicit** truthy row as a blocker, and
  otherwise relies on the concrete sync signals (trusted device, engine history,
  outbox rows, per-row metadata). If you use Connect/device sync, prefer the
  app UI.
- The `name` check catches a renamed asset, but `name`/`display_code` alone are
  not treated as user data because auto-resolution populates them identically
  for both the PHP and USD asset. An asset whose auto-created fields happen to
  look user-authored (e.g. a provider-enriched `notes`/`metadata`) is therefore
  reported for manual review rather than deleted.
- The auto allowlists (`AUTO_METADATA_KEYS`, `BUILTIN_PROVIDERS`,
  `AUTO_OVERRIDE_TYPES`) track the current Wealthfolio schema. A newer schema
  that writes a new auto key will block deletion (fail closed) — extend the
  allowlist deliberately after verifying it is auto-generated.
- Assets still referenced by activities cannot be cleaned by this script; it
  only reports them. Those need manual review (the activity/quote history may
  need to be reconciled against the USD asset first).
- Exit code is non-zero on usage/schema/database errors; a run that finds only
  skipped assets still exits `0` with a report.

Run the tests with:

```bash
python -m unittest discover -s scripts -p "test_*.py"
```

---

## Docker

```bash
# Build the multi-stage image
docker build -t wealthfolio-connect-open:latest .

# Run
docker run --rm -p 8080:8080 \
  -e DATABASE_URL="postgres://..." \
  -e JWT_SECRET="..." \
  -e CONNECT_AUTH_PUBLISHABLE_KEY="..." \
  wealthfolio-connect-open:latest
```

The container exposes `SERVER_PORT` (default `8080`) and provides
`/healthz` + `/readyz` for Kubernetes probes.

---

## CI/CD

GitHub Actions workflow at [`.github/workflows/ci.yml`](./.github/workflows/ci.yml):

- Runs on every PR and push to `main`.
- Steps: `go vet` → `golangci-lint` → `go test -race -coverprofile` → coverage gate (≥ 90%).
- A PostgreSQL service container is provisioned for integration tests.
- On `main`, the multi-stage Docker image is built and pushed using the
  `REGISTRY_URL` / `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` repo secrets.

---

## Project Structure

```
wealthfolio-connect-open/
├── cmd/
│   └── server/                 # main.go: fx.New() composition root
├── internal/                   # All non-main code (Go internal visibility)
│   ├── domain/                 # Pure business model
│   │   ├── auth/
│   │   ├── brokerage/
│   │   ├── sync/
│   │   └── repository/         # Repository interfaces
│   ├── application/            # Use cases / orchestration
│   │   ├── auth/
│   │   ├── brokerage/
│   │   └── sync/
│   ├── infrastructure/         # Adapters
│   │   ├── config/
│   │   ├── database/
│   │   ├── persistence/        # PG repositories
│   │   ├── auth/               # JWT signing
│   │   ├── logging/
│   │   └── clients/
│   │       ├── futu/           # TCP → local OpenD
│   │       ├── ibkr/           # socket → local IB Gateway / TWS
│   │       ├── binance/        # Spot REST
│   │       ├── okx/            # CEX + Web3/DEX (signed v5)
│   │       ├── bitget/         # Spot REST (signed v2)
│   │       ├── hyperliquid/    # public /info
│   │       └── cexcommon/      # shared snapshot translation
│   └── interfaces/
│       └── http/
│           ├── handlers/
│           └── middleware/
├── deploy/                     # Kubernetes manifests
├── .github/workflows/          # CI/CD
├── AGENTS.md                   # Conventions for AI agents and contributors
├── API.md                      # Full HTTP API reference
├── Dockerfile
└── README.md
```

---

## License

**AGPL-3.0-or-later** — same licence the upstream Wealthfolio project uses.
See [LICENSE](./LICENSE).
