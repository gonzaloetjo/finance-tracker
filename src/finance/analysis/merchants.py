"""Stage B — merchant normalization + persistent alias clustering.

Canonical names are pinned once and never mutate. New raw strings are matched
against existing canonicals via alias lookup (exact) then rapidfuzz (fuzzy).
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import UTC, datetime

from rapidfuzz import fuzz, process

_FUZZ_THRESHOLD = 85

# Tokens to strip during normalization (trailing city names, country codes, POS numbers)
_STRIP_TRAILING_RE = re.compile(
    r"\b(?:"
    # common French cities — only strip when at the end of the string
    r"PARIS(?:\s+\d+)?|LYON|MARSEILLE|TOULOUSE|NICE|NANTES|STRASBOURG|BORDEAUX|LILLE|RENNES"
    r"|MONTPELLIER|ISSY\s+LES\s+MOUL(?:INEAUX)?"
    # full country / city names that French banks append after merchant tokens
    r"|LUXEMBOURG|AMSTERDAM|LONDON|DUBLIN"
    # 2–3 letter country codes at end (FRA, DEU, NLD, IRL, etc.)
    r"|[A-Z]{2,3}"
    r")\s*$"
)

# Lone 3-5 digit store/branch numbers (e.g. "FRANPRIX 5063", "ZOOPLUS FR 4414")
_STORE_NUMBER_RE = re.compile(r"\s+\d{3,5}\b")

# PAYPAL * prefix → use the underlying merchant
_PAYPAL_RE = re.compile(r"^PAYPAL\s*\*\s*")

# "UBER * EATS P" → "UBER EATS"
_STAR_SPACE_RE = re.compile(r"\s*\*\s*")

# Trailing punctuation (comma, dot, slash, colon, dash, asterisk)
_TRAILING_PUNCT_RE = re.compile(r"[,./:\-*]+\s*$")

# Trailing reference token with ≥4 digits (e.g. " PAYLI2125856", " REF1234567890")
# Must have leading whitespace so we don't eat the whole name if it's numeric.
_NUMERIC_SUFFIX_RE = re.compile(r"\s+[A-Z]*\d{4,}\S*$")

# "<NAME> SA-<NAME>" → "<NAME>" (French corporate duplication: ORANGE SA-ORANGE)
_SA_DUPLICATE_RE = re.compile(r"^(.+?)\s+SA-\1$")


def _strip_diacritics(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _normalize_text(raw: str) -> str:
    """Turn a raw merchant string into a clean, comparable form."""
    s = raw.upper()
    s = _strip_diacritics(s)

    # Handle PAYPAL * → extract underlying merchant
    s = _PAYPAL_RE.sub("", s)

    # Collapse "UBER * EATS P" → "UBER EATS P"
    s = _STAR_SPACE_RE.sub(" ", s)

    # Strip trailing single letter (like "P" in "UBER EATS P")
    s = re.sub(r"\s+[A-Z]\s*$", "", s)

    # Strip store/branch numbers
    s = _STORE_NUMBER_RE.sub("", s)

    # Collapse "X SA-X" duplication before trailing-strip passes
    s = _SA_DUPLICATE_RE.sub(r"\1", s).strip()

    # Iterate: trailing punctuation → numeric ref suffix → city/country codes.
    # Stripping any one can expose the next (e.g. "NAME FRA PAYLI123/" → NAME).
    for _ in range(6):
        prev = s
        s = _TRAILING_PUNCT_RE.sub("", s).strip()
        s = _NUMERIC_SUFFIX_RE.sub("", s).strip()
        candidate = _STRIP_TRAILING_RE.sub("", s).strip()
        if candidate:
            s = candidate
        if s == prev or not s:
            break

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    return s


def normalize_merchant(
    raw: str,
    conn: sqlite3.Connection,
) -> int:
    """Resolve a raw merchant string to a persistent merchant_id.

    Steps:
    1. Exact alias lookup in merchant_aliases.
    2. Normalize text, then exact match on merchants.canonical_name.
    3. Fuzzy match against all existing canonical names (rapidfuzz > 85%).
    4. If no match, create new merchant + alias.

    Returns merchant_id. Always writes at least an alias entry (idempotent via
    INSERT OR IGNORE).
    """
    now = datetime.now(UTC).isoformat()

    # 1. Exact alias lookup
    row = conn.execute(
        "SELECT merchant_id FROM merchant_aliases WHERE alias = ?", (raw,)
    ).fetchone()
    if row:
        return row[0]

    normalized = _normalize_text(raw)
    if not normalized:
        normalized = raw.upper().strip()

    # 2. Exact canonical match
    row = conn.execute(
        "SELECT merchant_id FROM merchants WHERE canonical_name = ?", (normalized,)
    ).fetchone()
    if row:
        mid = row[0]
        _upsert_alias(conn, raw, mid, now)
        return mid

    # 3. Fuzzy match
    all_canonicals = conn.execute("SELECT merchant_id, canonical_name FROM merchants").fetchall()
    if all_canonicals:
        names = [r[1] for r in all_canonicals]
        ids = [r[0] for r in all_canonicals]
        match = process.extractOne(
            normalized, names, scorer=fuzz.token_sort_ratio, score_cutoff=_FUZZ_THRESHOLD
        )
        if match:
            mid = ids[names.index(match[0])]
            _upsert_alias(conn, raw, mid, now)
            _update_merchant_last_seen(conn, mid, now)
            return mid

    # 4. New merchant
    cur = conn.execute(
        "INSERT INTO merchants (canonical_name, first_seen, last_seen, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (normalized, now, now, now),
    )
    mid = cur.lastrowid
    assert mid is not None
    _upsert_alias(conn, raw, mid, now)
    return mid


def _upsert_alias(conn: sqlite3.Connection, alias: str, merchant_id: int, now: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO merchant_aliases (alias, merchant_id, created_at) VALUES (?, ?, ?)",
        (alias, merchant_id, now),
    )


def _update_merchant_last_seen(conn: sqlite3.Connection, merchant_id: int, now: str) -> None:
    conn.execute(
        "UPDATE merchants SET last_seen = ?, updated_at = ? WHERE merchant_id = ?",
        (now, now, merchant_id),
    )


def normalize_text(raw: str) -> str:
    """Public accessor for testing."""
    return _normalize_text(raw)


def resolve_merchant_id(conn: sqlite3.Connection, canonical_or_alias: str) -> int | None:
    """Accept either a canonical name or a raw alias string, return merchant_id."""
    row = conn.execute(
        "SELECT merchant_id FROM merchants WHERE canonical_name = ?", (canonical_or_alias,)
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT merchant_id FROM merchant_aliases WHERE alias = ?", (canonical_or_alias,)
    ).fetchone()
    if row:
        return row[0]
    return None


def top_merchants(
    conn: sqlite3.Connection,
    *,
    limit: int = 30,
    spend_only: bool = False,
    since: str | None = None,
    uncategorized_only: bool = False,
):
    """Ranked merchant table by outflow. Returns a pandas DataFrame.

    Columns: merchant_id, merchant, category, category_source, txns,
             total_spend, first_seen, last_seen, aliases_count.

    - `spend_only=True` drops transactions from accounts flagged
      `excluded_from_spend` (joint savings, investment, etc.).
    - `since` filters on booking_date.
    - `uncategorized_only=True` restricts to merchants where
      `category_source` is NULL or not in {user, curated} — the same
      population `merchant seed-top` / LLM categorization targets.
    """
    import pandas as pd

    query = """
        SELECT
          m.merchant_id                              AS merchant_id,
          m.canonical_name                           AS merchant,
          m.category                                 AS category,
          m.category_source                          AS category_source,
          COUNT(t.transaction_id)                    AS txns,
          COALESCE(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 0) AS total_spend,
          COALESCE(SUM(CASE WHEN t.amount > 0 THEN  t.amount ELSE 0 END), 0) AS total_income,
          COALESCE(SUM(t.amount), 0)                 AS net_amount,
          MIN(t.booking_date)                        AS first_seen,
          MAX(t.booking_date)                        AS last_seen,
          (SELECT COUNT(*) FROM merchant_aliases ma
             WHERE ma.merchant_id = m.merchant_id)   AS aliases_count
        FROM merchants m
        LEFT JOIN tx_enrichment e ON e.merchant_id = m.merchant_id
        LEFT JOIN transactions t  ON t.transaction_id = e.tx_id
        LEFT JOIN accounts a      ON a.account_uid = t.account_uid
        WHERE (t.currency = 'EUR' OR t.currency IS NULL)
    """
    params: list = []
    if since is not None:
        query += " AND t.booking_date >= ?"
        params.append(since)
    if spend_only:
        from finance.analysis.io import NON_SPEND_CATEGORIES

        query += " AND COALESCE(a.excluded_from_spend, 0) = 0"
        ph = ",".join("?" for _ in NON_SPEND_CATEGORIES)
        query += f" AND (m.category IS NULL OR m.category NOT IN ({ph}))"
        params.extend(sorted(NON_SPEND_CATEGORIES))
    if uncategorized_only:
        # "Uncategorized" means strictly no category. Rule/LLM/rule-stream/
        # curated/user are all "categorized" — even if the user might disagree
        # with a rule, it's not *missing*. Reviewing rule/llm tags is a
        # separate action (`finance merchant review --include-rule/--include-llm`
        # or click directly on the category badge in any merchant row).
        query += " AND m.category IS NULL"
    # Order by max absolute flow direction so income-only merchants surface
    # too (they'd have total_spend=0 and sink to the bottom otherwise).
    query += """
        GROUP BY m.merchant_id
        HAVING txns > 0
        ORDER BY MAX(total_spend, total_income) DESC
        LIMIT ?
    """
    params.append(limit)

    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return df
    df["first_seen"] = pd.to_datetime(df["first_seen"], errors="coerce")
    df["last_seen"] = pd.to_datetime(df["last_seen"], errors="coerce")
    return df


def deep_dive(conn: sqlite3.Connection, canonical_or_alias: str):
    """All transactions for one merchant plus a small summary.

    Returns a dict with:
      - merchant: canonical name
      - category, category_source
      - transactions: DataFrame (booking_date, amount, memo_raw, stream_id)
      - total_spend (abs sum of outflows), count, first_seen, last_seen
      - aliases: list of alias strings

    Returns None if the merchant isn't known.
    """
    import pandas as pd

    mid = resolve_merchant_id(conn, canonical_or_alias)
    if mid is None:
        return None

    m = conn.execute(
        "SELECT canonical_name, category, category_source, first_seen, last_seen"
        " FROM merchants WHERE merchant_id = ?",
        (mid,),
    ).fetchone()

    aliases = [
        r[0]
        for r in conn.execute(
            "SELECT alias FROM merchant_aliases WHERE merchant_id = ? ORDER BY alias", (mid,)
        ).fetchall()
    ]

    txns = pd.read_sql_query(
        """
        SELECT
          t.transaction_id   AS tx_id,
          t.booking_date     AS booking_date,
          t.amount           AS amount,
          t.currency         AS currency,
          t.remittance_info  AS memo_raw,
          e.stream_id        AS stream_id
        FROM transactions t
        JOIN tx_enrichment e ON e.tx_id = t.transaction_id
        WHERE e.merchant_id = ?
        ORDER BY t.booking_date DESC
        """,
        conn,
        params=[mid],
    )
    if not txns.empty:
        txns["booking_date"] = pd.to_datetime(txns["booking_date"], errors="coerce")

    total_spend = float(-txns.loc[txns["amount"] < 0, "amount"].sum()) if not txns.empty else 0.0

    return {
        "merchant_id": mid,
        "merchant": m[0] if m else canonical_or_alias,
        "category": m[1] if m else None,
        "category_source": m[2] if m else None,
        "first_seen": m[3] if m else None,
        "last_seen": m[4] if m else None,
        "transactions": txns,
        "total_spend": total_spend,
        "count": len(txns),
        "aliases": aliases,
    }


def rename_canonical(conn: sqlite3.Connection, old: str, new: str) -> bool:
    """Rename a merchant's canonical_name. Aliases survive. Returns True on success."""
    new_name = new.strip().upper()
    if not new_name:
        return False
    row = conn.execute(
        "SELECT merchant_id FROM merchants WHERE canonical_name = ?", (old,)
    ).fetchone()
    if not row:
        return False
    # Target collision?
    clash = conn.execute(
        "SELECT merchant_id FROM merchants WHERE canonical_name = ? AND merchant_id != ?",
        (new_name, row[0]),
    ).fetchone()
    if clash:
        return False
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE merchants SET canonical_name = ?, updated_at = ? WHERE merchant_id = ?",
        (new_name, now, row[0]),
    )
    conn.commit()
    return True


def merge_merchants(conn: sqlite3.Connection, src: str, dst: str) -> bool:
    """Merge merchant `src` into `dst`: re-point aliases, tx_enrichment, streams,
    then delete the source merchant row. Returns True on success."""
    src_row = conn.execute(
        "SELECT merchant_id FROM merchants WHERE canonical_name = ?", (src,)
    ).fetchone()
    dst_row = conn.execute(
        "SELECT merchant_id FROM merchants WHERE canonical_name = ?", (dst,)
    ).fetchone()
    if not src_row or not dst_row or src_row[0] == dst_row[0]:
        return False

    src_id, dst_id = src_row[0], dst_row[0]
    now = datetime.now(UTC).isoformat()

    conn.execute(
        "UPDATE merchant_aliases SET merchant_id = ? WHERE merchant_id = ?", (dst_id, src_id)
    )
    conn.execute(
        "UPDATE tx_enrichment    SET merchant_id = ? WHERE merchant_id = ?", (dst_id, src_id)
    )
    conn.execute(
        "UPDATE streams          SET merchant_id = ? WHERE merchant_id = ?", (dst_id, src_id)
    )
    conn.execute("DELETE FROM merchants WHERE merchant_id = ?", (src_id,))
    conn.execute("UPDATE merchants SET updated_at = ? WHERE merchant_id = ?", (now, dst_id))
    conn.commit()
    return True


def load_curated_merges() -> dict[str, str]:
    """Read the bundled merchant_merges.yaml → dict[src_canonical, dst_canonical]."""
    from importlib.resources import files

    import yaml

    path = files("finance.data").joinpath("merchant_merges.yaml")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}


def apply_curated_merges(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
) -> list[tuple[str, str, str]]:
    """Apply every src→dst pair from merchant_merges.yaml.

    Semantics per pair:
      - both exist  → merge src into dst
      - src only    → rename src to dst
      - dst only    → no-op (already canonical)
      - neither     → no-op (stale entry)

    Returns (src, dst, action) where action ∈ {merged, renamed, skipped}.
    """
    pairs = load_curated_merges()
    results: list[tuple[str, str, str]] = []
    for src, dst in pairs.items():
        src_exists = (
            conn.execute("SELECT 1 FROM merchants WHERE canonical_name = ?", (src,)).fetchone()
            is not None
        )
        dst_exists = (
            conn.execute("SELECT 1 FROM merchants WHERE canonical_name = ?", (dst,)).fetchone()
            is not None
        )

        if src_exists and dst_exists:
            action = "merged" if (dry_run or merge_merchants(conn, src, dst)) else "skipped"
        elif src_exists and not dst_exists:
            action = "renamed" if (dry_run or rename_canonical(conn, src, dst)) else "skipped"
        else:
            action = "skipped"
        results.append((src, dst, action))
    return results


def set_category(conn: sqlite3.Connection, canonical_or_alias: str, category: str) -> bool:
    """Set a merchant-level category with source='user' (never overwritten by re-enrich)."""
    mid = resolve_merchant_id(conn, canonical_or_alias)
    if mid is None:
        return False
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE merchants SET category = ?, category_source = 'user', updated_at = ?"
        " WHERE merchant_id = ?",
        (category, now, mid),
    )
    conn.commit()
    return True
