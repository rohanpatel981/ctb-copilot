"""DatabaseSource port — sync trial-balance rows from an upstream database.

Implementations:
  - DocumentDBSource (v0.3) — AWS DocumentDB / Mongo-compatible

A future PostgresSource or MySQLSource implements the same Protocol so the
sync orchestrator doesn't need to care which DB is on the other end.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol


class SyncProgress:
    """Lightweight callback so adapters can report incremental progress
    without coupling to the ingestions table."""

    def __init__(self, on_batch=None) -> None:
        self._on_batch = on_batch
        self.rows_seen = 0

    def report(self, n: int) -> None:
        self.rows_seen += n
        if self._on_batch is not None:
            self._on_batch(self.rows_seen)


class DatabaseSource(Protocol):
    """Pull canonical-shape trial-balance rows from a source database."""

    def stream_rows(
        self,
        *,
        filter_doc: dict[str, Any],
        batch_size: int,
        progress: SyncProgress | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield canonical-shape row dicts in batches.

        Each yielded element is a LIST of row dicts whose keys match the
        canonical column names in ``db.LLM_SCHEMA_DDL`` (minus the three
        provenance columns upload_id / row_number / period — those are
        added by the orchestrator).

        The adapter should also call ``progress.report(len(batch))`` after
        yielding each batch so the orchestrator can update live row counts.
        """
        ...
