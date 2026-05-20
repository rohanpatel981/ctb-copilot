"""Streamlit UI for ctb-copilot.

v0.5 layout: the UI is tenant-aware. The user fills in the 6-tuple
engagement identity (client_id, gaap_id, reporting_parent_company_id,
fin_year_id, reporting_period_id, currency_id) at the top of the page;
every API call threads those values through. The optional bearer token
is sent on every request when configured.

The sidebar shows only the loaded periods that match the active
engagement's tenant scope so multi-tenant deployments don't leak
other tenants' data through the UI.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import streamlit as st

from ctb_copilot.config import settings
from ctb_copilot.export import export_filename, query_result_to_xlsx

API = settings.api_base_url
TIMEOUT = httpx.Timeout(120.0)

# Defaults shown in the engagement form. These exist so a developer can
# `uv run ctb-ui` against the sample data with no friction; real users
# of multi-tenant deployments will fill these in from their portal.
_DEFAULT_ENGAGEMENT: dict[str, str] = {
    "clientId": "demo-client",
    "gaapId": "ind_as",
    "reportingParentCompanyId": "demo-rpc",
    "currencyId": "INR",
    "finYearId": "FY 2024-25",
    "reportingPeriodId": "Annual",
}

_ENGAGEMENT_FIELDS = (
    ("clientId", "Client ID"),
    ("gaapId", "GAAP ID"),
    ("reportingParentCompanyId", "Reporting Parent Company ID"),
    ("currencyId", "Currency"),
    ("finYearId", "FY (FinYear ID)"),
    ("reportingPeriodId", "Reporting period"),
)


# ---------- helpers ----------


def _auth_headers() -> dict[str, str]:
    """Build the Authorization header from session-state token (UI-supplied)
    or from settings (env-configured). UI-supplied wins."""
    token = st.session_state.get("api_token") or settings.api_token
    return {"Authorization": f"Bearer {token}"} if token else {}


def _api(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {}) or {}
    headers.update(_auth_headers())
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.request(method, f"{API}{path}", headers=headers, **kwargs)
        r.raise_for_status()
        return r.json()


def _api_safe(method: str, path: str, **kwargs):
    try:
        return _api(method, path, **kwargs)
    except httpx.HTTPError as e:
        # Don't spam the UI on 401s when the user hasn't entered a token yet
        if e.response is not None and e.response.status_code == 401:
            return None
        st.error(f"API error: {e}")
        return None


def _engagement() -> dict[str, str]:
    """Read the active engagement out of session state, filling missing
    fields with defaults so partial setup doesn't crash the page."""
    return {k: st.session_state.get(f"eng-{k}", _DEFAULT_ENGAGEMENT[k]) for k, _ in _ENGAGEMENT_FIELDS}


def _required_scope_keys() -> list[str]:
    return ["clientId", "gaapId", "reportingParentCompanyId", "currencyId"]


def _engagement_valid_for_sync() -> tuple[bool, list[str]]:
    eng = _engagement()
    missing = [k for k, _ in _ENGAGEMENT_FIELDS if not eng.get(k, "").strip()]
    return (len(missing) == 0, missing)


def _engagement_valid_for_query() -> tuple[bool, list[str]]:
    eng = _engagement()
    missing = [k for k in _required_scope_keys() if not eng.get(k, "").strip()]
    return (len(missing) == 0, missing)


def _matches_active_engagement(row: dict) -> bool:
    eng = _engagement()
    for k in _required_scope_keys():
        # Map camelCase eng → snake_case API field
        api_key = {
            "clientId": "client_id",
            "gaapId": "gaap_id",
            "reportingParentCompanyId": "reporting_parent_company_id",
            "currencyId": "currency_id",
        }[k]
        if (row.get(api_key) or "") != eng.get(k, ""):
            return False
    return True


def _fmt_number(val) -> str:
    if val is None:
        return "—"
    if isinstance(val, (int, float)):
        return f"{val:,.2f}"
    return str(val)


def _confidence_badge(conf: str) -> str:
    return {"high": "🟢 High", "medium": "🟡 Medium", "low": "🔴 Low"}.get(conf, conf)


# Map URL query-param names a parent FE may send → engagement-form session
# keys. Both short ("parentId") and long ("reportingParentCompanyId") forms
# are accepted so the FE side is free to pick either.
_URL_PARAM_TO_SESSION_KEY: dict[str, str] = {
    "clientId": "eng-clientId",
    "gaapId": "eng-gaapId",
    "parentId": "eng-reportingParentCompanyId",
    "reportingParentCompanyId": "eng-reportingParentCompanyId",
    "finYearId": "eng-finYearId",
    "periodId": "eng-reportingPeriodId",
    "reportingPeriodId": "eng-reportingPeriodId",
    "currencyId": "eng-currencyId",
    "finYearPeriod": "eng-finYearPeriod",
    # Human-readable names (display only — IDs above are what scope the data).
    "clientName": "eng-clientName",
    "gaapName": "eng-gaapName",
    "parentName": "eng-parentName",
    "finYearName": "eng-finYearName",
    "periodName": "eng-periodName",
    "currencyName": "eng-currencyName",
}

# Pairs of (id session-key, name session-key) used by the scope-chip
# renderer to prefer the human-readable name over the opaque ID.
_ID_NAME_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("eng-clientId", "eng-clientName", "Client"),
    ("eng-reportingParentCompanyId", "eng-parentName", "Parent"),
    ("eng-gaapId", "eng-gaapName", "GAAP"),
    ("eng-finYearId", "eng-finYearName", "FY"),
    ("eng-reportingPeriodId", "eng-periodName", "Period"),
    ("eng-currencyId", "eng-currencyName", "Currency"),
)


def _prefill_engagement_from_query_params() -> None:
    """If the page was opened with ?clientId=...&gaapId=...&parentId=...
    &finYearId=...&periodId=...&currencyId=..., write those into the
    engagement-form session state so the user lands pre-scoped.

    Runs at most once per session — subsequent reruns are skipped so the
    user's manual edits in the engagement form are not clobbered.
    """
    if st.session_state.get("_engagement_prefilled"):
        return
    locked: set[str] = set()
    for url_key, session_key in _URL_PARAM_TO_SESSION_KEY.items():
        value = st.query_params.get(url_key)
        if value:
            st.session_state[session_key] = value
            # Only ID fields get locked — names are display-only metadata
            # and the finYearPeriod label is user-editable by design.
            if session_key.startswith("eng-") and session_key.endswith("Id"):
                locked.add(session_key)
    st.session_state["_locked_engagement_keys"] = locked

    # Parse the optional finYears JSON list — [{id, name}, ...]. When
    # supplied, the engagement form renders a selectbox bound to
    # eng-finYearId so the user can switch FYs without typing UUIDs.
    fin_years_raw = st.query_params.get("finYears")
    fin_years_options: list[dict[str, str]] = []
    if fin_years_raw:
        try:
            parsed = json.loads(fin_years_raw)
            if isinstance(parsed, list):
                for item in parsed:
                    fy_id = (item or {}).get("id")
                    fy_name = (item or {}).get("name")
                    if fy_id and fy_name:
                        fin_years_options.append({"id": str(fy_id), "name": str(fy_name)})
        except (json.JSONDecodeError, AttributeError):
            pass
    st.session_state["_fin_year_options"] = fin_years_options
    st.session_state["_engagement_prefilled"] = True


# ---------- engagement form ----------


def render_engagement_form() -> None:
    """Active-engagement editor — rendered inside the sidebar as a single
    collapsible expander. Inputs stack vertically (sidebar is narrow), no
    bordered container — keeps the visual weight low so the chat in the
    main area stays the hero. Expanded by default only when something's
    missing."""
    ok, _missing = _engagement_valid_for_sync()
    # Map engagement-id field-key → session-state key holding the
    # human-readable name (when the parent FE passed one in the URL).
    _id_to_name_key: dict[str, str] = {
        "clientId": "eng-clientName",
        "gaapId": "eng-gaapName",
        "reportingParentCompanyId": "eng-parentName",
        "finYearId": "eng-finYearName",
        "reportingPeriodId": "eng-periodName",
        "currencyId": "eng-currencyName",
    }
    locked_keys: set[str] = st.session_state.get("_locked_engagement_keys") or set()
    fin_year_options: list[dict[str, str]] = st.session_state.get("_fin_year_options") or []
    with st.expander("🎯 Active engagement", expanded=not ok):
        st.caption("Every API call is scoped to this tuple.")
        for key, label in _ENGAGEMENT_FIELDS:
            session_key = f"eng-{key}"
            is_locked = session_key in locked_keys
            name_val = (st.session_state.get(_id_to_name_key.get(key, ""), "") or "").strip()

            # Special-case finYearId when the host passed a list of
            # available FYs — render a selectbox so the user can switch
            # without typing UUIDs. The bound value is the ID; sync uses
            # whatever's currently selected.
            if key == "finYearId" and fin_year_options:
                ids = [opt["id"] for opt in fin_year_options]
                current_id = st.session_state.get(session_key, _DEFAULT_ENGAGEMENT[key])
                try:
                    default_index = ids.index(current_id) if current_id in ids else 0
                except ValueError:
                    default_index = 0
                selected_id = st.selectbox(
                    label,
                    options=ids,
                    index=default_index,
                    key=f"{session_key}-select",
                    format_func=lambda i: next(
                        (o["name"] for o in fin_year_options if o["id"] == i), i
                    ),
                    help="Pick a financial year — sync will use the selected ID.",
                )
                # Keep the canonical eng-finYearId session value in sync
                # with the selectbox, and refresh the matching name.
                st.session_state[session_key] = selected_id
                matched_name = next(
                    (o["name"] for o in fin_year_options if o["id"] == selected_id), ""
                )
                if matched_name:
                    st.session_state["eng-finYearName"] = matched_name
                continue

            # Locked field — render as compact read-only "row" instead
            # of a greyed-out input: bold label + name as the primary
            # value, with the underlying ID shown smaller / muted below.
            # Keeps the visual focus on what humans recognise.
            if is_locked:
                id_val = st.session_state.get(session_key, "")
                primary = name_val or id_val or "—"
                st.markdown(
                    f"<div class='ctb-locked-row'>"
                    f"<div class='ctb-locked-label'>{label}</div>"
                    f"<div class='ctb-locked-value'>{primary}</div>"
                    + (f"<div class='ctb-locked-id'>{id_val}</div>" if name_val and id_val else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )
                continue

            st.text_input(
                label,
                value=st.session_state.get(session_key, _DEFAULT_ENGAGEMENT[key]),
                key=session_key,
            )

        # Financial-year-period label: read-only, derived from the FY
        # name supplied by the host. Falls back to whatever the user
        # already had in session state (e.g. a manual default).
        fyp_value = (
            (st.session_state.get("eng-finYearName") or "").strip()
            or (st.session_state.get("eng-finYearPeriod") or "").strip()
        )
        st.session_state["eng-finYearPeriod"] = fyp_value
        st.markdown(
            f"<div class='ctb-locked-row'>"
            f"<div class='ctb-locked-label'>Financial year period</div>"
            f"<div class='ctb-locked-value'>{fyp_value or '—'}</div>"
            "</div>",
            unsafe_allow_html=True,
        )


def _engagement_fin_year_period() -> str | None:
    val = st.session_state.get("eng-finYearPeriod") or ""
    return val.strip() or None


# ---------- sync (DocumentDB) ----------


def render_docdb_sync() -> None:
    """DocumentDB-sync expander, rendered inside the sidebar by the caller."""
    cfg = _api_safe("GET", "/sync/config")
    if not cfg or not cfg.get("configured"):
        return

    eng = _engagement()
    ok, missing = _engagement_valid_for_sync()

    with st.expander("🔄 Sync from DocumentDB", expanded=False):
        st.caption(f"Source: `{cfg['database']}.{cfg['collection']}`")

        preview = dict(eng)
        preview["status"] = "ACTIVE"  # server pins this
        with st.expander("Filter preview", expanded=False):
            st.code(json.dumps(preview, indent=2), language="json")

        if not ok:
            st.warning(f"Fill in: {', '.join(missing)} (above) before syncing.")
        elif st.button("🚀 Sync now", key="sync-trigger", type="primary"):
            try:
                payload: dict = {"filter": eng}
                fyp = _engagement_fin_year_period()
                if fyp:
                    payload["finYearPeriod"] = fyp
                resp = _api("POST", "/sync", json=payload)
                st.session_state["active_sync_id"] = resp["sync_id"]
                st.rerun()
            except httpx.HTTPError as e:
                detail = ""
                try:
                    detail = e.response.json().get("detail", "") if e.response else ""
                except Exception:
                    pass
                st.error(f"Sync request failed: {e}. {detail}")

        active_id = st.session_state.get("active_sync_id")
        if active_id:
            status = _api_safe("GET", f"/sync/{active_id}")
            if status:
                s = status.get("status")
                rows = status.get("row_count")
                fy = status.get("fin_year_id")
                if s in ("pending", "running", "reading", "inserting"):
                    prog = f"Syncing **{fy}**"
                    prog += f" — {rows:,} rows so far…" if rows else " — starting up…"
                    st.info(prog)
                    st.progress(0)
                    time.sleep(2)
                    st.rerun()
                elif s == "done":
                    st.success(f"✓ Synced {rows:,} rows for {fy}")
                    if st.button("Dismiss", key="dismiss-done"):
                        st.session_state.pop("active_sync_id", None)
                        st.rerun()
                elif s == "failed":
                    st.error(f"Sync failed: {status.get('error') or 'unknown error'}")
                    if st.button("Dismiss", key="dismiss-failed"):
                        st.session_state.pop("active_sync_id", None)
                        st.rerun()


# ---------- sidebar (loaded data) ----------


def render_sidebar() -> None:
    """Loaded data + recent ingestions. Caller wraps in `with st.sidebar:`."""
    with st.expander("📊 Loaded data", expanded=True):
        st.caption("Scoped to the active engagement.")
        periods = _api_safe("GET", "/periods") or []
        periods = [p for p in periods if _matches_active_engagement(p)]
        if not periods:
            st.info("No data for this engagement yet. Sync or upload a CTB.")
        else:
            for p in periods:
                period_lbl = f"{p.get('fin_year_id')} · {p.get('reporting_period_id')}"
                st.write(f"**{period_lbl}** — {p['row_count']:,} rows · {p['entity_count']} entities")

    with st.expander("📜 Recent ingestions", expanded=False):
        uploads = _api_safe("GET", "/uploads") or []
        uploads = [u for u in uploads if _matches_active_engagement(u)]
        if not uploads:
            st.caption("No ingestions yet for this engagement.")
        else:
            for u in uploads[:10]:
                icon = {"done": "✓", "failed": "✗", "pending": "⏳",
                        "reading": "⏳", "inserting": "⏳", "running": "⏳",
                        "replaced": "↩"}.get(u["status"], "•")
                row_text = f" — {u['row_count']:,} rows" if u["row_count"] else ""
                fy = u.get("fin_year_id") or "?"
                st.caption(f"{icon} **{fy}** · {u['filename']}{row_text}")
                if u["status"] == "failed" and u.get("error"):
                    st.caption(f"  error: {u['error']}")
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()


# ---------- Excel upload ----------


def render_upload() -> None:
    """Excel upload as a collapsible sidebar section. Caller wraps with sidebar."""
    with st.expander("📤 Upload a CTB", expanded=False):
        st.caption(
            "Re-uploading for the same engagement replaces previous data."
        )
        file = st.file_uploader(
            "CTB Excel file",
            type=["xlsx", "xlsb", "xls"],
            label_visibility="collapsed",
            help="Single sheet, headers in row 1, 22 columns matching the CTB layout.",
        )

        ok, missing = _engagement_valid_for_sync()
        if file and not ok:
            st.warning(f"Fill in: {', '.join(missing)}")
        elif file and st.button("Upload & ingest", type="primary", use_container_width=True):
            eng = _engagement()
            files = {"file": (file.name, file.getvalue(), file.type or "application/octet-stream")}
            form_data = dict(eng)
            fyp = _engagement_fin_year_period()
            if fyp:
                form_data["finYearPeriod"] = fyp
            try:
                with httpx.Client(timeout=TIMEOUT) as client:
                    r = client.post(
                        f"{API}/upload",
                        files=files,
                        data=form_data,
                        headers=_auth_headers(),
                    )
                    r.raise_for_status()
                    resp = r.json()
                st.success(f"Upload queued: `{resp['upload_id']}`.")
            except httpx.HTTPError as e:
                detail = ""
                try:
                    detail = e.response.json().get("detail", "") if e.response else ""
                except Exception:
                    pass
                st.error(f"Upload failed: {e}. {detail}")


# ---------- chat ----------


def _humanize_col(col: str) -> str:
    """Turn `consol_gl_description` into 'Consol GL Description'."""
    overrides = {
        "fin_year_period": "Period",
        "amount_consolidated": "Consolidated",
        "amount_reporting_ccy": "Reporting amount",
        "amount_functional_ccy": "Functional amount",
        "adj_other_consolidated": "Other adjustment",
        "adj_nci": "NCI",
        "adj_goodwill": "Goodwill",
        "adj_ppa": "PPA",
        "adj_intercompany": "Intercompany",
        "adj_investment_capital": "Investment / share-capital",
        "adj_retained_earnings": "Retained earnings",
        "adj_fctr": "FCTR",
        "fs_category": "FS category",
        "bs_classification": "BS classification",
        "fsli": "FSLI",
        "consol_gl_code": "GL code",
        "consol_gl_description": "GL description",
        "entity_name": "Entity",
        "entity_code": "Entity code",
        "gl_nature": "GL nature",
        "functional_currency": "Currency",
    }
    return overrides.get(col, col.replace("_", " ").capitalize())


# Internal columns we never want to show in the answer surface — they're
# scope/audit data, not part of the answer. They stay visible in the
# "Source rows" expander so a CA can audit if needed.
_HIDDEN_COLUMNS = frozenset({
    "client_id", "gaap_id", "reporting_parent_company_id",
    "fin_year_id", "reporting_period_id", "currency_id",
    "upload_id", "row_number",
})


def _visible_keys(row: dict) -> list[str]:
    return [k for k in row.keys() if k not in _HIDDEN_COLUMNS]


def _render_single_row(row: dict) -> None:
    """Pretty render for a 1-row answer: a row of metric cards.

    Each numeric column gets a big-number card; each text/category column
    appears as a small chip above the numbers. NULLs render as an em-dash
    so the analyst sees the column was selected but had no data.
    """
    keys = _visible_keys(row)
    text_keys = [k for k in keys if not isinstance(row.get(k), (int, float))]
    num_keys = [k for k in keys if isinstance(row.get(k), (int, float))]

    if text_keys:
        chips = "".join(
            f"<span class='ctb-chip'>{_humanize_col(k)} · {row.get(k)}</span>"
            for k in text_keys
            if row.get(k) is not None
        )
        if chips:
            st.markdown(f"<div class='ctb-chips'>{chips}</div>", unsafe_allow_html=True)

    if num_keys:
        cols = st.columns(min(len(num_keys), 4))
        for i, k in enumerate(num_keys):
            with cols[i % len(cols)]:
                val = row.get(k)
                display = _fmt_number(val) if isinstance(val, (int, float)) else "—"
                st.metric(label=_humanize_col(k), value=display)


def _render_multi_rows(rows: list[dict]) -> None:
    """Pretty render for multi-row answers: a clean dataframe with
    humanized headers, hidden scope columns."""
    if not rows:
        return
    visible_cols = [k for k in rows[0].keys() if k not in _HIDDEN_COLUMNS]
    cleaned = [
        {_humanize_col(k): r.get(k) for k in visible_cols}
        for r in rows
    ]
    st.dataframe(cleaned, use_container_width=True, hide_index=True)


def render_answer(result: dict, index: int = 0) -> None:
    st.markdown(f"**You** · {result['question']}")
    st.markdown(f"**Assistant** &nbsp;&nbsp; {_confidence_badge(result['confidence'])}")

    post = result.get("post_process", "none")
    rows = result.get("rows", [])

    # Headline number(s) first — what the user actually asked for.
    if post == "yoy_pct" and result.get("yoy_changes"):
        cols = st.columns(min(len(result["yoy_changes"]), 4))
        for i, ch in enumerate(result["yoy_changes"]):
            with cols[i % len(cols)]:
                pct = f"{ch['pct_change']:+.2f}%" if ch["pct_change"] is not None else "n/a"
                st.metric(
                    label=f"{_humanize_col(ch['metric'])} · {ch['from_period']} → {ch['to_period']}",
                    value=pct,
                    delta=f"{_fmt_number(ch['from_value'])} → {_fmt_number(ch['to_value'])}",
                    delta_color="off",
                )
    elif post == "ratio" and result.get("ratios"):
        cols = st.columns(min(len(result["ratios"]), 3))
        for i, r in enumerate(result["ratios"]):
            with cols[i % len(cols)]:
                period_lbl = r.get("period") or ""
                val = f"{r['value']*100:.2f}%" if r["value"] is not None else "—"
                st.metric(
                    label=f"{_humanize_col(r['numerator'])} ÷ {_humanize_col(r['denominator'])}"
                          + (f" · {period_lbl}" if period_lbl else ""),
                    value=val,
                )
    elif post == "ratio" and rows:
        # The LLM asked for a ratio but the post-processor couldn't compute
        # one (typically because one side of the row was NULL — e.g. the
        # numerator's category-filter matched zero rows). Show the row
        # cleanly so the user can see WHY.
        if len(rows) == 1:
            _render_single_row(rows[0])
        else:
            _render_multi_rows(rows)
        st.caption(
            "Ratio could not be computed — one side of the calculation was empty. "
            "Check the row above; the filter probably matched no data."
        )
    elif rows:
        if len(rows) == 1:
            _render_single_row(rows[0])
        else:
            _render_multi_rows(rows)
    else:
        st.info("No rows returned.")

    st.markdown(f"> {result['explanation']}")

    with st.expander("View SQL"):
        st.code(result["sql"], language="sql")
        if post != "none":
            st.caption(f"Post-processing: `{post}`")

    with st.expander(f"View source rows ({len(rows)})"):
        if rows:
            st.dataframe(rows, use_container_width=True)
        else:
            st.caption("No rows to show.")

    try:
        xlsx_bytes = query_result_to_xlsx(result)
        st.download_button(
            "📄 Export to Excel",
            data=xlsx_bytes,
            file_name=export_filename(result.get("question")),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"export-{index}",
            help="Download answer + SQL + source rows as a 3-sheet .xlsx for working papers.",
        )
    except Exception as e:
        st.caption(f"Export unavailable: {e}")


def _build_scope_for_query() -> dict[str, str]:
    """Build the TenantScope payload for /query. Mandatory fields always
    present; fin_year_id / reporting_period_id only included if filled
    (so YoY questions across years can omit fin_year_id)."""
    eng = _engagement()
    scope = {k: eng[k] for k in _required_scope_keys()}
    if eng.get("finYearId", "").strip():
        scope["finYearId"] = eng["finYearId"]
    if eng.get("reportingPeriodId", "").strip():
        scope["reportingPeriodId"] = eng["reportingPeriodId"]
    return scope


_BRAND_CSS = """
<style>
/* Sidebar: tighter spacing, brand-tinted background. */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #FBFAFE 0%, #F4EFFC 100%);
}
section[data-testid="stSidebar"] [data-testid="stExpander"] {
    border: 1px solid #E5E0F2;
    border-radius: 10px;
    margin-bottom: 8px;
    background: #FFFFFF;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary p {
    font-weight: 600;
}

/* Main area: brand-purple hero header. */
.ctb-hero {
    background: linear-gradient(135deg, #6F42C1 0%, #8B5CF6 100%);
    color: #FFFFFF;
    padding: 22px 28px;
    border-radius: 14px;
    margin-bottom: 18px;
    box-shadow: 0 4px 16px rgba(111, 66, 193, 0.18);
}
.ctb-hero h1 {
    margin: 0;
    color: #FFFFFF !important;
    font-size: 1.6rem;
}
.ctb-hero p {
    margin: 4px 0 0 0;
    opacity: 0.92;
    font-size: 0.95rem;
}

/* Scope-chip row under the hero. */
.ctb-chips {
    display: flex; flex-wrap: wrap; gap: 8px;
    margin: 0 0 22px 0;
}
.ctb-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 12px; border-radius: 999px;
    background: #F4EFFC; color: #4C2D91;
    font-size: 0.85rem; font-weight: 500;
    border: 1px solid #E5D9F7;
}
.ctb-chip.muted {
    background: #F3F4F6; color: #6B7280; border-color: #E5E7EB;
}

/* Locked engagement row (sidebar): compact label + bold value + muted ID. */
.ctb-locked-row {
    padding: 8px 10px;
    margin-bottom: 8px;
    border: 1px solid #E5E0F2;
    border-radius: 8px;
    background: #FAFAFE;
}
.ctb-locked-label {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #6B7280;
    margin-bottom: 2px;
}
.ctb-locked-value {
    font-size: 0.95rem;
    font-weight: 600;
    color: #1F2937;
    line-height: 1.25;
    word-break: break-word;
}
.ctb-locked-id {
    font-size: 0.72rem;
    color: #9CA3AF;
    margin-top: 2px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    word-break: break-all;
}

/* Chat input: make it the visual focus. */
[data-testid="stChatInput"] textarea {
    font-size: 1.05rem;
    min-height: 56px;
    border: 1.5px solid #D8CFEF;
}
[data-testid="stChatInput"] textarea:focus {
    border-color: #6F42C1;
    box-shadow: 0 0 0 3px rgba(111, 66, 193, 0.15);
}

/* Suggestion-chip label */
.ctb-chip-row-label {
    font-size: 0.78rem;
    font-weight: 600;
    color: #6B7280;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 8px 0 6px 0;
}
</style>
"""


def _inject_brand_css() -> None:
    st.markdown(_BRAND_CSS, unsafe_allow_html=True)


def render_active_scope_chips() -> None:
    """Top-of-main pills summarising the active engagement at a glance.
    Prefer the human-readable name (passed via URL `…Name` params) and
    fall back to a truncated ID when no name is available.
    Read-only — edits happen in the sidebar's `Active engagement` expander."""
    chips: list[tuple[str, bool]] = []  # (text, is_set)
    fyp = _engagement_fin_year_period()
    if fyp:
        chips.append((fyp, True))

    for id_key, name_key, label in _ID_NAME_PAIRS:
        # finYearPeriod takes precedence over FY/Period chips when set,
        # so skip those two to avoid noise.
        if fyp and label in ("FY", "Period"):
            continue
        name_val = (st.session_state.get(name_key) or "").strip()
        id_val = (st.session_state.get(id_key) or "").strip()
        if name_val:
            chips.append((f"{label} · {name_val}", True))
        elif id_val:
            chips.append((f"{label} · {id_val[:8]}…", True))

    if not chips:
        chips.append(("No engagement set", False))
    html = "<div class='ctb-chips'>"
    for text, is_set in chips:
        cls = "ctb-chip" if is_set else "ctb-chip muted"
        html += f"<span class='{cls}'>{text}</span>"
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


# Hand-curated "FAQ" shortcuts shown above the chat input. Click → fires
# the question through the normal /query flow. The text is what the user
# would have typed; the LLM handles interpretation as usual.
_SUGGESTION_CHIPS: tuple[tuple[str, str], ...] = (
    ("💰 Totals by category", "Total by FS category for the active period"),
    ("🏢 Totals by entity", "Total consolidated amount by entity"),
    ("🔝 Top 10 line items", "Top 10 GL codes by absolute consolidated amount"),
    ("📉 YoY change in revenue", "YoY change in revenue"),
    ("⚖️ Operating margin", "Operating margin for the active period"),
    ("🧮 Adjustment breakdown", "Sum of each adjustment column (NCI, goodwill, PPA, intercompany, FCTR, retained earnings)"),
    ("🔄 Reconcile consolidated vs reporting", "Decompose amount_consolidated into amount_reporting_ccy + each adj_* for each FS category"),
    ("📐 Total of entire TB", "Total of the entire trial balance — should net to zero"),
)


def _render_suggestion_chips() -> None:
    """Row of clickable shortcut buttons above the chat input. Click sets
    a session-state question that the chat handler picks up on the next
    rerun and fires through the normal /query flow."""
    st.markdown("<div class='ctb-chip-row-label'>Suggested questions</div>", unsafe_allow_html=True)
    cols = st.columns(4)
    for i, (label, question) in enumerate(_SUGGESTION_CHIPS):
        with cols[i % 4]:
            if st.button(label, key=f"chip-{i}", use_container_width=True):
                st.session_state["_pending_question"] = question
                st.rerun()


def _render_landing_summary() -> None:
    """TB at a glance — runs ONCE on landing (no chat yet) via the
    non-LLM /summary endpoint. Cheap, predictable, no Anthropic call."""
    ok, _ = _engagement_valid_for_query()
    if not ok:
        st.info(
            "Set the active engagement in the sidebar to see a TB summary "
            "and ask questions."
        )
        return

    scope = _build_scope_for_query()
    data = _api_safe("POST", "/summary", json={"scope": scope})
    if not data:
        return
    if not data.get("has_data"):
        st.info(
            "No data synced for this engagement yet. Use the sidebar's "
            "**🔄 Sync from DocumentDB** or **📤 Upload a CTB** to load rows."
        )
        return

    fyp = data.get("fin_year_period") or ""
    if fyp:
        st.caption(f"Showing summary for **{fyp}** · scope locked above.")

    # ----- top-of-page: 4 metric cards, one per FS category (biggest 4) -----
    cats = data.get("by_category") or []
    if cats:
        top_cats = cats[:4]
        metric_cols = st.columns(len(top_cats))
        for col, cat in zip(metric_cols, top_cats):
            with col:
                st.metric(
                    label=cat["fs_category"] or "—",
                    value=_fmt_number(cat["total"]),
                    delta=f"{cat['row_count']} GL lines",
                    delta_color="off",
                )

    # ----- bar chart: amount by FS category -----
    if cats:
        chart_data = {c["fs_category"]: c["total"] for c in cats if c["fs_category"]}
        if chart_data:
            st.markdown("**Consolidated amount by FS category**")
            st.bar_chart(chart_data, height=240, use_container_width=True)

    # ----- two-column row: entity totals + top items -----
    left, right = st.columns(2)
    with left:
        st.markdown("**Top entities by total**")
        ents = data.get("by_entity") or []
        if ents:
            ent_table = [
                {"Entity": e["entity_name"], "Total": _fmt_number(e["total"])}
                for e in ents[:8]
            ]
            st.dataframe(ent_table, hide_index=True, use_container_width=True)
        else:
            st.caption("No entity data available.")
    with right:
        st.markdown("**Top 10 line items**")
        items = data.get("top_items") or []
        if items:
            item_table = [
                {
                    "GL": f"{it['consol_gl_code']} · {it['consol_gl_description'] or ''}".strip(" ·"),
                    "Entity": it["entity_name"] or "—",
                    "Amount": _fmt_number(it["amount"]),
                }
                for it in items
            ]
            st.dataframe(item_table, hide_index=True, use_container_width=True)
        else:
            st.caption("No line items available.")

    # ----- adjustment breakdown (only if any adjustments are non-zero) -----
    adj = data.get("adjustment_totals") or []
    if adj:
        st.markdown("**Adjustment totals**")
        adj_chart = {a["adjustment"].replace("adj_", ""): a["total"] for a in adj}
        st.bar_chart(adj_chart, height=200, use_container_width=True)


def render_chat() -> None:
    if "chat" not in st.session_state:
        st.session_state.chat = []

    # First-load landing summary (only when there's no chat history yet).
    if not st.session_state.chat:
        _render_landing_summary()
        st.divider()

    st.caption("Answers are scoped to the active engagement. Edit it in the sidebar.")
    for i, entry in enumerate(st.session_state.chat):
        render_answer(entry, index=i)
        st.divider()

    _render_suggestion_chips()

    # A chip-click in the previous run wrote to _pending_question; pick that
    # up here and fire it as if the user had typed it.
    chip_question = st.session_state.pop("_pending_question", None)
    typed_question = st.chat_input("Ask anything about your CTB…")
    question = chip_question or typed_question
    if question:
        ok, missing = _engagement_valid_for_query()
        if not ok:
            st.error(f"Fill in the engagement fields first: {', '.join(missing)}")
            return
        scope = _build_scope_for_query()
        with st.spinner("Thinking…"):
            try:
                result = _api("POST", "/query", json={"question": question, "scope": scope})
                st.session_state.chat.append(result)
                st.rerun()
            except httpx.HTTPError as e:
                detail = ""
                try:
                    detail = e.response.json().get("detail", "") if e.response else ""
                except Exception:
                    pass
                st.error(f"Query failed: {e}. {detail}")


# ---------- entry points ----------


def main() -> None:
    st.set_page_config(
        page_title="CTB Copilot",
        layout="wide",
        page_icon="📊",
        initial_sidebar_state="expanded",
    )
    _prefill_engagement_from_query_params()
    _inject_brand_css()

    # === Sidebar: all setup / admin / data status ===
    with st.sidebar:
        st.markdown("### 📊 CTB Copilot")
        st.caption("Multi-tenant Q&A over consolidated TB")
        st.divider()
        render_engagement_form()
        render_docdb_sync()
        render_upload()
        st.divider()
        render_sidebar()

    # === Main: hero + scope chips + chat ===
    st.markdown(
        """
        <div class="ctb-hero">
            <h1>Ask a question</h1>
            <p>Plain-English Q&A over your consolidated trial balance. Every answer ships with the SQL and source rows.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_active_scope_chips()
    render_chat()


def run() -> None:
    """Entry point for `uv run ctb-ui`. Launches Streamlit on this script."""
    here = Path(__file__).resolve()
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(here)], check=False)


if __name__ == "__main__":
    main()
