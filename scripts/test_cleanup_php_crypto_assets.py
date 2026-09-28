"""Tests for scripts/cleanup_php_crypto_assets.py.

Run from the repository root:

    python -m unittest discover -s scripts -p "test_*.py" -v

The in-memory schema mirrors the real Wealthfolio tables (with foreign keys) so
cascade behaviour and PRAGMA foreign_key_check are exercised for real.
"""

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cleanup_php_crypto_assets as mod  # noqa: E402

SCHEMA = """
CREATE TABLE assets (
    id TEXT PRIMARY KEY,
    kind TEXT,
    name TEXT,
    notes TEXT,
    metadata TEXT,
    is_active INTEGER DEFAULT 1,
    provider_config TEXT,
    instrument_type TEXT,
    instrument_symbol TEXT,
    quote_mode TEXT,
    quote_ccy TEXT
);
CREATE TABLE activities (
    id TEXT PRIMARY KEY,
    asset_id TEXT,
    activity_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    activity_type_override TEXT,
    activity_date TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'USD',
    source_system TEXT
);
CREATE TABLE quotes (
    id TEXT PRIMARY KEY,
    asset_id TEXT REFERENCES assets(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'YAHOO',
    notes TEXT,
    day TEXT NOT NULL DEFAULT '2026-01-01',
    currency TEXT NOT NULL DEFAULT 'USD'
);
CREATE TABLE quote_sync_state (asset_id TEXT PRIMARY KEY);
CREATE TABLE asset_taxonomy_assignments (
    id TEXT PRIMARY KEY,
    asset_id TEXT REFERENCES assets(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE asset_logos (asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE);
CREATE TABLE lots (id TEXT PRIMARY KEY, asset_id TEXT REFERENCES assets(id) ON DELETE CASCADE);
CREATE TABLE lot_disposals (id TEXT PRIMARY KEY, asset_id TEXT REFERENCES assets(id) ON DELETE CASCADE);
CREATE TABLE snapshot_positions (id TEXT PRIMARY KEY, asset_id TEXT REFERENCES assets(id) ON DELETE CASCADE);
CREATE TABLE holdings_snapshots (id TEXT PRIMARY KEY, positions TEXT DEFAULT '{}');
CREATE TABLE app_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT);
CREATE TABLE sync_device_config (device_id TEXT PRIMARY KEY, trust_state TEXT NOT NULL DEFAULT 'untrusted');
CREATE TABLE sync_engine_state (id INTEGER PRIMARY KEY, last_push_at TEXT, last_pull_at TEXT);
CREATE TABLE sync_outbox (event_id TEXT PRIMARY KEY, entity TEXT, entity_id TEXT);
CREATE TABLE sync_entity_metadata (entity TEXT, entity_id TEXT, PRIMARY KEY (entity, entity_id));
CREATE TABLE sync_applied_events (event_id TEXT PRIMARY KEY, entity TEXT, entity_id TEXT);
"""


def make_db(extra_schema: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.executescript(SCHEMA + extra_schema)
    return conn


def add_asset(conn, asset_id, symbol="BTC", ccy="PHP", active=1, kind="INVESTMENT", **fields):
    columns = {
        "name": fields.get("name"),
        "notes": fields.get("notes"),
        "metadata": fields.get("metadata"),
        "provider_config": fields.get("provider_config"),
    }
    conn.execute(
        "INSERT INTO assets (id, kind, name, notes, metadata, is_active, provider_config, "
        "instrument_type, instrument_symbol, quote_mode, quote_ccy) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'CRYPTO', ?, 'MARKET', ?)",
        (
            asset_id,
            kind,
            columns["name"],
            columns["notes"],
            columns["metadata"],
            active,
            columns["provider_config"],
            symbol,
            ccy,
        ),
    )


def add_pair(conn, **fields):
    add_asset(conn, "php-1", ccy="PHP", **fields)
    add_asset(conn, "usd-1", ccy="USD", **fields)


def add_activity(
    conn,
    activity_id,
    asset_id,
    day,
    currency="PHP",
    source_system="BINANCE",
    activity_type="BUY",
    activity_type_override=None,
):
    conn.execute(
        "INSERT INTO activities "
        "(id, asset_id, activity_type, activity_type_override, activity_date, currency, source_system) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (activity_id, asset_id, activity_type, activity_type_override, day, currency, source_system),
    )


def apply(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        removed = mod.delete_orphans(conn, mod.plan(conn, "PHP", "USD"))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return removed


def apply_full(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        removed = mod.delete_orphans(conn, mod.plan(conn, "PHP", "USD"))
        quote_plan = mod.plan_stale_broker_quotes(conn, "PHP", "USD")
        removed["cross_currency_quotes"] = mod.delete_stale_broker_quotes(
            conn, quote_plan, "PHP", "USD"
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return removed


def decision_for(conn, asset_id="php-1"):
    return [d for d in mod.plan(conn, "PHP", "USD") if d["id"] == asset_id][0]


class PlanTest(unittest.TestCase):
    def test_orphan_php_asset_is_deletable_with_its_generated_rows(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO quotes(id, asset_id, source, notes) VALUES ('q1', 'php-1', 'YAHOO', NULL)")
        conn.execute("INSERT INTO quotes(id, asset_id, source, notes) VALUES ('q2', 'php-1', 'YAHOO', NULL)")
        conn.execute("INSERT INTO quote_sync_state VALUES ('php-1')")
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a1', 'php-1', 'migrated')")
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a2', 'php-1', 'AUTO')")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "DELETE")
        self.assertEqual(decision["removable"]["quotes"], 2)
        self.assertEqual(decision["removable"]["asset_taxonomy_assignments"], 2)

    def test_manual_quote_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO quotes(id, asset_id, source, notes) VALUES ('q1', 'php-1', 'MANUAL', NULL)")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("manual price", decision["reason"])

    def test_quoted_user_note_blocks_even_when_source_is_a_provider(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO quotes(id, asset_id, source, notes) VALUES ('q1', 'php-1', 'YAHOO', 'valuation note')")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("user note", decision["reason"])

    def test_blank_note_does_not_block(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO quotes(id, asset_id, source, notes) VALUES ('q1', 'php-1', 'YAHOO', '   ')")
        self.assertEqual(decision_for(conn)["action"], "DELETE")

    def test_unknown_taxonomy_source_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a1', 'php-1', 'ai')")
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_manual_taxonomy_source_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a1', 'php-1', 'manual')")
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_generated_taxonomy_sources_match_case_insensitively(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a1', 'php-1', 'Migrated')")
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a2', 'php-1', 'auto')")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "DELETE")
        self.assertEqual(decision["removable"]["asset_taxonomy_assignments"], 2)

    def test_custom_logo_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO asset_logos VALUES ('php-1')")
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_every_blocking_table_blocks(self):
        cases = {
            "activities": "INSERT INTO activities(id, asset_id) VALUES ('x', 'php-1')",
            "lots": "INSERT INTO lots VALUES ('x', 'php-1')",
            "lot_disposals": "INSERT INTO lot_disposals VALUES ('x', 'php-1')",
            "snapshot_positions": "INSERT INTO snapshot_positions VALUES ('x', 'php-1')",
            "holdings_snapshots.positions": (
                "INSERT INTO holdings_snapshots VALUES ('s1', '{\"php-1\": {}}')"
            ),
        }
        for name, insert in cases.items():
            conn = make_db()
            add_pair(conn)
            conn.execute(insert)
            decision = decision_for(conn)
            self.assertEqual(decision["action"], "SKIP", name)
            self.assertTrue(decision["blockers"], name)

    def test_allocation_constraint_blocks_only_for_asset_subject(self):
        schema = (
            "CREATE TABLE allocation_target_constraints "
            "(id TEXT, subject_type TEXT, subject_id TEXT);"
        )
        conn = make_db(schema)
        add_pair(conn)
        conn.execute("INSERT INTO allocation_target_constraints VALUES ('c1','category','php-1')")
        self.assertEqual(decision_for(conn)["action"], "DELETE")
        conn.execute("INSERT INTO allocation_target_constraints VALUES ('c2','asset','php-1')")
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_unknown_asset_id_table_blocks(self):
        conn = make_db("CREATE TABLE mystery (id TEXT PRIMARY KEY, asset_id TEXT);")
        add_pair(conn)
        conn.execute("INSERT INTO mystery VALUES ('m1', 'php-1')")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("unrecognised", decision["reason"])

    def test_multiple_counterparts_block(self):
        conn = make_db()
        add_asset(conn, "php-1")
        add_asset(conn, "usd-1", ccy="USD")
        add_asset(conn, "usd-2", ccy="USD")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("multiple", decision["reason"])

    def test_missing_or_inactive_counterpart_is_not_a_candidate(self):
        conn = make_db()
        add_asset(conn, "php-1")
        add_asset(conn, "usd-1", ccy="USD", active=0)
        self.assertEqual(mod.plan(conn, "PHP", "USD"), [])

        conn2 = make_db()
        add_asset(conn2, "php-1")
        self.assertEqual(mod.plan(conn2, "PHP", "USD"), [])

    def test_kind_mismatch_is_not_a_candidate(self):
        conn = make_db()
        add_asset(conn, "php-1", kind="OTHER")
        add_asset(conn, "usd-1", ccy="USD", kind="INVESTMENT")
        self.assertEqual(mod.plan(conn, "PHP", "USD"), [])

    def test_usd_asset_is_never_a_candidate(self):
        conn = make_db()
        add_asset(conn, "usd-1", ccy="USD")
        self.assertEqual(mod.plan(conn, "PHP", "USD"), [])

    def test_planning_does_not_modify(self):
        conn = make_db()
        add_pair(conn)
        before = decision_for(conn)
        self.assertEqual(before, decision_for(conn))
        self.assertIsNotNone(conn.execute("SELECT id FROM assets WHERE id='php-1'").fetchone())


class UserOwnedFieldTest(unittest.TestCase):
    """A user-configured PHP asset must never be silently deleted."""

    def test_custom_scraper_provider_blocks(self):
        conn = make_db()
        add_pair(
            conn,
            provider_config='{"preferred_provider":"CUSTOM_SCRAPER","custom_provider_id":"c1"}',
        )
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("provider_config", decision["reason"])

    def test_unknown_provider_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute(
            "UPDATE assets SET provider_config = '{\"preferred_provider\":\"MY_BANK\"}' "
            "WHERE id = 'php-1'"
        )
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_auto_yahoo_crypto_provider_config_is_allowed(self):
        conn = make_db()
        add_pair(
            conn,
            provider_config='{"preferred_provider":"YAHOO","overrides":{"YAHOO":'
            '{"type":"crypto_symbol","symbol":"BTC-PHP"}}}',
        )
        self.assertEqual(decision_for(conn)["action"], "DELETE")

    def test_unexpected_provider_config_key_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute(
            "UPDATE assets SET provider_config = "
            "'{\"preferred_provider\":\"YAHOO\",\"custom_provider_code\":\"x\"}' "
            "WHERE id = 'php-1'"
        )
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_override_provider_and_type_must_be_known(self):
        conn = make_db()
        add_pair(conn)
        conn.execute(
            "UPDATE assets SET provider_config = "
            "'{\"preferred_provider\":\"YAHOO\",\"overrides\":{\"MY_BANK\":{\"symbol\":\"X\"}}}' "
            "WHERE id = 'php-1'"
        )
        self.assertEqual(decision_for(conn)["action"], "SKIP")

        conn.execute(
            "UPDATE assets SET provider_config = "
            "'{\"preferred_provider\":\"YAHOO\",\"overrides\":{\"YAHOO\":{\"type\":\"weird\"}}}' "
            "WHERE id = 'php-1'"
        )
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_invalid_provider_config_json_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("UPDATE assets SET provider_config = 'not json' WHERE id = 'php-1'")
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_asset_notes_block(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("UPDATE assets SET notes = 'my PHP position' WHERE id = 'php-1'")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("notes", decision["reason"])

    def test_inactive_asset_blocks(self):
        conn = make_db()
        add_asset(conn, "php-1", ccy="PHP", active=0)
        add_asset(conn, "usd-1", ccy="USD")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("is_active", decision["reason"])

    def test_user_metadata_key_blocks_but_auto_keys_are_allowed(self):
        conn = make_db()
        add_pair(
            conn,
            metadata='{"legacy":{"old_id":"BTC"},"identifiers":{"isin":"X"},'
            '"sectors":"[]","marketCap":1}',
        )
        self.assertEqual(decision_for(conn)["action"], "DELETE")

        conn.execute(
            "UPDATE assets SET metadata = '{\"watchlist\":\"mine\"}' WHERE id = 'php-1'"
        )
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("metadata", decision["reason"])

    def test_invalid_metadata_json_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("UPDATE assets SET metadata = '[' WHERE id = 'php-1'")
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_name_mismatch_blocks(self):
        conn = make_db()
        add_pair(conn, name="Bitcoin")
        self.assertEqual(decision_for(conn)["action"], "DELETE")

        conn.execute("UPDATE assets SET name = 'My Coin' WHERE id = 'php-1'")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("name", decision["reason"])

    def test_unset_names_do_not_block(self):
        conn = make_db()
        add_pair(conn)
        self.assertEqual(decision_for(conn)["action"], "DELETE")


class DeviceSyncTest(unittest.TestCase):
    def test_sync_enabled_blocks_everything(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO app_settings VALUES ('sync_enabled', 'true')")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("device sync", decision["reason"])

    def test_sync_disabled_explicitly_does_not_block(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO app_settings VALUES ('sync_enabled', 'false')")
        self.assertEqual(decision_for(conn)["action"], "DELETE")

    def test_trusted_device_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO sync_device_config VALUES ('dev-1', 'trusted')")
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_sync_engine_history_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute(
            "INSERT INTO sync_engine_state VALUES (1, '2026-01-01T00:00:00Z', NULL)"
        )
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_outbox_rows_block(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO sync_outbox VALUES ('e1', 'asset', 'php-1')")
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_quote_sync_metadata_row_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO quotes(id, asset_id, source, notes) VALUES ('q1', 'php-1', 'YAHOO', NULL)")
        # shared per-row sync state for the quote we would delete
        conn.execute("INSERT INTO sync_entity_metadata VALUES ('quote', 'q1')")
        decision = decision_for(conn)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("device sync", decision["reason"])

    def test_assignment_sync_metadata_row_blocks(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a1', 'php-1', 'migrated')")
        conn.execute(
            "INSERT INTO sync_entity_metadata VALUES ('asset_taxonomy_assignment', 'a1')"
        )
        self.assertEqual(decision_for(conn)["action"], "SKIP")

    def test_sync_metadata_for_unrelated_entity_does_not_block(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO sync_entity_metadata VALUES ('goal', 'php-1')")
        self.assertEqual(decision_for(conn)["action"], "DELETE")


class CrossCurrencyQuoteTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_asset(self.conn, "usd-sol", symbol="SOL", ccy="USD", name="Solana")

    def add_quote(self, quote_id, day="2026-09-01", currency="PHP", source="BROKER", notes=None,
                  asset_id="usd-sol"):
        self.conn.execute(
            "INSERT INTO quotes(id, asset_id, day, source, currency, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (quote_id, asset_id, day, source, currency, notes),
        )

    def plan(self):
        return mod.plan_stale_broker_quotes(self.conn, "PHP", "USD")

    def test_only_same_asset_day_binance_php_buy_is_eligible(self):
        self.add_quote("stale")
        add_activity(self.conn, "buy-1", "usd-sol", "2026-09-01T08:00:00Z")
        decision = self.plan()[0]
        self.assertEqual(decision["action"], "DELETE_QUOTE")
        self.assertEqual(decision["matching_activity_ids"], ["buy-1"])
        self.assertEqual(decision["asset_currency"], "USD")

    def test_activity_currency_source_type_and_day_must_all_match(self):
        cases = [
            ("EUR", "BINANCE", "BUY", "2026-09-01"),
            ("PHP", "OKX", "BUY", "2026-09-01"),
            ("PHP", "BINANCE", "SELL", "2026-09-01"),
            ("PHP", "BINANCE", "BUY", "2026-09-02"),
        ]
        for index, (currency, source, kind, day) in enumerate(cases):
            quote_id = "q%d" % index
            self.add_quote(quote_id)
            add_activity(self.conn, "a%d" % index, "usd-sol", day, currency, source, kind)
        decisions = self.plan()
        self.assertEqual([d["action"] for d in decisions], ["SKIP"] * len(cases))
        self.assertTrue(all("no matching" in d["reason"] for d in decisions))

    def test_activity_type_override_is_the_effective_type(self):
        self.add_quote("override")
        add_activity(
            self.conn, "a1", "usd-sol", "2026-09-01", activity_type="UNKNOWN",
            activity_type_override="BUY",
        )
        self.assertEqual(self.plan()[0]["action"], "DELETE_QUOTE")

    def test_noted_quote_is_reported_but_never_deleted(self):
        self.add_quote("noted", notes="keep my valuation")
        add_activity(self.conn, "a1", "usd-sol", "2026-09-01")
        decision = self.plan()[0]
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("quote note", decision["reason"])

    def test_manual_and_usd_quotes_are_not_candidates(self):
        self.add_quote("manual", source="MANUAL", notes="manual PHP quote")
        self.add_quote("usd-broker", currency="USD")
        self.assertEqual(self.plan(), [])

    def test_other_assets_are_not_candidates(self):
        add_asset(self.conn, "cad-sol", symbol="SOL", ccy="CAD")
        add_asset(self.conn, "usd-stock", symbol="ABC", ccy="USD")
        self.add_quote("wrong-ccy", asset_id="cad-sol")
        self.add_quote("equity", asset_id="usd-stock")
        self.conn.execute("UPDATE assets SET instrument_type='EQUITY' WHERE id='usd-stock'")
        self.assertEqual(self.plan(), [])

    def test_sync_reference_on_quote_or_activity_blocks(self):
        self.add_quote("synced")
        add_activity(self.conn, "a1", "usd-sol", "2026-09-01")
        self.conn.execute("INSERT INTO sync_entity_metadata VALUES ('quote', 'synced')")
        decision = self.plan()[0]
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("device sync references", decision["reason"])

        self.conn.execute("DELETE FROM sync_entity_metadata")
        self.conn.execute("INSERT INTO sync_applied_events VALUES ('e1', 'activity', 'a1')")
        decision = self.plan()[0]
        self.assertEqual(decision["action"], "SKIP")

    def test_delete_preserves_asset_activities_positions_and_other_quotes(self):
        self.add_quote("stale")
        self.add_quote("usd", currency="USD")
        self.add_quote("manual", source="MANUAL", currency="PHP")
        self.add_quote("noted", notes="user note")
        for n in range(4):
            add_activity(self.conn, "buy-%d" % n, "usd-sol", "2026-09-01")
        self.conn.execute("INSERT INTO snapshot_positions(id, asset_id) VALUES ('pos1', 'usd-sol')")

        removed = apply_full(self.conn)
        self.assertEqual(removed["cross_currency_quotes"], 1)
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT id FROM quotes ORDER BY id").fetchall()],
            ["manual", "noted", "usd"],
        )
        self.assertIsNotNone(self.conn.execute("SELECT id FROM assets WHERE id='usd-sol'").fetchone())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshot_positions").fetchone()[0], 1)

    def test_revalidation_aborts_if_matching_activity_disappears(self):
        self.add_quote("stale")
        add_activity(self.conn, "buy-1", "usd-sol", "2026-09-01")
        stale_plan = self.plan()
        self.conn.execute("DELETE FROM activities WHERE id='buy-1'")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(mod.SchemaError):
                mod.delete_stale_broker_quotes(self.conn, stale_plan, "PHP", "USD")
        finally:
            self.conn.execute("ROLLBACK")
        self.assertIsNotNone(self.conn.execute("SELECT id FROM quotes WHERE id='stale'").fetchone())


class ApplyTest(unittest.TestCase):
    def test_deletes_orphan_and_its_generated_rows(self):
        conn = make_db()
        add_pair(conn)
        conn.execute("INSERT INTO quotes(id, asset_id, source, notes) VALUES ('q1', 'php-1', 'YAHOO', NULL)")
        conn.execute("INSERT INTO quote_sync_state VALUES ('php-1')")
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a1', 'php-1', 'migrated')")

        removed = apply(conn)
        self.assertEqual(
            removed,
            {
                "assets": 1,
                "quotes": 1,
                "asset_taxonomy_assignments": 1,
                "quote_sync_state": 1,
            },
        )
        self.assertIsNone(conn.execute("SELECT id FROM assets WHERE id='php-1'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT id FROM assets WHERE id='usd-1'").fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)

    def test_apply_is_idempotent(self):
        conn = make_db()
        add_pair(conn)
        apply(conn)
        self.assertEqual(mod.plan(conn, "PHP", "USD"), [])
        self.assertEqual(
            apply(conn),
            {
                "assets": 0,
                "quotes": 0,
                "asset_taxonomy_assignments": 0,
                "quote_sync_state": 0,
            },
        )

    def test_reference_appearing_after_planning_aborts_the_delete(self):
        conn = make_db()
        add_pair(conn)
        stale_plan = mod.plan(conn, "PHP", "USD")
        self.assertEqual(stale_plan[0]["action"], "DELETE")

        conn.execute("INSERT INTO activities(id, asset_id) VALUES ('late', 'php-1')")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(mod.SchemaError):
                mod.delete_orphans(conn, stale_plan)
        finally:
            conn.execute("ROLLBACK")

        self.assertIsNotNone(conn.execute("SELECT id FROM assets WHERE id='php-1'").fetchone())

    def test_sync_enabled_mid_transaction_aborts_the_delete(self):
        conn = make_db()
        add_pair(conn)
        stale_plan = mod.plan(conn, "PHP", "USD")
        self.assertEqual(stale_plan[0]["action"], "DELETE")

        conn.execute("INSERT INTO app_settings VALUES ('sync_enabled', 'true')")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(mod.SchemaError):
                mod.delete_orphans(conn, stale_plan)
        finally:
            conn.execute("ROLLBACK")
        self.assertIsNotNone(conn.execute("SELECT id FROM assets WHERE id='php-1'").fetchone())

    def test_schema_mismatch_is_rejected(self):
        conn = make_db()
        conn.execute("DROP TABLE quotes")
        with self.assertRaises(mod.SchemaError):
            mod.verify_schema(conn)

    def test_missing_user_field_column_is_rejected(self):
        conn = make_db()
        conn.execute("ALTER TABLE assets DROP COLUMN provider_config")
        with self.assertRaises(mod.SchemaError):
            mod.verify_schema(conn)


class FileTest(unittest.TestCase):
    """Exercise the real code path against an on-disk database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "wealthfolio.db")
        conn = sqlite3.connect(self.db)
        conn.isolation_level = None
        conn.executescript(SCHEMA)
        add_pair(conn)
        conn.execute("INSERT INTO quotes(id, asset_id, source, notes) VALUES ('q1', 'php-1', 'YAHOO', NULL)")
        conn.execute("INSERT INTO quote_sync_state VALUES ('php-1')")
        conn.execute("INSERT INTO asset_taxonomy_assignments VALUES ('a1', 'php-1', 'migrated')")
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_dry_run_makes_no_changes_and_no_backup(self):
        decisions, removed, backup_path = mod.execute(self.db, "PHP", "USD", apply=False)
        self.assertIsNone(removed)
        self.assertIsNone(backup_path)
        self.assertEqual([d["action"] for d in decisions["stale_php_assets"]], ["DELETE"])
        self.assertEqual(decisions["cross_currency_quotes"], [])
        self.assertEqual(len(self.read("SELECT id FROM assets WHERE id='php-1'")), 1)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if ".bak-" in f], [])

    def test_apply_backs_up_then_deletes(self):
        decisions, removed, backup_path = mod.execute(self.db, "PHP", "USD", apply=True)
        self.assertEqual(removed["assets"], 1)
        self.assertTrue(backup_path and os.path.exists(backup_path))
        self.assertEqual(self.read("SELECT id FROM assets"), [("usd-1",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM quotes"), [(0,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM quote_sync_state"), [(0,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM asset_taxonomy_assignments"), [(0,)])

        backup = sqlite3.connect(backup_path)
        try:
            self.assertEqual(len(backup.execute("SELECT id FROM assets").fetchall()), 2)
        finally:
            backup.close()

    def test_apply_with_sync_enabled_deletes_nothing_and_takes_no_backup(self):
        conn = sqlite3.connect(self.db)
        conn.isolation_level = None
        conn.execute("INSERT INTO app_settings VALUES ('sync_enabled', 'true')")
        conn.close()

        decisions, removed, backup_path = mod.execute(self.db, "PHP", "USD", apply=True)
        self.assertIsNone(removed)
        self.assertIsNone(backup_path)
        self.assertEqual([d["action"] for d in decisions["stale_php_assets"]], ["SKIP"])
        self.assertEqual(len(self.read("SELECT id FROM assets WHERE id='php-1'")), 1)

    def test_apply_with_nothing_to_delete_takes_no_backup(self):
        mod.execute(self.db, "PHP", "USD", apply=True)
        strays = [f for f in os.listdir(self.tmp.name) if ".bak-" in f]
        decisions, removed, backup_path = mod.execute(self.db, "PHP", "USD", apply=True)
        self.assertEqual(decisions, {"stale_php_assets": [], "cross_currency_quotes": []})
        self.assertIsNone(removed)
        self.assertIsNone(backup_path)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if ".bak-" in f], strays)

    def test_foreign_key_failure_rolls_back(self):
        conn = sqlite3.connect(self.db)
        conn.isolation_level = None
        conn.executescript(
            "CREATE TABLE hidden_refs "
            "(id TEXT PRIMARY KEY, ref TEXT NOT NULL REFERENCES assets(id));"
        )
        conn.execute("INSERT INTO hidden_refs VALUES ('h1', 'php-1')")
        conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            mod.execute(self.db, "PHP", "USD", apply=True)

        self.assertEqual(len(self.read("SELECT id FROM assets WHERE id='php-1'")), 1)
        self.assertEqual(self.read("SELECT COUNT(*) FROM quotes"), [(1,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM quote_sync_state"), [(1,)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM asset_taxonomy_assignments"), [(1,)]
        )

    def test_backup_does_not_overwrite_existing(self):
        with open(self.db + ".probe", "wb") as handle:
            handle.write(b"payload")
        first = mod.backup(self.db)
        second = mod.backup(self.db)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first) and os.path.exists(second))

    def test_non_sqlite_file_is_rejected(self):
        other = os.path.join(self.tmp.name, "not-a-db.bin")
        with open(other, "wb") as handle:
            handle.write(b"definitely not sqlite")
        with self.assertRaises(mod.SchemaError):
            mod.execute(other, "PHP", "USD", apply=False)


class CrossCurrencyQuoteFileTest(unittest.TestCase):
    """Verify quote planning/apply/backup against a real SQLite file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "quotes.db")
        conn = sqlite3.connect(self.db)
        conn.isolation_level = None
        conn.executescript(SCHEMA)
        add_asset(conn, "sol-usd", symbol="SOL", ccy="USD", name="Solana")
        conn.execute(
            "INSERT INTO quotes(id, asset_id, day, source, currency, notes) "
            "VALUES ('stale', 'sol-usd', '2026-09-01', 'BROKER', 'PHP', NULL)"
        )
        conn.execute(
            "INSERT INTO quotes(id, asset_id, day, source, currency, notes) "
            "VALUES ('usd', 'sol-usd', '2026-09-01', 'BROKER', 'USD', NULL)"
        )
        conn.execute(
            "INSERT INTO quotes(id, asset_id, day, source, currency, notes) "
            "VALUES ('manual', 'sol-usd', '2026-09-01', 'MANUAL', 'PHP', NULL)"
        )
        conn.execute(
            "INSERT INTO quotes(id, asset_id, day, source, currency, notes) "
            "VALUES ('noted', 'sol-usd', '2026-09-01', 'BROKER', 'PHP', 'keep me')"
        )
        for n in range(4):
            add_activity(conn, "buy-%d" % n, "sol-usd", "2026-09-01T08:00:00Z")
        conn.execute("INSERT INTO snapshot_positions(id, asset_id) VALUES ('pos1', 'sol-usd')")
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_dry_run_reports_quote_section_without_writes(self):
        report, removed, backup_path = mod.execute(self.db, "PHP", "USD", apply=False)
        self.assertEqual(report["stale_php_assets"], [])
        self.assertEqual(
            [(d["id"], d["action"]) for d in report["cross_currency_quotes"]],
            [("noted", "SKIP"), ("stale", "DELETE_QUOTE")],
        )
        self.assertIsNone(removed)
        self.assertIsNone(backup_path)
        self.assertEqual(self.read("SELECT COUNT(*) FROM quotes"), [(4,)])
        self.assertEqual([name for name in os.listdir(self.tmp.name) if ".bak-" in name], [])

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mod.main([self.db]), 0)
        self.assertIn("Stale PHP crypto assets:", output.getvalue())
        self.assertIn("Stale cross-currency broker quotes:", output.getvalue())
        self.assertIn("DELETE_QUOTE", output.getvalue())

    def test_apply_backups_and_deletes_only_target_quote(self):
        report, removed, backup_path = mod.execute(self.db, "PHP", "USD", apply=True)
        self.assertEqual(removed["cross_currency_quotes"], 1)
        self.assertTrue(backup_path and os.path.exists(backup_path))
        self.assertEqual(
            self.read("SELECT id FROM quotes ORDER BY id"),
            [("manual",), ("noted",), ("usd",)],
        )
        self.assertEqual(self.read("SELECT id FROM assets"), [("sol-usd",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM activities"), [(4,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM snapshot_positions"), [(1,)])
        self.assertEqual(self.read("PRAGMA foreign_key_check"), [])

        backup = sqlite3.connect(backup_path)
        try:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 4)
        finally:
            backup.close()

        report2, removed2, backup2 = mod.execute(self.db, "PHP", "USD", apply=True)
        self.assertEqual(
            [(d["id"], d["action"]) for d in report2["cross_currency_quotes"]],
            [("noted", "SKIP")],
        )
        self.assertIsNone(removed2)
        self.assertIsNone(backup2)

    def test_quote_foreign_key_failure_rolls_back_the_transaction(self):
        conn = sqlite3.connect(self.db)
        conn.isolation_level = None
        conn.executescript(
            "CREATE TABLE quote_guard (id TEXT PRIMARY KEY, quote_id TEXT "
            "REFERENCES quotes(id)); INSERT INTO quote_guard VALUES ('g1', 'stale');"
        )
        conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            mod.execute(self.db, "PHP", "USD", apply=True)

        self.assertEqual(self.read("SELECT id FROM quotes ORDER BY id"),
                         [("manual",), ("noted",), ("stale",), ("usd",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM activities"), [(4,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM snapshot_positions"), [(1,)])


if __name__ == "__main__":
    unittest.main()
