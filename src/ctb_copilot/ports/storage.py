from pathlib import Path
from typing import BinaryIO, Protocol


class StorageBackend(Protocol):
    """Where uploaded CTB files live.

    Implementations: LocalDiskStorage (v1), S3CompatibleStorage (v2 — works for S3, R2, MinIO).
    Business logic must depend on this Protocol, never on a concrete adapter.
    """

    def put(self, key: str, stream: BinaryIO) -> str:
        """Store the stream under `key`. Returns a path/URI usable with `local_path()`."""
        ...

    def local_path(self, key: str) -> Path:
        """Return a filesystem path the ingestion worker can read.

        For local disk this is the file directly. For S3 this implementation
        downloads to a temp file and returns its path. The caller does not
        need to know which.
        """
        ...

    def delete(self, key: str) -> None: ...
