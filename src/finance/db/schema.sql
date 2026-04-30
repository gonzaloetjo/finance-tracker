CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  aspsp_name TEXT NOT NULL,
  aspsp_country TEXT NOT NULL,
  valid_until TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
  account_uid TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  iban TEXT,
  name TEXT,
  currency TEXT,
  account_type TEXT,
  raw_json TEXT NOT NULL,
  -- When 1, analyses with --spend-only filter this account out. Off by default;
  -- flip via `finance accounts exclude <uid>`. Useful for joint / savings /
  -- investment accounts you don't want mixed into spending totals.
  excluded_from_spend INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS balances (
  account_uid TEXT NOT NULL REFERENCES accounts(account_uid),
  balance_type TEXT NOT NULL,
  amount REAL NOT NULL,
  currency TEXT NOT NULL,
  reference_date TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (account_uid, balance_type, reference_date)
);

CREATE TABLE IF NOT EXISTS transactions (
  transaction_id TEXT PRIMARY KEY,
  account_uid TEXT NOT NULL REFERENCES accounts(account_uid),
  booking_date TEXT,
  value_date TEXT,
  amount REAL NOT NULL,
  currency TEXT NOT NULL,
  creditor_name TEXT,
  debtor_name TEXT,
  remittance_info TEXT,
  raw_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_account_date
  ON transactions(account_uid, booking_date DESC);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_uid TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  transactions_added INTEGER,
  status TEXT NOT NULL,
  error TEXT
);

-- Phase 6 — enrichment layer. Persistent merchant identity + category + stream membership.
CREATE TABLE IF NOT EXISTS merchants (
  merchant_id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_name TEXT NOT NULL UNIQUE,
  display_name TEXT,
  category TEXT,
  category_source TEXT,           -- 'user' | 'curated' | 'rule' | 'llm' | NULL
  category_confidence REAL,        -- 0..1, meaningful for 'llm'
  notes TEXT,
  first_seen TEXT,
  last_seen TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS merchant_aliases (
  alias TEXT PRIMARY KEY,
  merchant_id INTEGER NOT NULL REFERENCES merchants(merchant_id),
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS streams (
  stream_id TEXT PRIMARY KEY,     -- IMMUTABLE: sha1(merchant_id + flat ±15% band bucket)[:16]
  merchant_id INTEGER NOT NULL REFERENCES merchants(merchant_id),
  txn_type TEXT,
  median_amount REAL,
  amount_tolerance REAL,           -- classification-driven, descriptive only
  median_days INTEGER,
  regularity REAL,
  classification TEXT,             -- weekly|monthly|quarterly|annual|irregular
  is_recurring INTEGER NOT NULL DEFAULT 0,
  is_subscription INTEGER NOT NULL DEFAULT 0,
  subscription_override INTEGER,          -- NULL=auto, 1=force sub, 0=force not-sub
  active INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT,
  last_seen TEXT,
  count INTEGER,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS tx_enrichment (
  tx_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id) ON DELETE CASCADE,
  txn_type TEXT,                   -- FACTURE|PRLV|VIR|VIREMENT|FRAIS|RETRAIT|INTERETS|OTHER
  merchant_id INTEGER REFERENCES merchants(merchant_id),
  stream_id TEXT REFERENCES streams(stream_id),
  memo_merchant_raw TEXT,          -- parsed merchant string before normalization
  enriched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tx_overrides (
  tx_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id) ON DELETE CASCADE,
  category TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_enrichment_merchant ON tx_enrichment(merchant_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_stream ON tx_enrichment(stream_id);
CREATE INDEX IF NOT EXISTS idx_merchant_aliases_mid ON merchant_aliases(merchant_id);

-- Phase 7 — LLM advisory cache.
CREATE TABLE IF NOT EXISTS advice (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,              -- 'subscription_overlap' | 'cutback' | 'integral_offer'
  generated_at TEXT NOT NULL,
  model TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  dismissed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_advice_kind_hash ON advice(kind, input_hash);

-- LLM proposals below the auto-write threshold — one per merchant, overwritten
-- on subsequent runs. Surfaced on the Uncategorized page with Accept/Ignore.
CREATE TABLE IF NOT EXISTS llm_proposals (
  merchant_id INTEGER PRIMARY KEY REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  category TEXT NOT NULL,
  confidence REAL NOT NULL,
  reasoning TEXT,
  model TEXT,
  generated_at TEXT NOT NULL
);

-- Phase 7 — LLM call observability (cost + cache-hit tracking).
CREATE TABLE IF NOT EXISTS llm_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,              -- 'categorize' | 'advise_subscriptions' | ...
  model TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  status TEXT NOT NULL,            -- ok | error
  error TEXT
);
