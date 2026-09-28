"""Lifetime ownership of a local state path; no application imports."""
import os
from pathlib import Path


class SingleInstance:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.handle = open(self.path, "a+b")
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise SystemExit(f"A dashboard or maintenance process already owns this state file: {self.path}") from exc

    def close(self):
        self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
