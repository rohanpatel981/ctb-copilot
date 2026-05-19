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
}


def _prefill_engagement_from_query_params() -> None:
    """If the page was opened with ?clientId=...&gaapId=...&parentId=...
    &finYearId=...&periodId=...&currencyId=..., write those into the
    engagement-form session state so the user lands pre-scoped.

    Runs at most once per session — subsequent reruns are skipped so the
    user's manual edits in the engagement form are not clobbered.
    """
    if st.session_state.get("_engagement_prefilled"):
        return
    for url_key, session_key in _URL_PARAM_TO_SESSION_KEY.items():
        value = st.query_params.get(url_key)
        if value:
            st.session_state[session_key] = value
    st.session_state["_engagement_prefilled"] = True


# ---------- engagement form ----------


def render_engagement_form() -> None:
    """Active-engagement editor — rendered inside the sidebar as a single
    collapsible expander. Inputs stack vertically (sidebar is narrow), no
    bordered container — keeps the visual weight low so the chat in the
    main area stays the hero. Expanded by default only when something's
    missing."""
    ok, _missing = _engagement_valid_for_sync()
    with st.expander("🎯 Active engagement", expanded=not ok):
        st.caption("Every API call is scoped to this tuple.")
        for key, label in _ENGAGEMENT_FIELDS:
            st.text_input(
                label,
                value=st.session_state.get(f"eng-{key}", _DEFAULT_ENGAGEMENT[key]),
                key=f"eng-{key}",
            )

        st.text_input(
            "Financial year period (label)",
            value=st.session_state.get("eng-finYearPeriod", ""),
            key="eng-finYearPeriod",
            placeholder="FY 2024-25",
            help=(
                "Human-readable label tagged on every synced row. The assistant "
                "filters years using this, not the UUIDs."
            ),
        )

        if settings.api_token is None:
            st.text_input(
                "API token",
                value=st.session_state.get("api_token", ""),
                key="api_token",
                type="password",
                help="Authorization: Bearer …. Leave blank in dev mode.",
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


def render_answer(result: dict, index: int = 0) -> None:
    st.markdown(f"**You** · {result['question']}")
    st.markdown(f"**Assistant** &nbsp;&nbsp; {_confidence_badge(result['confidence'])}")

    post = result.get("post_process", "none")
    rows = result.get("rows", [])

    if post == "yoy_pct" and result.get("yoy_changes"):
        for ch in result["yoy_changes"]:
            pct = f"{ch['pct_change']:+.2f}%" if ch["pct_change"] is not None else "n/a"
            st.markdown(
                f"- **{ch['metric']}**: {_fmt_number(ch['from_value'])} ({ch['from_period']}) "
                f"→ {_fmt_number(ch['to_value'])} ({ch['to_period']}) &nbsp; **{pct}**"
            )
    elif post == "ratio" and result.get("ratios"):
        for r in result["ratios"]:
            period_lbl = f" ({r['period']})" if r.get("period") else ""
            val = f"{r['value']:.4f}" if r["value"] is not None else "n/a"
            st.markdown(f"- **{r['numerator']} / {r['denominator']}**{period_lbl}: **{val}**")
    elif rows:
        for r in rows[:3]:
            parts = [f"**{k}**: {_fmt_number(v)}" for k, v in r.items()]
            st.markdown("  ·  ".join(parts))
        if len(rows) > 3:
            st.caption(f"…and {len(rows) - 3} more rows. See *Source rows* below.")
    else:
        st.markdown("_(no rows returned)_")

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
</style>
"""


def _inject_brand_css() -> None:
    st.markdown(_BRAND_CSS, unsafe_allow_html=True)


def render_active_scope_chips() -> None:
    """Top-of-main pills summarising the active engagement at a glance.
    Read-only — edits happen in the sidebar's `Active engagement` expander."""
    eng = _engagement()
    fyp = _engagement_fin_year_period()
    chips: list[tuple[str, bool]] = []  # (text, is_set)
    if fyp:
        chips.append((fyp, True))
    elif eng.get("finYearId", "").strip():
        chips.append((f"FY · {eng['finYearId'][:8]}…", True))
    if eng.get("reportingPeriodId", "").strip():
        chips.append((f"Period · {eng['reportingPeriodId'][:8]}…", True))
    if eng.get("clientId", "").strip():
        chips.append((f"Client · {eng['clientId']}", True))
    if eng.get("currencyId", "").strip():
        chips.append((f"Currency · {eng['currencyId'][:8]}…", True))
    if not chips:
        chips.append(("No engagement set", False))
    html = "<div class='ctb-chips'>"
    for text, is_set in chips:
        cls = "ctb-chip" if is_set else "ctb-chip muted"
        html += f"<span class='{cls}'>{text}</span>"
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


def render_chat() -> None:
    if "chat" not in st.session_state:
        st.session_state.chat = []

    st.caption("Answers are scoped to the active engagement. Edit it in the sidebar.")
    for i, entry in enumerate(st.session_state.chat):
        render_answer(entry, index=i)
        st.divider()

    question = st.chat_input("What's the YoY change in current liabilities?")
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
