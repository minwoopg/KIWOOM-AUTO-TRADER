"""One application per state file. OS releases the lock after a crash."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def single_instance_lock(path: str | Path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise RuntimeError(f"자동매매 중복 실행 차단: {path}") from exc
        try:
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        # Do not unlink: a second process may already hold this inode open.
