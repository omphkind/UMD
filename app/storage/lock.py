"""An OS-released lock protects complete task transactions across processes."""

import os
from pathlib import Path
import threading

from app.core.errors import StorageError, TaskBusyError

_registry: dict[str, "_LockState"] = {}
_registry_guard = threading.Lock()


class _LockState:
    def __init__(self):
        self.mutex = threading.RLock()
        self.depth = 0
        self.stream = None


class FileLock:
    """Non-blocking, reentrant per thread; lock file may safely outlive a run."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        key = os.path.normcase(str(self.path.resolve()))
        with _registry_guard:
            self.state = _registry.setdefault(key, _LockState())
        self.entered = False

    def __enter__(self):
        state = self.state
        if not state.mutex.acquire(blocking=False):
            raise TaskBusyError("Эта задача уже обрабатывается другим процессом или окном UMD.")
        try:
            if state.depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                stream = self.path.open("a+b")
                try:
                    stream.seek(0, os.SEEK_END)
                    if stream.tell() == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    stream.close()
                    raise TaskBusyError("Эта задача уже обрабатывается другим процессом UMD.") from exc
                state.stream = stream
            state.depth += 1
            self.entered = True
            return self
        except Exception as exc:
            state.mutex.release()
            if isinstance(exc, OSError):
                raise StorageError(f"Не удалось заблокировать файл задачи {self.path}.") from exc
            raise

    def __exit__(self, *_):
        if not self.entered:
            return
        state = self.state
        state.depth -= 1
        try:
            if state.depth == 0:
                stream = state.stream
                state.stream = None
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
        finally:
            self.entered = False
            state.mutex.release()
