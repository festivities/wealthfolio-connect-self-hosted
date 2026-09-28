#!/usr/bin/env python3
"""Remove stale fiat-quoted CRYPTO assets left in a Wealthfolio SQLite database.

Background
----------
The CEX connector used to publish a fiat-funded crypto purchase (for example a
Binance "Buy Crypto" payment settled in PHP) quoted in the transaction fiat, so
Wealthfolio created a CRYPTO asset whose quote currency was PHP
(``CRYPTO:BTC/PHP``). It now publishes the crypto leg quoted in USD
(``CRYPTO:BTC/USD``). Existing installations therefore keep the old PHP asset
around together with its quotes, ``quote_sync_state`` row and *auto-generated*
taxonomy assignments, even after every activity has been re-upserted against
the USD asset.

This is a manual maintenance utility, not part of the running server. It never
guesses: it removes only unreferenced connector artifacts and narrowly matched
old PHP broker quotes backed by a Binance fiat BUY activity. Anything that
could be user intent is a **blocker** (reported as ``SKIP``, never deleted).

What blocks a deletion
-----------------------
* any reference from ``activities``, ``lots``, ``lot_disposals``,
  ``snapshot_positions``, ``holdings_snapshots.positions`` / ``goal_plans`` JSON,
  ``allocation_target_constraints`` (``subject_type='asset'``), or an
  unrecognised ``asset_id`` table;
* a custom logo override (``asset_logos``) — always user data;
* a ``MANUAL`` quote or a quote carrying a user note;
* a taxonomy assignment whose ``source`` is not the generated set
  (``AUTO`` / ``migrated``);
* **device sync**: if sync is enabled or has any state (see
  ``device_sync_reason``) nothing is deleted at all, and each targeted row is
  additionally checked against the sync metadata/outbox;
* **user-owned asset fields**: a non-empty ``assets.notes``, an inactive
  (``is_active = 0``) asset, a ``provider_config`` that is not a plain built-in
  provider configuration, ``metadata`` with keys outside the auto allowlist, or
  a ``name`` that differs from its USD counterpart's.

Transaction and failure guarantees
----------------------------------
* Writes run in a single ``BEGIN IMMEDIATE`` transaction with
  ``PRAGMA foreign_keys = ON``; ``PRAGMA foreign_key_check`` is verified before
  ``COMMIT`` and any error rolls the whole transaction back.
* Planning happens inside that transaction and the reference scan is re-run
  under the lock, so an asset that gained a reference (or had sync enabled)
  between preview and deletion is refused instead of half-deleted.
* ``--apply`` copies the database (and ``-wal`` / ``-shm``) **before** it is
  opened for writing. Nothing to delete ⇒ no backup. Re-running is a no-op.
* The script fails closed on an incompatible schema.

Cross-currency quote cleanup
----------------------------
Only ``BROKER`` quotes in PHP on active, market-priced, USD-quoted CRYPTO assets
are considered. A quote is deleted only if its exact asset and day have a
``BUY`` activity with ``source_system = 'BINANCE'`` and ``currency = 'PHP'``;
quotes with notes and device-sync references are preserved.

Usage
-----
    python scripts/cleanup_php_crypto_assets.py /path/to/wealthfolio.db
    python scripts/cleanup_php_crypto_assets.py /path/to/wealthfolio.db --apply
    python scripts/cleanup_php_crypto_assets.py /path/to/wealthfolio.db --json

Stop the Wealthfolio server before ``--apply``. Encrypted / ``.wfbackup``
databases are not supported.

Exit codes: 0 = completed (possibly with skipped assets), 1 = usage/schema/
database error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from urllib.parse import quote

SQLITE_MAGIC = b"SQLite format 3\x00"

# Required to exist with these columns; anything else aborts (fail closed).
REQUIRED_COLUMNS = {
    "assets": {
        "id",
        "kind",
        "name",
        "notes",
        "metadata",
        "is_active",
        "provider_config",
        "instrument_type",
        "instrument_symbol",
        "quote_mode",
        "quote_ccy",
    },
    "activities": {
        "id", "asset_id", "activity_type", "activity_type_override",
        "activity_date", "currency", "source_system",
    },
    "quotes": {"id", "asset_id", "day", "source", "currency", "notes"},
    "quote_sync_state": {"asset_id"},
    "asset_taxonomy_assignments": {"asset_id", "source"},
}

# Rows here block deletion (data / derived state tied to activities).
BLOCKING_TABLES = ("activities", "lots", "lot_disposals", "snapshot_positions")
# User-provided logo override: never deleted, always blocks.
PROTECTED_TABLES = ("asset_logos",)
# Device-sync bookkeeping tables: checked per targeted row and detected globally.
SYNC_ROW_TABLES = ("sync_entity_metadata", "sync_outbox", "sync_applied_events")
SYNC_ENTITY_IDS = {
    "asset": "id",
    "asset_logo": "id",
    "quote": "id",
    "asset_taxonomy_assignment": "id",
}
# Pure caches with no FK: removed together with an orphaned asset.
DELETABLE_TABLES = ("quote_sync_state",)
JSON_REFERENCE_COLUMNS = (
    ("holdings_snapshots", "positions"),
    ("goal_plans", "settings_json"),
    ("goal_plans", "summary_json"),
)
ALLOCATION_SUBJECT_TABLE = ("allocation_target_constraints", "subject_id", "subject_type")
# The only assignment sources considered generated and safe to remove.
GENERATED_TAXONOMY_SOURCES = ("auto", "migrated")

# Built-in market-data providers (market_data_providers seeds). Anything else in
# provider_config (CUSTOM_SCRAPER, custom_provider_id, a custom provider code)
# means a user-configured source and blocks deletion.
BUILTIN_PROVIDERS = {
    "YAHOO",
    "ALPHA_VANTAGE",
    "MARKETDATA_APP",
    "METAL_PRICE_API",
    "FINNHUB",
    "BOERSE_FRANKFURT",
    "US_TREASURY_CALC",
    "OPENFIGI",
}
AUTO_OVERRIDE_TYPES = {"crypto_symbol", "equity_symbol", "fx_symbol", "fx_pair"}
# Keys written by migrations, provider enrichment or instrument specs. Any other
# key is treated as user data and blocks deletion.
AUTO_METADATA_KEYS = {
    "legacy",
    "identifiers",
    "sectors",
    "industry",
    "countries",
    "quoteType",
    "website",
    "marketCap",
    "peRatio",
    "week52High",
    "week52Low",
    "option",
    "bond",
    "contractMultiplier",
}
_TRUTHY = {"true", "1", "yes", "on"}
STALE_BROKER_SOURCE = "BROKER"

_TAXONOMY_IN = "(" + ",".join("'%s'" % s for s in GENERATED_TAXONOMY_SOURCES) + ")"
TAXONOMY_GENERATED_PREDICATE = "LOWER(COALESCE(source,'')) IN %s" % _TAXONOMY_IN
QUOTE_GENERATED_PREDICATE = (
    "UPPER(COALESCE(source,'')) <> 'MANUAL' AND COALESCE(TRIM(notes),'') = ''"
)

_KNOWN_ASSET_TABLES = (
    set(BLOCKING_TABLES)
    | set(PROTECTED_TABLES)
    | set(DELETABLE_TABLES)
    | set(SYNC_ROW_TABLES)
    | {"quotes", "asset_taxonomy_assignments"}
    | {t for t, _ in JSON_REFERENCE_COLUMNS}
    | {ALLOCATION_SUBJECT_TABLE[0]}
)


class SchemaError(RuntimeError):
    """Raised when the database is not a supported, safe-to-touch Wealthfolio DB."""


def _uri(path: str, mode: str) -> str:
    posix = os.path.abspath(path).replace("\\", "/")
    if not posix.startswith("/"):
        posix = "/" + posix
    return "file://%s?mode=%s" % (quote(posix), mode)


def connect(path: str, readonly: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _uri(path, "ro" if readonly else "rw"), uri=True, isolation_level=None, timeout=30
    )
    conn.row_factory = sqlite3.Row
    return conn


def check_header(path: str) -> None:
    if not os.path.isfile(path):
        raise SchemaError("database file not found: %s" % path)
    with open(path, "rb") as handle:
        magic = handle.read(len(SQLITE_MAGIC))
    if magic != SQLITE_MAGIC:
        raise SchemaError("not a plain SQLite database (encrypted or corrupt?): %s" % path)


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return [r[0] for r in rows if not r[0].startswith("sqlite_")]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {r[1] for r in conn.execute('PRAGMA table_info("%s")' % escaped)}


def verify_schema(conn: sqlite3.Connection) -> None:
    present = set(_tables(conn))
    for table, required in REQUIRED_COLUMNS.items():
        if table not in present:
            raise SchemaError("required table missing: %s" % table)
        missing = required - _columns(conn, table)
        if missing:
            raise SchemaError(
                "table %s is missing expected columns: %s" % (table, sorted(missing))
            )


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _json_object(raw: str | None):
    """Parse a JSON column; returns (value_or_None, is_valid)."""
    if raw is None:
        return None, True
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None, False
    return (value if isinstance(value, dict) else None), isinstance(value, dict)


def provider_config_reason(raw: str) -> str | None:
    """Return a short reason if provider_config is not a plain built-in config."""
    config, valid = _json_object(raw)
    if not valid:
        return "not a JSON object"
    extra = sorted(set(config) - {"preferred_provider", "overrides"})
    if extra:
        return "unexpected keys %s" % ",".join(extra)
    provider = config.get("preferred_provider")
    if provider is not None and str(provider).upper() not in BUILTIN_PROVIDERS:
        return "provider %s" % provider
    overrides = config.get("overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            return "overrides not an object"
        for key, value in overrides.items():
            if str(key).upper() not in BUILTIN_PROVIDERS:
                return "override provider %s" % key
            if not isinstance(value, dict):
                return "override for %s not an object" % key
            unknown = sorted(set(value) - {"type", "symbol", "from", "to"})
            if unknown:
                return "override %s keys %s" % (key, ",".join(unknown))
            override_type = value.get("type")
            if override_type is not None and override_type not in AUTO_OVERRIDE_TYPES:
                return "override type %s" % override_type
    return None


def device_sync_reason(
    conn: sqlite3.Connection, present: set[str], cols: dict[str, set[str]]
) -> str | None:
    """Describe any configured/used device sync, or None when clearly unused."""

    def has(table: str, *columns: str) -> bool:
        return table in present and set(columns) <= cols.get(table, set())

    reasons = []
    if has("app_settings", "setting_key", "setting_value"):
        row = conn.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = 'sync_enabled'"
        ).fetchone()
        if row and str(row[0]).strip().lower() in _TRUTHY:
            reasons.append("sync_enabled=true")
    if has("sync_device_config", "trust_state"):
        trusted = _count(
            conn, "SELECT COUNT(*) FROM sync_device_config WHERE trust_state = 'trusted'"
        )
        if trusted:
            reasons.append("trusted devices=%d" % trusted)
    if has("sync_engine_state", "last_push_at", "last_pull_at"):
        row = conn.execute(
            "SELECT last_push_at, last_pull_at FROM sync_engine_state LIMIT 1"
        ).fetchone()
        if row and (row[0] or row[1]):
            reasons.append("sync engine has run")
    if "sync_outbox" in present:
        outbox = _count(conn, "SELECT COUNT(*) FROM sync_outbox")
        if outbox:
            reasons.append("outbox rows=%d" % outbox)
    return ", ".join(reasons) or None


def asset_field_blockers(row: sqlite3.Row) -> dict[str, int]:
    """Blockers for user-owned columns on an ``assets`` row."""
    blockers: dict[str, int] = {}
    if row["notes"] is not None and str(row["notes"]).strip():
        blockers["assets.notes (user note)"] = 1
    if row["is_active"] != 1:
        blockers["assets.is_active=0 (deactivated)"] = 1
    if row["provider_config"] is not None:
        reason = provider_config_reason(row["provider_config"])
        if reason:
            blockers["assets.provider_config (%s)" % reason] = 1
    if row["metadata"] is not None:
        meta, valid = _json_object(row["metadata"])
        if not valid:
            blockers["assets.metadata (not a JSON object)"] = 1
        else:
            extra = sorted(set(meta) - AUTO_METADATA_KEYS)
            if extra:
                blockers["assets.metadata (user keys: %s)" % ",".join(extra)] = 1
    return blockers


def inspect(
    conn: sqlite3.Connection,
    asset_id: str,
    present: set[str],
    cols: dict[str, set[str]],
    counterpart: sqlite3.Row | None,
) -> tuple[dict[str, int], dict[str, int]]:
    """Return (blocking references, removable generated rows) for one asset."""

    def has(table: str, column: str = "asset_id") -> bool:
        return table in present and column in cols.get(table, set())

    blockers: dict[str, int] = {}
    removable: dict[str, int] = {}

    for table in BLOCKING_TABLES + PROTECTED_TABLES:
        if has(table):
            found = _count(
                conn, 'SELECT COUNT(*) FROM "%s" WHERE asset_id = ?' % table, (asset_id,)
            )
            if found:
                blockers[table] = found

    if has("quotes"):
        total = _count(conn, "SELECT COUNT(*) FROM quotes WHERE asset_id = ?", (asset_id,))
        protected = _count(
            conn,
            "SELECT COUNT(*) FROM quotes WHERE asset_id = ? AND NOT (%s)"
            % QUOTE_GENERATED_PREDICATE,
            (asset_id,),
        )
        if protected:
            blockers["quotes (manual price or user note)"] = protected
        removable["quotes"] = total - protected

    if has("asset_taxonomy_assignments"):
        generated = _count(
            conn,
            "SELECT COUNT(*) FROM asset_taxonomy_assignments WHERE asset_id = ? AND %s"
            % TAXONOMY_GENERATED_PREDICATE,
            (asset_id,),
        )
        total = _count(
            conn,
            "SELECT COUNT(*) FROM asset_taxonomy_assignments WHERE asset_id = ?",
            (asset_id,),
        )
        if total - generated:
            blockers["asset_taxonomy_assignments (non-generated source)"] = total - generated
        removable["asset_taxonomy_assignments"] = generated

    # Device sync: global gate plus per-row checks for the rows we would delete.
    sync_reason = device_sync_reason(conn, present, cols)
    if sync_reason:
        blockers["device sync (%s)" % sync_reason] = 1
    sync_targets = [
        ("asset", "SELECT ?", (asset_id,)),
        ("asset_logo", "SELECT ?", (asset_id,)),
    ]
    if has("quotes"):
        sync_targets.append(("quote", "SELECT id FROM quotes WHERE asset_id = ?", (asset_id,)))
    if has("asset_taxonomy_assignments"):
        sync_targets.append(
            (
                "asset_taxonomy_assignment",
                "SELECT id FROM asset_taxonomy_assignments WHERE asset_id = ? AND %s"
                % TAXONOMY_GENERATED_PREDICATE,
                (asset_id,),
            )
        )
    for table in SYNC_ROW_TABLES:
        if not has(table, "entity_id") or "entity" not in cols.get(table, set()):
            continue
        for entity, id_sql, params in sync_targets:
            found = _count(
                conn,
                'SELECT COUNT(*) FROM "%s" WHERE entity = ? AND entity_id IN (%s)'
                % (table, id_sql),
                (entity,) + params,
            )
            if found:
                blockers["%s (%s, device sync)" % (table, entity)] = found

    table, subject_col, type_col = ALLOCATION_SUBJECT_TABLE
    if has(table, subject_col) and type_col in cols.get(table, set()):
        found = _count(
            conn,
            'SELECT COUNT(*) FROM "%s" WHERE %s = ? AND UPPER(COALESCE(%s,\'\')) = \'ASSET\''
            % (table, subject_col, type_col),
            (asset_id,),
        )
        if found:
            blockers[table] = found

    # ponytail: LIKE-scan the JSON columns per candidate (O(rows) each). Fine for
    # a handful of candidates; add a json_each index if it ever hurts.
    for table, column in JSON_REFERENCE_COLUMNS:
        if has(table, column):
            found = _count(
                conn,
                'SELECT COUNT(*) FROM "%s" WHERE "%s" LIKE ?' % (table, column),
                ("%" + asset_id + "%",),
            )
            if found:
                blockers["%s.%s" % (table, column)] = found

    # Any asset_id table we do not know is treated as a blocking reference.
    for table in present:
        if table in _KNOWN_ASSET_TABLES or "asset_id" not in cols.get(table, set()):
            continue
        found = _count(
            conn, 'SELECT COUNT(*) FROM "%s" WHERE asset_id = ?' % table, (asset_id,)
        )
        if found:
            blockers["%s.asset_id (unrecognised)" % table] = found

    row = conn.execute(
        "SELECT name, notes, metadata, is_active, provider_config FROM assets WHERE id = ?",
        (asset_id,),
    ).fetchone()
    if row is not None:
        blockers.update(asset_field_blockers(row))
        if counterpart is not None:
            left = (row["name"] or "").strip()
            right = (counterpart["name"] or "").strip()
            if left != right:
                blockers["assets.name differs from counterpart"] = 1

    return blockers, removable


def candidates(conn: sqlite3.Connection, from_ccy: str, to_ccy: str) -> list[dict]:
    """PHP-quoted CRYPTO rows plus their active USD counterparts."""
    rows = conn.execute(
        """
        SELECT id, instrument_symbol, kind
        FROM assets
        WHERE instrument_type = 'CRYPTO'
          AND quote_ccy = ?
          AND quote_mode = 'MARKET'
          AND instrument_symbol IS NOT NULL
        ORDER BY instrument_symbol, id
        """,
        (from_ccy,),
    ).fetchall()
    result = []
    for row in rows:
        counterparts = conn.execute(
            """
            SELECT id, name
            FROM assets
            WHERE instrument_type = 'CRYPTO'
              AND instrument_symbol = ?
              AND quote_ccy = ?
              AND is_active = 1
              AND kind = ?
              AND id <> ?
            ORDER BY id
            """,
            (row["instrument_symbol"], to_ccy, row["kind"], row["id"]),
        ).fetchall()
        result.append(
            {
                "id": row["id"],
                "symbol": row["instrument_symbol"],
                "counterparts": [dict(c) for c in counterparts],
            }
        )
    return result


def plan(conn: sqlite3.Connection, from_ccy: str, to_ccy: str) -> list[dict]:
    present = set(_tables(conn))
    cols = {table: _columns(conn, table) for table in present}
    decisions = []
    for cand in candidates(conn, from_ccy, to_ccy):
        counterparts = cand["counterparts"]
        if not counterparts:
            continue
        counterpart_id = counterparts[0]["id"] if len(counterparts) == 1 else None
        if len(counterparts) > 1:
            blockers, removable = {}, {}
            action, reason = "SKIP", "multiple active %s counterparts" % to_ccy
        else:
            blockers, removable = inspect(
                conn, cand["id"], present, cols, counterparts[0]
            )
            if blockers:
                action, reason = "SKIP", "blocked by " + "; ".join(
                    "%s (%d)" % (t, c) for t, c in sorted(blockers.items())
                )
            else:
                action, reason = "DELETE", "orphaned connector asset"
        decisions.append(
            {
                "id": cand["id"],
                "symbol": cand["symbol"],
                "counterpart": counterpart_id,
                "action": action,
                "reason": reason,
                "blockers": blockers,
                "removable": removable,
            }
        )
    return decisions


def binance_fiat_buy_ids(
    conn: sqlite3.Connection, asset_id: str, day: str, from_ccy: str
) -> list[str]:
    """Find Binance BUY activities for exactly this asset, currency and date."""
    rows = conn.execute(
        """
        SELECT id
        FROM activities
        WHERE asset_id = ?
          AND substr(trim(activity_date), 1, 10) = ?
          AND UPPER(trim(currency)) = UPPER(?)
          AND UPPER(trim(COALESCE(source_system, ''))) = 'BINANCE'
          AND UPPER(trim(COALESCE(NULLIF(trim(activity_type_override), ''), activity_type))) = 'BUY'
        ORDER BY id
        """,
        (asset_id, day, from_ccy),
    ).fetchall()
    return [row[0] for row in rows]


def _sync_references(
    conn: sqlite3.Connection,
    present: set[str],
    cols: dict[str, set[str]],
    targets: list[tuple[str, str]],
) -> list[str]:
    """Return sync references for (entity, entity_id) target pairs."""
    found = []
    for table in SYNC_ROW_TABLES:
        if table not in present or not {"entity", "entity_id"} <= cols.get(table, set()):
            continue
        for entity, entity_id in targets:
            count = _count(
                conn,
                'SELECT COUNT(*) FROM "%s" WHERE entity = ? AND entity_id = ?' % table,
                (entity, entity_id),
            )
            if count:
                found.append("%s:%s:%s (%d)" % (table, entity, entity_id, count))
    return found


def plan_stale_broker_quotes(
    conn: sqlite3.Connection, from_ccy: str, to_ccy: str
) -> list[dict]:
    """Report old cross-currency BROKER quotes backed by Binance fiat BUYs.

    The query is intentionally scoped to active, market-priced USD CRYPTO
    assets and PHP BROKER quotes. Rows that fail the activity, notes or sync
    checks are reported as SKIP, never modified.
    """
    present = set(_tables(conn))
    cols = {table: _columns(conn, table) for table in present}
    rows = conn.execute(
        """
        SELECT q.id, q.asset_id, q.day, q.currency, q.notes,
               a.instrument_symbol, a.quote_ccy
        FROM quotes q
        JOIN assets a ON a.id = q.asset_id
        WHERE UPPER(trim(q.source)) = ?
          AND UPPER(trim(q.currency)) = UPPER(?)
          AND UPPER(trim(a.quote_ccy)) = UPPER(?)
          AND a.instrument_type = 'CRYPTO'
          AND a.quote_mode = 'MARKET'
          AND a.is_active = 1
        ORDER BY q.asset_id, q.day, q.id
        """,
        (STALE_BROKER_SOURCE, from_ccy, to_ccy),
    ).fetchall()

    decisions = []
    global_sync_reason = device_sync_reason(conn, present, cols)
    for row in rows:
        quote_id, asset_id, day, currency, notes, symbol, asset_ccy = row
        activity_ids = binance_fiat_buy_ids(conn, asset_id, day, from_ccy)
        blockers = {}
        if notes is not None and str(notes).strip():
            blockers["quote note (user data)"] = 1
        if not activity_ids:
            blockers["no matching PHP BINANCE BUY activity"] = 1
        if global_sync_reason:
            blockers["device sync (%s)" % global_sync_reason] = 1
        sync_targets = [("quote", quote_id)] + [("activity", item) for item in activity_ids]
        sync_refs = _sync_references(conn, present, cols, sync_targets)
        if sync_refs:
            blockers["device sync references"] = len(sync_refs)

        action = "SKIP" if blockers else "DELETE_QUOTE"
        reason = (
            "blocked by " + "; ".join("%s (%d)" % item for item in sorted(blockers.items()))
            if blockers
            else "PHP BROKER quote linked to same-day PHP BINANCE BUY"
        )
        decisions.append(
            {
                "id": quote_id,
                "asset_id": asset_id,
                "symbol": symbol,
                "day": day,
                "currency": currency,
                "asset_currency": asset_ccy,
                "matching_activity_ids": activity_ids,
                "section": "cross_currency_quotes",
                "action": action,
                "reason": reason,
                "blockers": blockers,
            }
        )
    return decisions


def delete_stale_broker_quotes(
    conn: sqlite3.Connection, decisions: list[dict], from_ccy: str, to_ccy: str
) -> int:
    """Delete eligible stale quotes after rechecking every guard under lock."""
    present = set(_tables(conn))
    cols = {table: _columns(conn, table) for table in present}
    removed = 0
    for decision in decisions:
        if decision["action"] != "DELETE_QUOTE":
            continue
        quote = conn.execute(
            """
            SELECT q.id, q.asset_id, q.day, q.currency, q.source, q.notes,
                   a.instrument_symbol, a.quote_ccy, a.instrument_type,
                   a.quote_mode, a.is_active
            FROM quotes q JOIN assets a ON a.id = q.asset_id
            WHERE q.id = ?
            """,
            (decision["id"],),
        ).fetchone()
        if quote is None:
            raise SchemaError("quote %s disappeared after planning" % decision["id"])
        if (
            quote["asset_id"] != decision["asset_id"]
            or quote["source"].strip().upper() != STALE_BROKER_SOURCE
            or quote["currency"].strip().upper() != from_ccy.upper()
            or quote["quote_ccy"].strip().upper() != to_ccy.upper()
            or quote["instrument_type"] != "CRYPTO"
            or quote["quote_mode"] != "MARKET"
            or quote["is_active"] != 1
            or (quote["notes"] is not None and str(quote["notes"]).strip())
        ):
            raise SchemaError("quote %s is no longer eligible" % decision["id"])
        activity_ids = binance_fiat_buy_ids(conn, quote["asset_id"], quote["day"], from_ccy)
        if not activity_ids:
            raise SchemaError("quote %s lost its matching Binance BUY" % decision["id"])
        sync_reason = device_sync_reason(conn, present, cols)
        if sync_reason:
            raise SchemaError("device sync became active: %s" % sync_reason)
        sync_refs = _sync_references(
            conn,
            present,
            cols,
            [("quote", quote["id"])] + [("activity", item) for item in activity_ids],
        )
        if sync_refs:
            raise SchemaError("quote %s has device-sync references: %s" % (quote["id"], sync_refs))

        conn.execute(
            """
            DELETE FROM quotes
            WHERE id = ? AND asset_id = ? AND UPPER(trim(source)) = ?
              AND UPPER(trim(currency)) = UPPER(?)
              AND COALESCE(trim(notes), '') = ''
            """,
            (quote["id"], quote["asset_id"], STALE_BROKER_SOURCE, from_ccy),
        )
        count = conn.execute("SELECT changes()").fetchone()[0]
        if count != 1:
            raise SchemaError("quote %s delete count was %d, expected 1" % (quote["id"], count))
        removed += count
    return removed


def delete_orphans(conn: sqlite3.Connection, decisions: list[dict]) -> dict[str, int]:
    """Delete planned orphans. Caller must already hold a write transaction.

    The reference scan is re-run here, under the lock, so an asset that gained a
    reference after planning is refused instead of half-deleted.
    """
    present = set(_tables(conn))
    cols = {table: _columns(conn, table) for table in present}
    removed = {table: 0 for table in ("assets", "quotes", "asset_taxonomy_assignments")}
    removed.update({table: 0 for table in DELETABLE_TABLES})

    for decision in decisions:
        if decision["action"] != "DELETE":
            continue
        asset_id = decision["id"]

        counterpart_id = decision.get("counterpart")
        counterpart = None
        if counterpart_id:
            row = conn.execute(
                "SELECT id, name FROM assets WHERE id = ?", (counterpart_id,)
            ).fetchone()
            counterpart = dict(row) if row else None
        if not counterpart or counterpart["id"] == asset_id:
            raise SchemaError(
                "asset %s lost its replacement counterpart after planning" % asset_id
            )
        blockers, _ = inspect(conn, asset_id, present, cols, counterpart)
        if blockers:
            raise SchemaError(
                "asset %s became unsafe after planning: %s" % (asset_id, blockers)
            )

        for table, predicate in (
            ("asset_taxonomy_assignments", TAXONOMY_GENERATED_PREDICATE),
            ("quotes", QUOTE_GENERATED_PREDICATE),
        ):
            if table not in present:
                continue
            conn.execute(
                'DELETE FROM "%s" WHERE asset_id = ? AND (%s)' % (table, predicate),
                (asset_id,),
            )
            removed[table] += conn.execute("SELECT changes()").fetchone()[0]

        for table in DELETABLE_TABLES:
            if table in present:
                conn.execute('DELETE FROM "%s" WHERE asset_id = ?' % table, (asset_id,))
                removed[table] += conn.execute("SELECT changes()").fetchone()[0]

        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise SchemaError("asset %s disappeared during the transaction" % asset_id)
        removed["assets"] += 1

    return removed


def backup(path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = "%s.bak-%s" % (path, stamp)
    counter = 1
    while os.path.exists(base):
        counter += 1
        base = "%s.bak-%s-%d" % (path, stamp, counter)
    for suffix in ("", "-wal", "-shm"):
        src = path + suffix
        if os.path.exists(src):
            shutil.copy2(src, base + suffix)
    return base


def execute(
    db_path: str, from_ccy: str, to_ccy: str, apply: bool
) -> tuple[dict[str, list[dict]], dict[str, int] | None, str | None]:
    """Plan, and (when applying) delete in one locked, foreign-key-checked tx."""
    check_header(db_path)

    conn = connect(db_path, readonly=True)
    try:
        verify_schema(conn)
        preview = {
            "stale_php_assets": plan(conn, from_ccy, to_ccy),
            "cross_currency_quotes": plan_stale_broker_quotes(conn, from_ccy, to_ccy),
        }
    finally:
        conn.close()

    has_targets = any(
        d["action"] == action
        for section, action in (("stale_php_assets", "DELETE"), ("cross_currency_quotes", "DELETE_QUOTE"))
        for d in preview[section]
    )
    if not apply or not has_targets:
        return preview, None, None

    backup_path = backup(db_path)  # before the file is opened for writing
    conn = connect(db_path, readonly=False)
    try:
        verify_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise SchemaError("could not enable PRAGMA foreign_keys")
        conn.execute("BEGIN IMMEDIATE")
        try:
            decisions = {
                "stale_php_assets": plan(conn, from_ccy, to_ccy),
                "cross_currency_quotes": plan_stale_broker_quotes(conn, from_ccy, to_ccy),
            }
            removed = delete_orphans(conn, decisions["stale_php_assets"])
            removed["cross_currency_quotes"] = delete_stale_broker_quotes(
                conn, decisions["cross_currency_quotes"], from_ccy, to_ccy
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise SchemaError(
                    "foreign_key_check failed after deletion: %s" % (violations[:5],)
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return decisions, removed, backup_path
    finally:
        conn.close()


def render(report: dict[str, list[dict]], from_ccy: str, to_ccy: str, removed, backup_path) -> None:
    targets = 0
    for section, label, action in (
        ("stale_php_assets", "Stale PHP crypto assets", "DELETE"),
        ("cross_currency_quotes", "Stale cross-currency broker quotes", "DELETE_QUOTE"),
    ):
        decisions = report[section]
        selected = [d for d in decisions if d["action"] == action]
        targets += len(selected)
        print("%s: %d candidate(s), %d eligible, %d skipped"
              % (label, len(decisions), len(selected), len(decisions) - len(selected)))
        for d in decisions:
            if section == "stale_php_assets":
                detail = "%s %s/%s -> %s" % (d["symbol"], from_ccy, d["symbol"], to_ccy)
            else:
                detail = "%s %s %s -> asset quote %s (activity %s)" % (
                    d["symbol"], d["day"], d["currency"], d["asset_currency"],
                    ",".join(d["matching_activity_ids"]) or "none",
                )
            print("  [%s] %s  %s" % (d["action"], detail, d["reason"]))
            if d["action"] == action and section == "stale_php_assets":
                print("         id=%s rows: %s" % (d["id"], d["removable"] or "{}"))
    if backup_path:
        print("Backup: %s" % backup_path)
    if removed is not None:
        print("Applied:", removed)
    elif not targets:
        print("Nothing to do; no backup taken.")
    else:
        print("Dry run. Re-run with --apply to delete the rows above.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database", help="path to the Wealthfolio SQLite database")
    parser.add_argument("--from-ccy", default="PHP", help="stale quote currency (default PHP)")
    parser.add_argument("--to-ccy", default="USD", help="replacement quote currency (default USD)")
    parser.add_argument("--apply", action="store_true", help="delete rows (default: dry run)")
    parser.add_argument("--json", action="store_true", help="print a JSON summary")
    args = parser.parse_args(argv)

    try:
        decisions, removed, backup_path = execute(
            args.database, args.from_ccy, args.to_ccy, args.apply
        )
    except (SchemaError, sqlite3.Error) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {"backup": backup_path, "decisions": decisions, "removed": removed},
                indent=2,
            )
        )
    else:
        render(decisions, args.from_ccy, args.to_ccy, removed, backup_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
