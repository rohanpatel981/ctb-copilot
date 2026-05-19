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
    """Top-of-page form for the active engagement. Every downstream call
    threads these 6 IDs."""
    with st.container(border=True):
        st.subheader("Active engagement")
        st.caption(
            "All API calls — upload, sync, query — are scoped to this tenant tuple. "
            "Multi-tenant deployments expect the FE to fill these from the parent portal; "
            "for local development the defaults match the sample CTB."
        )
        cols = st.columns(3)
        for i, (key, label) in enumerate(_ENGAGEMENT_FIELDS):
            with cols[i % 3]:
                st.text_input(
                    label,
                    value=st.session_state.get(f"eng-{key}", _DEFAULT_ENGAGEMENT[key]),
                    key=f"eng-{key}",
                )

        if settings.api_token is None:
            st.text_input(
                "API token (Authorization: Bearer …) — leave blank if API is in dev mode",
                value=st.session_state.get("api_token", ""),
                key="api_token",
                type="password",
            )


# ---------- sync (DocumentDB) ----------


def render_docdb_sync() -> None:
    """Sidebar section for DocumentDB sync. Uses the active engagement
    as the filter automatically."""
    cfg = _api_safe("GET", "/sync/config")
    if not cfg or not cfg.get("configured"):
        return

    eng = _engagement()
    ok, missing = _engagement_valid_for_sync()

    with st.sidebar:
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
                    resp = _api("POST", "/sync", json={"filter": eng})
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
    with st.sidebar:
        st.header("📊 Loaded data")
        st.caption("Scoped to the active engagement.")

        periods = _api_safe("GET", "/periods") or []
        periods = [p for p in periods if _matches_active_engagement(p)]
        if not periods:
            st.info("No data for this engagement yet. Sync from DocumentDB or upload a CTB.")
        else:
            st.subheader("Periods")
            for p in periods:
                period_lbl = f"{p.get('fin_year_id')} · {p.get('reporting_period_id')}"
                st.write(f"**{period_lbl}** — {p['row_count']:,} rows · {p['entity_count']} entities")

        st.divider()
        st.header("📜 Recent ingestions")
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

        if st.button("🔄 Refresh"):
            st.rerun()


# ---------- Excel upload ----------


def render_upload() -> None:
    st.subheader("Upload a Consolidated Trial Balance")
    st.caption(
        "The file is ingested under the active engagement above. "
        "Re-uploading for the same engagement replaces the previous data; other engagements are untouched."
    )
    file = st.file_uploader(
        "CTB Excel file",
        type=["xlsx", "xlsb", "xls"],
        help="Single sheet, headers in row 1, 22 columns matching the expected CTB layout.",
    )

    ok, missing = _engagement_valid_for_sync()
    if file and not ok:
        st.warning(f"Fill in the engagement fields above first: {', '.join(missing)}")
    elif file and st.button("Upload & ingest", type="primary"):
        eng = _engagement()
        files = {"file": (file.name, file.getvalue(), file.type or "application/octet-stream")}
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                r = client.post(
                    f"{API}/upload",
                    files=files,
                    data=eng,
                    headers=_auth_headers(),
                )
                r.raise_for_status()
                resp = r.json()
            st.success(
                f"Upload queued: `{resp['upload_id']}`. Ingestion runs in background — refresh the sidebar to see status."
            )
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


def render_chat() -> None:
    if "chat" not in st.session_state:
        st.session_state.chat = []

    st.subheader("Ask a question")
    st.caption("Answers are scoped to the active engagement. Clear FY or Reporting Period above to ask cross-period (YoY) questions.")
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
    st.set_page_config(page_title="ctb-copilot", layout="wide", page_icon="📊")
    st.title("ctb-copilot")
    st.caption("Multi-tenant Q&A over consolidated trial balance data. Every answer shows its SQL and source rows.")
    _prefill_engagement_from_query_params()
    render_engagement_form()
    render_docdb_sync()
    render_sidebar()
    render_upload()
    st.divider()
    render_chat()


def run() -> None:
    """Entry point for `uv run ctb-ui`. Launches Streamlit on this script."""
    here = Path(__file__).resolve()
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(here)], check=False)


if __name__ == "__main__":
    main()
