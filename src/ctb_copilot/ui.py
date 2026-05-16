"""Streamlit UI for ctb-copilot. Talks to FastAPI over HTTP.

v1 layout: sidebar shows ingested periods + entities; main area has an upload
section and a chat. Every answer includes the SQL it ran and the source rows
it used so a CA can verify before pasting into working papers.
"""

import subprocess
import sys
from pathlib import Path

import json
import time

import httpx
import streamlit as st

from ctb_copilot.config import settings
from ctb_copilot.export import export_filename, query_result_to_xlsx

API = settings.api_base_url
TIMEOUT = httpx.Timeout(120.0)

FY_OPTIONS: list[str] = [f"FY {y}-{(y + 1) % 100:02d}" for y in range(2005, 2050)]
DEFAULT_FY = "FY 2024-25"


def _api(method: str, path: str, **kwargs) -> dict | list:
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.request(method, f"{API}{path}", **kwargs)
        r.raise_for_status()
        return r.json()


def _api_safe(method: str, path: str, **kwargs):
    try:
        return _api(method, path, **kwargs)
    except httpx.HTTPError as e:
        st.error(f"API error: {e}")
        return None


def _fmt_number(val) -> str:
    if val is None:
        return "—"
    if isinstance(val, (int, float)):
        return f"{val:,.2f}"
    return str(val)


def _confidence_badge(conf: str) -> str:
    return {"high": "🟢 High", "medium": "🟡 Medium", "low": "🔴 Low"}.get(conf, conf)


def render_docdb_sync() -> None:
    """Sidebar section for DocumentDB sync. Hidden if not configured in .env."""
    cfg = _api_safe("GET", "/sync/config")
    if not cfg or not cfg.get("configured"):
        return

    with st.sidebar:
        with st.expander("🔄 Sync from DocumentDB", expanded=False):
            st.caption(f"Source: `{cfg['database']}.{cfg['collection']}`")

            period = st.selectbox(
                "Period to sync",
                options=FY_OPTIONS,
                index=FY_OPTIONS.index(DEFAULT_FY),
                key="sync-period",
                help="Will be sent as the value of the configured period field.",
            )

            filter_preview = dict(cfg.get("default_filter") or {})
            if cfg.get("period_field"):
                filter_preview[cfg["period_field"]] = period
            if cfg.get("reporting_period") and cfg.get("reporting_period_field"):
                filter_preview[cfg["reporting_period_field"]] = cfg["reporting_period"]

            with st.expander("Filter preview", expanded=False):
                st.code(json.dumps(filter_preview, indent=2), language="json")

            if st.button("🚀 Sync now", key="sync-trigger", type="primary"):
                try:
                    with httpx.Client(timeout=TIMEOUT) as client:
                        r = client.post(f"{API}/sync", json={"period": period, "filter_override": {}})
                        r.raise_for_status()
                        resp = r.json()
                    st.session_state["active_sync_id"] = resp["sync_id"]
                    st.session_state["active_sync_period"] = period
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
                    period_lbl = status.get("period")
                    if s in ("pending", "running"):
                        prog_text = f"Syncing **{period_lbl}**"
                        if rows:
                            prog_text += f" — {rows:,} rows so far…"
                        else:
                            prog_text += " — starting up…"
                        st.info(prog_text)
                        st.progress(0)  # indeterminate; actual % needs a known total
                        time.sleep(2)
                        st.rerun()
                    elif s == "done":
                        st.success(f"✓ Synced {rows:,} rows for {period_lbl}")
                        if st.button("Dismiss", key="dismiss-done"):
                            st.session_state.pop("active_sync_id", None)
                            st.rerun()
                    elif s == "failed":
                        st.error(f"Sync failed: {status.get('error') or 'unknown error'}")
                        if st.button("Dismiss", key="dismiss-failed"):
                            st.session_state.pop("active_sync_id", None)
                            st.rerun()


def render_sidebar() -> None:
    with st.sidebar:
        st.header("📊 Loaded data")

        periods = _api_safe("GET", "/periods") or []
        if not periods:
            st.info("No periods ingested yet. Upload a CTB to get started.")
        else:
            st.subheader("Periods")
            for p in periods:
                st.write(f"**{p['period']}** — {p['row_count']:,} rows · {p['entity_count']} entities")

        entities = _api_safe("GET", "/entities") or []
        if entities:
            st.subheader(f"Entities ({len(entities)})")
            for e in entities:
                st.caption(f"`{e['entity_code']}` — {e['entity_name']}")

        st.divider()
        st.header("📜 Recent uploads")
        uploads = _api_safe("GET", "/uploads") or []
        if not uploads:
            st.caption("No uploads yet.")
        else:
            for u in uploads[:10]:
                icon = {"done": "✓", "failed": "✗", "pending": "⏳", "reading": "⏳", "inserting": "⏳"}.get(u["status"], "•")
                row_text = f" — {u['row_count']:,} rows" if u["row_count"] else ""
                st.caption(f"{icon} **{u['period']}** · {u['filename']}{row_text}")
                if u["status"] == "failed" and u.get("error"):
                    st.caption(f"  error: {u['error']}")

        if st.button("🔄 Refresh"):
            st.rerun()


def render_upload() -> None:
    st.subheader("Upload a Consolidated Trial Balance")
    col1, col2 = st.columns([2, 1])
    with col1:
        file = st.file_uploader(
            "CTB Excel file",
            type=["xlsx", "xlsb", "xls"],
            help="Single sheet, headers in row 1, 22 columns matching the expected CTB layout.",
            label_visibility="collapsed",
        )
    with col2:
        period = st.selectbox(
            "FY period tag",
            options=FY_OPTIONS,
            index=FY_OPTIONS.index(DEFAULT_FY),
            help=(
                "The financial year this CTB represents. "
                "Uploading a file with an FY tag that already exists will REPLACE the previous data for that period."
            ),
            label_visibility="collapsed",
        )

    if file and period and st.button("Upload & ingest", type="primary"):
        files = {"file": (file.name, file.getvalue(), file.type or "application/octet-stream")}
        data = {"period": period}
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                r = client.post(f"{API}/upload", files=files, data=data)
                r.raise_for_status()
                resp = r.json()
            st.success(f"Upload queued: `{resp['upload_id']}`. Ingestion runs in background — refresh the sidebar to see status.")
        except httpx.HTTPError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", "") if e.response else ""
            except Exception:
                pass
            st.error(f"Upload failed: {e}. {detail}")


def render_answer(result: dict, index: int = 0) -> None:
    """Render a single QueryResult dict from POST /query."""
    st.markdown(f"**You** · {result['question']}")
    st.markdown(f"**Assistant** &nbsp;&nbsp; {_confidence_badge(result['confidence'])}")

    # Headline: post-processed metric if any, else the first row's values
    post = result.get("post_process", "none")
    rows = result.get("rows", [])
    columns = result.get("columns", [])

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
        # Show the first 1-3 rows as the "headline"
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


def render_chat() -> None:
    if "chat" not in st.session_state:
        st.session_state.chat = []  # list[dict] of QueryResult shapes

    st.subheader("Ask a question")
    for i, entry in enumerate(st.session_state.chat):
        render_answer(entry, index=i)
        st.divider()

    question = st.chat_input("What's the YoY change in current liabilities?")
    if question:
        with st.spinner("Thinking…"):
            try:
                with httpx.Client(timeout=TIMEOUT) as client:
                    r = client.post(f"{API}/query", json={"question": question})
                    r.raise_for_status()
                    result = r.json()
                st.session_state.chat.append(result)
                st.rerun()
            except httpx.HTTPError as e:
                detail = ""
                try:
                    detail = e.response.json().get("detail", "") if e.response else ""
                except Exception:
                    pass
                st.error(f"Query failed: {e}. {detail}")


def main() -> None:
    st.set_page_config(page_title="ctb-copilot", layout="wide", page_icon="📊")
    st.title("ctb-copilot")
    st.caption("Q&A over consolidated trial balance Excel files. Every answer shows its SQL and source rows.")
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
