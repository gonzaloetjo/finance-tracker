"""Phase 8 web dashboard router.

Read routes compose Stage C functions and render DataFrames via
`_table.html`. Write routes are HTMX-driven and return a single swapped
fragment. The DB path is pulled from `app.state.finance`, so this module
doesn't need its own `AppState` — it piggybacks on the one configured in
`web.app.create_app()`.
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from finance.analysis.alerts import new_large_merchants, subscription_stopped
from finance.analysis.forecast import next_expected_charges
from finance.analysis.merchants import (
    deep_dive,
    set_category,
    top_merchants,
)
from finance.analysis.overview import build_overview
from finance.analysis.recurring import find_recurring
from finance.analysis.subscriptions import find_overlaps, find_subscriptions
from finance.db import store
from finance.llm.advise import dismiss_advice, list_advice
from finance.llm.categorize import load_taxonomy

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    """The Jinja2Templates instance is attached by create_app."""
    return request.app.state.finance_templates


def _db(request: Request):
    """Open a SQLite connection using the db_path from AppState."""
    state = request.app.state.finance
    conn = store.connect(state.db_path)
    store.init_schema(conn)
    return conn


def _df_to_rows(df: pd.DataFrame | None) -> list[dict]:
    """DataFrame → list[dict], with NaN → None and Timestamps → 'YYYY-MM-DD'."""
    if df is None or df.empty:
        return []
    # Normalize datetime columns to ISO dates (display-only).
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d")
    return df.where(df.notna(), None).to_dict(orient="records")


# ═════════════════════════════════════════════════════════════════════════════
# GET / — overview
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/", response_class=HTMLResponse)
def overview_page(request: Request, spend_only: bool = True, months: int = 3):
    with _db(request) as conn:
        data = build_overview(
            conn,
            months=months,
            top_n=15,
            forecast_days=30,
            spend_only=spend_only,
            threshold=500.0,
        )
        mtd = store.month_to_date_totals(conn)
        monthly = store.monthly_series(conn, months=6)
    return _templates(request).TemplateResponse(
        request,
        "overview.html",
        {
            "data": data,
            "spend_only": spend_only,
            "months": months,
            "mtd": mtd,
            "monthly": monthly,
            "top_merchants_rows": _df_to_rows(data.top_merchants),
            "trends_rows": _df_to_rows(data.trends),
            "recurring_rows": _df_to_rows(data.recurring),
            "subs_rows": _df_to_rows(data.subscriptions),
            "overlaps_rows": _df_to_rows(data.overlaps),
            "forecast_rows": _df_to_rows(data.forecast),
            "alerts_rows": _df_to_rows(data.new_large),
            "stopped_rows": _df_to_rows(data.stopped),
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# GET /merchants — top-N list
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/merchants", response_class=HTMLResponse)
def merchants_page(
    request: Request,
    top: int = 50,
    uncategorized: bool = False,
    spend_only: bool = False,
    since: str | None = None,
):
    with _db(request) as conn:
        df = top_merchants(
            conn,
            limit=top,
            spend_only=spend_only,
            since=since,
            uncategorized_only=uncategorized,
        )
    rows = _df_to_rows(df)
    taxonomy = load_taxonomy()
    summary = {
        "count": int(len(df)),
        "total_spend": float(df["total_spend"].sum()) if not df.empty else 0.0,
        "total_income": float(df["total_income"].sum()) if not df.empty else 0.0,
        "total_txns": int(df["txns"].sum()) if not df.empty else 0,
    }

    # When reviewing uncategorized merchants, also fetch any persisted LLM
    # proposals (below auto-write threshold) so the user can Accept / Ignore.
    proposals: list[dict] = []
    if uncategorized:
        with _db(request) as conn:
            p_rows = conn.execute(
                """
                SELECT p.merchant_id, m.canonical_name AS merchant, p.category,
                       p.confidence, p.reasoning, p.model, p.generated_at,
                       COALESCE(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 0) AS total_spend
                FROM llm_proposals p
                JOIN merchants m ON m.merchant_id = p.merchant_id
                LEFT JOIN tx_enrichment e ON e.merchant_id = p.merchant_id
                LEFT JOIN transactions t ON t.transaction_id = e.tx_id
                WHERE m.category IS NULL
                GROUP BY p.merchant_id
                ORDER BY p.confidence DESC, total_spend DESC
                """
            ).fetchall()
        proposals = [dict(r) for r in p_rows]

    # When reviewing uncategorized merchants, fetch up to 3 example memos per
    # row so the user has enough context to categorize in-place.
    memos_by_mid: dict[int, list[str]] = {}
    if uncategorized and rows:
        mids = [int(r["merchant_id"]) for r in rows]
        ph = ",".join("?" for _ in mids)
        with _db(request) as conn:
            memo_rows = conn.execute(
                f"""
                SELECT e.merchant_id, t.remittance_info, t.booking_date
                FROM tx_enrichment e
                JOIN transactions t ON t.transaction_id = e.tx_id
                WHERE e.merchant_id IN ({ph})
                ORDER BY e.merchant_id, t.booking_date DESC
                """,
                mids,
            ).fetchall()
        for mr in memo_rows:
            mid = int(mr["merchant_id"])
            if len(memos_by_mid.get(mid, [])) < 3 and mr["remittance_info"]:
                memos_by_mid.setdefault(mid, []).append(mr["remittance_info"])

    from finance.llm.categorize import AUTO_WRITE_THRESHOLD

    return _templates(request).TemplateResponse(
        request,
        "merchants.html",
        {
            "rows": rows,
            "taxonomy": taxonomy,
            "summary": summary,
            "memos_by_mid": memos_by_mid,
            "proposals": proposals,
            "auto_write_threshold": AUTO_WRITE_THRESHOLD,
            "filters": {
                "top": top,
                "uncategorized": uncategorized,
                "spend_only": spend_only,
                "since": since or "",
            },
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# GET /merchants/{canonical} — deep-dive
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/merchants/{canonical}", response_class=HTMLResponse)
def merchant_detail_page(request: Request, canonical: str):
    with _db(request) as conn:
        dd = deep_dive(conn, canonical)
    if dd is None:
        raise HTTPException(404, f"Merchant '{canonical}' not found")
    dd["tx_rows"] = _df_to_rows(dd["transactions"])
    return _templates(request).TemplateResponse(
        request,
        "merchant_detail.html",
        {"m": dd, "taxonomy": load_taxonomy()},
    )


# ═════════════════════════════════════════════════════════════════════════════
# POST /merchants/{merchant_id}/category — inline category edit (HTMX)
# ═════════════════════════════════════════════════════════════════════════════


@router.post("/merchants/llm-categorize", response_class=HTMLResponse)
def merchants_llm_categorize(request: Request, provider: str = "api"):
    """Run the LLM categorizer on currently-uncategorized merchants.

    `provider=api`       → Anthropic API (pay-per-token, fast, default).
    `provider=claude-cli` → local Claude Code CLI subprocess (uses subscription,
                            includes WebSearch for merchant disambiguation).
    """
    from finance.llm.categorize import categorize_uncategorized
    from finance.llm.client import redact_key, resolve_api_key
    from finance.llm.providers import ClaudeCLIError, make_provider

    if provider == "api" and not resolve_api_key():
        return HTMLResponse(
            '<div class="bg-red-50 text-red-800 border border-red-200 rounded px-4 py-3">'
            '<div class="font-semibold">No Anthropic API key</div>'
            '<div class="text-sm mt-1">Set one via Settings page or switch to '
            "the Claude Code provider button.</div></div>"
        )

    try:
        llm = make_provider(provider)
    except ClaudeCLIError as e:
        return HTMLResponse(
            f'<div class="bg-red-50 text-red-800 border border-red-200 rounded px-4 py-3">'
            f'<div class="font-semibold">Claude Code provider unavailable</div>'
            f'<div class="text-sm mt-1">{str(e)[:240]}</div></div>'
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        with _db(request) as conn:
            summary = categorize_uncategorized(conn, client=llm, limit=150)
    except Exception as e:
        return HTMLResponse(
            f'<div class="bg-red-50 text-red-800 border border-red-200 rounded px-4 py-3">'
            f'<div class="font-semibold">LLM call failed</div>'
            f'<div class="text-sm mt-1">{redact_key(str(e))[:240]}</div>'  # type: ignore[index]
            f"</div>"
        )

    return _templates(request).TemplateResponse(
        request,
        "_llm_categorize_result.html",
        {"summary": summary, "provider": provider},
    )


@router.get("/llm/progress", response_class=HTMLResponse)
def llm_progress(request: Request):
    """Poll-endpoint — returns the latest llm_runs state as a small HTML fragment.

    The uncategorized page polls this every 2 s while the LLM button's
    spinner is showing. Safe to GET even when nothing's running — returns
    a clean 'idle' fragment.
    """
    with _db(request) as conn:
        running = conn.execute(
            """
            SELECT id, started_at, error AS label
            FROM llm_runs
            WHERE status = 'running'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        # Most-recent completed run in the last 10 min (so user can see
        # "batch N/M finished" even while the next batch is in flight).
        recent_ok = conn.execute(
            """
            SELECT error AS label, ended_at
            FROM llm_runs
            WHERE status IN ('ok', 'error')
              AND started_at >= datetime('now', '-10 minutes')
            ORDER BY id DESC
            LIMIT 3
            """
        ).fetchall()

    if running:
        label = running["label"] or "running…"
        return HTMLResponse(
            f'<div class="text-xs text-violet-900">'
            f'<span class="spinner mr-2"></span>'
            f"<strong>Working:</strong> {label}"
            f' <span class="muted">· started {running["started_at"][11:19]} UTC</span>'
            f"</div>"
            + "".join(
                f'<div class="text-xs muted mt-0.5">✓ {r["label"] or ""}</div>' for r in recent_ok
            )
        )
    # Nothing running.
    if recent_ok:
        return HTMLResponse(
            '<div class="text-xs text-emerald-800">'
            "<strong>✓ Done.</strong> Last batches: "
            + ", ".join(r["label"] or "" for r in recent_ok)
            + "</div>"
        )
    return HTMLResponse('<div class="text-xs muted">idle</div>')


@router.post("/merchants/accept-all-proposals", response_class=HTMLResponse)
def merchants_accept_all_proposals(request: Request):
    """Apply every persisted llm_proposal as source='user' and clear them.

    Atomic: either every proposal applies (no user-override conflict) or we
    still return how many made it through.
    """
    from finance.analysis.merchants import set_category

    with _db(request) as conn:
        rows = conn.execute(
            """
            SELECT m.merchant_id, m.canonical_name, p.category
            FROM llm_proposals p
            JOIN merchants m ON m.merchant_id = p.merchant_id
            WHERE m.category IS NULL
            """
        ).fetchall()
        applied = 0
        for r in rows:
            if set_category(conn, r["canonical_name"], r["category"]):
                applied += 1
        # Always clear all proposals — accepted ones became categories;
        # failed ones shouldn't linger either.
        conn.execute("DELETE FROM llm_proposals")
        conn.commit()

    return HTMLResponse(
        f'<div class="border border-emerald-200 bg-emerald-50 rounded px-4 py-3">'
        f'<div class="flex items-baseline justify-between">'
        f'<div><span class="font-semibold text-emerald-900">✓ Applied {applied} proposal(s)</span></div>'
        f'<button type="button" onclick="window.location.reload()"'
        f' class="text-xs bg-emerald-900 text-white px-3 py-1 rounded hover:bg-emerald-700">'
        f"Refresh page</button></div>"
        f'<div class="text-xs muted mt-1">Auto-refreshing in <span id="accept-all-countdown">2</span>s.</div>'
        f'<script>(function(){{let n=2;const el=document.getElementById("accept-all-countdown");'
        f"const t=setInterval(()=>{{n-=1;if(el)el.textContent=n;if(n<=0){{clearInterval(t);window.location.reload();}}}},1000);}})();</script>"
        f"</div>"
    )


@router.post("/merchants/{merchant_id}/accept-proposal", response_class=HTMLResponse)
def merchants_accept_proposal(request: Request, merchant_id: int):
    """Accept a low-confidence LLM proposal for this merchant.

    Applies the stored category with source='user' (user approved), deletes
    the proposal row, and returns a 200 with empty body so the HTMX row can
    be swapped out / deleted.
    """
    from finance.analysis.merchants import set_category

    with _db(request) as conn:
        row = conn.execute(
            """
            SELECT m.canonical_name, p.category
            FROM llm_proposals p
            JOIN merchants m ON m.merchant_id = p.merchant_id
            WHERE p.merchant_id = ?
            """,
            (merchant_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no proposal for this merchant")
        ok = set_category(conn, row["canonical_name"], row["category"])
        if not ok:
            raise HTTPException(500, "failed to apply category")
        conn.execute("DELETE FROM llm_proposals WHERE merchant_id = ?", (merchant_id,))
        conn.commit()
    return Response(status_code=200)


@router.post("/merchants/{merchant_id}/ignore-proposal", response_class=HTMLResponse)
def merchants_ignore_proposal(request: Request, merchant_id: int):
    """Dismiss the proposal for this run. Will be re-surfaced if a future LLM
    run proposes the same category again."""
    with _db(request) as conn:
        cur = conn.execute(
            "DELETE FROM llm_proposals WHERE merchant_id = ?",
            (merchant_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "no proposal for this merchant")
    return Response(status_code=200)


@router.post("/merchants/{merchant_id}/category", response_class=HTMLResponse)
def merchants_set_category(
    request: Request,
    merchant_id: int,
    category: str = Form(default=""),
):
    with _db(request) as conn:
        row = conn.execute(
            "SELECT canonical_name FROM merchants WHERE merchant_id = ?",
            (merchant_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "merchant not found")
        canonical = row[0]
        # Empty submission = clear user override (allow re-LLM). Non-empty = set.
        if category:
            set_category(conn, canonical, category)
        else:
            conn.execute(
                "UPDATE merchants SET category = NULL, category_source = NULL,"
                " category_confidence = NULL WHERE merchant_id = ?",
                (merchant_id,),
            )
            conn.commit()
        # Reload the fresh row so the fragment shows the authoritative state.
        m = conn.execute(
            "SELECT merchant_id, category, category_source FROM merchants WHERE merchant_id = ?",
            (merchant_id,),
        ).fetchone()
    return _templates(request).TemplateResponse(
        request,
        "_category_picker.html",
        {
            "merchant_id": m["merchant_id"],
            "category": m["category"] or "",
            "source": m["category_source"] or "",
            "taxonomy": load_taxonomy(),
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# Stage C drill-down pages
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/recurring", response_class=HTMLResponse)
def recurring_page(request: Request, active_only: bool = True):
    with _db(request) as conn:
        df = find_recurring(conn, active_only=active_only)
    summary = {
        "count": int(len(df)),
        "monthly_total": float(df["monthly_cost"].abs().sum()) if not df.empty else 0.0,
    }
    return _templates(request).TemplateResponse(
        request,
        "recurring.html",
        {"rows": _df_to_rows(df), "active_only": active_only, "summary": summary},
    )


@router.get("/subscriptions", response_class=HTMLResponse)
def subscriptions_page(request: Request):
    from finance.analysis.subscriptions import find_sub_candidates
    from finance.analysis.totals import compute_totals

    with _db(request) as conn:
        subs = find_subscriptions(conn, active_only=True)
        overlaps = find_overlaps(conn)
        candidates = find_sub_candidates(conn)
        totals = compute_totals(conn, months=3, spend_only=True)
    summary = {
        "count": int(len(subs)),
        "monthly_total": float(subs["monthly_cost"].abs().sum()) if not subs.empty else 0.0,
        "overlap_domains": int(len(overlaps)),
        "overlap_monthly": float(overlaps["monthly_cost"].abs().sum())
        if not overlaps.empty
        else 0.0,
        "candidates": int(len(candidates)),
    }
    return _templates(request).TemplateResponse(
        request,
        "subscriptions.html",
        {
            "subs": _df_to_rows(subs),
            "overlaps": _df_to_rows(overlaps),
            "candidates": _df_to_rows(candidates),
            "summary": summary,
            "totals": totals,
        },
    )


@router.post("/streams/{stream_id}/accept-as-sub", response_class=HTMLResponse)
def stream_accept_as_sub(request: Request, stream_id: str):
    """User: 'yes, this IS a subscription' — override the category gate."""
    with _db(request) as conn:
        cur = conn.execute(
            "UPDATE streams SET subscription_override = 1, is_subscription = 1 WHERE stream_id = ?",
            (stream_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "stream not found")
    return Response(status_code=200)


@router.post("/streams/{stream_id}/reject-as-sub", response_class=HTMLResponse)
def stream_reject_as_sub(request: Request, stream_id: str):
    """User: 'no, not a subscription' — lock it out so it doesn't keep showing."""
    with _db(request) as conn:
        cur = conn.execute(
            "UPDATE streams SET subscription_override = 0, is_subscription = 0 WHERE stream_id = ?",
            (stream_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "stream not found")
    return Response(status_code=200)


@router.get("/forecast", response_class=HTMLResponse)
def forecast_page(request: Request, days: int = 30):
    with _db(request) as conn:
        df = next_expected_charges(conn, horizon_days=days)
    summary = {
        "count": int(len(df)),
        "total_outflow": float(df[df["typical_amount"] < 0]["typical_amount"].abs().sum())
        if not df.empty
        else 0.0,
        "total_inflow": float(df[df["typical_amount"] > 0]["typical_amount"].sum())
        if not df.empty
        else 0.0,
    }
    return _templates(request).TemplateResponse(
        request,
        "forecast.html",
        {"rows": _df_to_rows(df), "days": days, "summary": summary},
    )


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, threshold: float = 500.0):
    with _db(request) as conn:
        new_large = new_large_merchants(conn, amount_threshold=threshold)
        stopped = subscription_stopped(conn)
    summary = {
        "new_large_count": int(len(new_large)),
        "new_large_total": float(new_large["amount"].abs().sum()) if not new_large.empty else 0.0,
        "stopped_count": int(len(stopped)),
        "stopped_monthly_saved": float(stopped["estimated_saved"].sum())
        if not stopped.empty
        else 0.0,
    }
    return _templates(request).TemplateResponse(
        request,
        "alerts.html",
        {
            "new_large": _df_to_rows(new_large),
            "stopped": _df_to_rows(stopped),
            "threshold": threshold,
            "summary": summary,
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# GET /advice + POST /advice/{id}/dismiss
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/advice", response_class=HTMLResponse)
def advice_page(request: Request, show_dismissed: bool = False):
    import json

    with _db(request) as conn:
        rows = list_advice(conn, include_dismissed=show_dismissed)
        # Pull payload_json for each shown row — list_advice only returns metadata.
        full = []
        for r in rows:
            payload_row = conn.execute(
                "SELECT payload_json FROM advice WHERE id = ?", (r["id"],)
            ).fetchone()
            full.append(
                {
                    **r,
                    "payload": json.loads(payload_row[0]) if payload_row else {},
                }
            )
    return _templates(request).TemplateResponse(
        request,
        "advice.html",
        {"items": full, "show_dismissed": show_dismissed},
    )


@router.post("/advice/{advice_id}/dismiss")
def advice_dismiss(request: Request, advice_id: int):
    with _db(request) as conn:
        ok = dismiss_advice(conn, advice_id)
    if not ok:
        raise HTTPException(404, "advice not found")
    # HTMX: delete the card from the DOM. Return 200 with empty body.
    return Response(status_code=200)


# ═════════════════════════════════════════════════════════════════════════════
# POST /accounts/{uid}/toggle — flip excluded_from_spend (HTMX)
# ═════════════════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════════════════
# GET /rules — regex rule management
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    from finance.categorize import load_rules
    from finance.config import get_settings

    settings = get_settings()
    rules = load_rules(settings.rules_path)
    return _templates(request).TemplateResponse(
        request,
        "rules.html",
        {
            "rules": rules,
            "rules_path": str(settings.rules_path),
            "taxonomy": load_taxonomy(),
        },
    )


@router.post("/rules/add", response_class=HTMLResponse)
def rules_add(
    request: Request,
    match: str = Form(...),
    category: str = Form(...),
):
    """Add a new rule at the end of rules.yaml. First-match-wins so order matters."""
    import re as _re

    from finance.categorize import Rule, load_rules, save_rules
    from finance.config import get_settings

    settings = get_settings()
    # Validate regex compiles
    try:
        compiled = _re.compile(match)
    except _re.error as e:
        return HTMLResponse(
            f'<div class="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">'
            f"Invalid regex: {e}</div>",
            status_code=400,
        )
    if category not in load_taxonomy():
        return HTMLResponse(
            f'<div class="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">'
            f"Category {category!r} not in taxonomy.</div>",
            status_code=400,
        )

    rules = load_rules(settings.rules_path)
    rules.append(Rule(match=compiled, category=category))
    save_rules(settings.rules_path, rules)
    # Return a fresh full list fragment so the whole table swaps.
    return _templates(request).TemplateResponse(
        request,
        "_rules_table.html",
        {"rules": rules},
    )


@router.post("/rules/{index}/delete", response_class=HTMLResponse)
def rules_delete(request: Request, index: int):
    from finance.categorize import load_rules, save_rules
    from finance.config import get_settings

    settings = get_settings()
    rules = load_rules(settings.rules_path)
    if not 0 <= index < len(rules):
        raise HTTPException(404, "rule index out of range")
    rules.pop(index)
    save_rules(settings.rules_path, rules)
    return _templates(request).TemplateResponse(
        request,
        "_rules_table.html",
        {"rules": rules},
    )


@router.post("/rules/reenrich", response_class=HTMLResponse)
def rules_reenrich(request: Request):
    """Re-run the enrichment pipeline so rule changes propagate."""
    from finance.analysis.enrich import enrich_transactions
    from finance.categorize import load_rules
    from finance.config import get_settings

    settings = get_settings()
    rules = load_rules(settings.rules_path)
    with _db(request) as conn:
        summary = enrich_transactions(conn, reenrich=True, rules=rules)
    return _templates(request).TemplateResponse(
        request,
        "_reenrich_result.html",
        {
            "total": summary.total_transactions,
            "classified": summary.merchants_classified,
            "income_tagged": summary.income_tagged,
            "transfer_tagged": summary.transfer_tagged,
            "streams": summary.streams_computed,
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# GET /settings — API key status + LLM usage
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    import os

    from finance.llm.client import (
        DEFAULT_ADVISE_MODEL,
        DEFAULT_CATEGORIZE_MODEL,
        resolve_api_key,
    )

    key_source = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        key_source = "environment variable"
    elif resolve_api_key():
        key_source = "OS keyring"

    # Recent LLM runs
    with _db(request) as conn:
        rows = conn.execute(
            """
            SELECT kind, model, started_at, ended_at, input_tokens,
                   output_tokens, status, error
            FROM llm_runs
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
        totals = conn.execute(
            """
            SELECT kind,
                   COUNT(*) AS n,
                   COALESCE(SUM(input_tokens), 0) AS in_tok,
                   COALESCE(SUM(output_tokens), 0) AS out_tok
            FROM llm_runs
            GROUP BY kind
            ORDER BY kind
            """
        ).fetchall()

    return _templates(request).TemplateResponse(
        request,
        "settings.html",
        {
            "key_source": key_source,
            "default_categorize_model": DEFAULT_CATEGORIZE_MODEL,
            "default_advise_model": DEFAULT_ADVISE_MODEL,
            "runs": [dict(r) for r in rows],
            "totals": [dict(r) for r in totals],
        },
    )


@router.post("/settings/llm-key", response_class=HTMLResponse)
def settings_set_llm_key(request: Request, api_key: str = Form(...)):
    from finance.llm.client import store_api_key

    api_key = api_key.strip()
    if not api_key:
        return HTMLResponse(
            '<div class="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">'
            "Empty key — not stored.</div>",
            status_code=400,
        )
    try:
        store_api_key(api_key)
    except Exception as e:
        return HTMLResponse(
            f'<div class="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">'
            f"Keyring error: {e}</div>",
            status_code=500,
        )
    return HTMLResponse(
        '<div class="text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded px-3 py-2">'
        "✓ API key stored in OS keyring. Reload the page to confirm status.</div>"
    )


@router.post("/accounts/{account_uid}/toggle", response_class=HTMLResponse)
def accounts_toggle_exclude(request: Request, account_uid: str):
    with _db(request) as conn:
        row = conn.execute(
            "SELECT COALESCE(excluded_from_spend, 0) FROM accounts WHERE account_uid = ?",
            (account_uid,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "account not found")
        new_val = 0 if row[0] else 1
        conn.execute(
            "UPDATE accounts SET excluded_from_spend = ? WHERE account_uid = ?",
            (new_val, account_uid),
        )
        conn.commit()
        acc = conn.execute(
            """
            SELECT a.account_uid, a.name, a.iban, a.currency,
                   COALESCE(a.excluded_from_spend, 0) AS excluded,
                   s.aspsp_name
            FROM accounts a JOIN sessions s ON s.session_id = a.session_id
            WHERE a.account_uid = ?
            """,
            (account_uid,),
        ).fetchone()
    return _templates(request).TemplateResponse(
        request,
        "_account_row.html",
        {"a": dict(acc)},
    )
