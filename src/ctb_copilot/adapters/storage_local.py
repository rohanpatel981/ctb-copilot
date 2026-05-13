import shutil
from pathlib import Path
from typing import BinaryIO


class LocalDiskStorage:
    """v1 storage: writes to a directory on the local filesystem."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, stream: BinaryIO) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            shutil.copyfileobj(stream, f)
        return str(path)

    def local_path(self, key: str) -> Path:
        return self._path(key)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
