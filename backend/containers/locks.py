import threading

_build_locks: dict[int, threading.Lock] = {}


_build_locks_guard = threading.Lock()


def _get_build_lock(app_id: int) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(app_id)
        if lock is None:
            lock = threading.Lock()
            _build_locks[app_id] = lock
        return lock
