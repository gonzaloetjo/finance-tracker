"""Tests for merchant normalization + persistent alias clustering."""

from __future__ import annotations

from finance.analysis.merchants import normalize_merchant, normalize_text
from finance.db.store import connect, init_schema


def test_normalize_text_basics():
    assert normalize_text("FRANPRIX 5063 PARIS 19") == "FRANPRIX"
    assert normalize_text("UBER * EATS P") == "UBER EATS"
    assert normalize_text("PAYPAL *AIRBNB LUXEMBOURG") == "AIRBNB"
    assert normalize_text("CANAL PLUS FR ISSY LES MOUL") == "CANAL PLUS"
    assert normalize_text("DELIVEROO") == "DELIVEROO"
    assert normalize_text("VOI FR") == "VOI"
    assert normalize_text("ZOOPLUS FR 4414") == "ZOOPLUS"
    assert normalize_text("APPLE.COM/BILL") == "APPLE.COM/BILL"


def test_normalize_text_diacritics():
    assert normalize_text("Café René") == "CAFE RENE"


def test_normalize_text_trailing_punctuation():
    assert normalize_text("NOTION LABS,") == "NOTION LABS"
    assert normalize_text("RENELACHANCE.") == "RENELACHANCE"
    assert normalize_text("CAFE DU COIN,") == "CAFE DU COIN"
    # Must not eat infix punctuation
    assert normalize_text("APPLE.COM/BILL") == "APPLE.COM/BILL"


def test_normalize_text_numeric_ref_suffix():
    assert normalize_text("AMZ DIGITAL FRA PAYLI2125856/") == "AMZ DIGITAL"
    assert normalize_text("SHOP FRA REF1234567890") == "SHOP"
    # Short digit runs (< 4) are not references — stay as store numbers
    assert normalize_text("FRANPRIX 5063 PARIS") == "FRANPRIX"


def test_normalize_text_sa_duplication():
    assert normalize_text("ORANGE SA-ORANGE") == "ORANGE"
    assert normalize_text("BOUYGUES SA-BOUYGUES") == "BOUYGUES"
    # Non-duplicate SA- prefix is untouched
    assert normalize_text("AXA FRANCE") == "AXA FRANCE"


def test_normalize_merchant_creates_new(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid = normalize_merchant("DELIVEROO", conn)
        conn.commit()
        assert mid is not None
        row = conn.execute(
            "SELECT canonical_name FROM merchants WHERE merchant_id=?", (mid,)
        ).fetchone()
        assert row[0] == "DELIVEROO"
        alias = conn.execute(
            "SELECT alias FROM merchant_aliases WHERE merchant_id=?", (mid,)
        ).fetchone()
        assert alias[0] == "DELIVEROO"


def test_normalize_merchant_alias_lookup_idempotent(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid1 = normalize_merchant("FRANPRIX 5063 PARIS 19", conn)
        conn.commit()
        mid2 = normalize_merchant("FRANPRIX 5063 PARIS 19", conn)
        conn.commit()
        assert mid1 == mid2
        count = conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0]
        assert count == 1


def test_normalize_merchant_fuzzy_clusters_aliases(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid1 = normalize_merchant("DELIVEROO", conn)
        conn.commit()
        # Slightly different raw string should cluster to same merchant
        mid2 = normalize_merchant("DELIVEROO FRA", conn)
        conn.commit()
        assert mid1 == mid2
        aliases = conn.execute(
            "SELECT alias FROM merchant_aliases WHERE merchant_id=?", (mid1,)
        ).fetchall()
        assert len(aliases) == 2


def test_corpus_growth_stability(tmp_path):
    """Canonical names assigned to corpus A must remain stable when B ⊃ A is added."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        # Phase 1: create merchants for corpus A
        mid_carrefour = normalize_merchant("CARREFOUR CITY 0234", conn)
        mid_deliveroo = normalize_merchant("DELIVEROO", conn)
        conn.commit()

        canon_a_carrefour = conn.execute(
            "SELECT canonical_name FROM merchants WHERE merchant_id=?", (mid_carrefour,)
        ).fetchone()[0]
        canon_a_deliveroo = conn.execute(
            "SELECT canonical_name FROM merchants WHERE merchant_id=?", (mid_deliveroo,)
        ).fetchone()[0]

        # Phase 2: add more merchants (corpus B ⊃ A)
        normalize_merchant("UBER EATS", conn)
        normalize_merchant("NETFLIX", conn)
        normalize_merchant("APPLE.COM/BILL", conn)
        conn.commit()

        # Canonical names for A merchants must be unchanged
        canon_b_carrefour = conn.execute(
            "SELECT canonical_name FROM merchants WHERE merchant_id=?", (mid_carrefour,)
        ).fetchone()[0]
        canon_b_deliveroo = conn.execute(
            "SELECT canonical_name FROM merchants WHERE merchant_id=?", (mid_deliveroo,)
        ).fetchone()[0]
        assert canon_a_carrefour == canon_b_carrefour
        assert canon_a_deliveroo == canon_b_deliveroo


def test_normalize_merchant_distinct_merchants_not_merged(tmp_path):
    """CARREFOUR and CANAL PLUS must not fuzzy-merge despite both starting with CA."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid1 = normalize_merchant("CARREFOUR", conn)
        mid2 = normalize_merchant("CANAL PLUS", conn)
        conn.commit()
        assert mid1 != mid2
