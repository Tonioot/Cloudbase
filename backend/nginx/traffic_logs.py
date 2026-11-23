"""What nginx sent to the visitors of each app: read from the per-app logs that nginx/config.py turns on.

Each line is the number of bytes of one answer. nginx (as root) appends to app-<id>.log; this module reads what was
added since the last time, from an offset per file. The offset is kept per inode, so a file that is moved aside keeps
its place. A file that gets big is moved to app-<id>.log.old and nginx is told to open a new one (a reload); the old
one is read to its end and removed once nginx no longer writes to it.

The offsets are handed back to the caller, which stores them in the same transaction as the numbers they produced
(workers/traffic.py), so a restart neither loses nor counts a line twice.
"""
import os
import re
import time

from nginx.paths import TRAFFIC_LOG_DIR

ROTATE_BYTES = 32 * 1024 * 1024     # about 5 million requests
OLD_UNUSED_SECONDS = 3600            # nginx workers that serve a long connection keep the old file open until it ends
MAX_READ = 64 * 1024 * 1024          # per file per round, so a huge backlog does not hold the worker up

_FILE = re.compile(r"^app-(\d+)\.log(\.old)?$")


def _read_from(path: str, offset: int) -> tuple[int, int]:
    """(bytes the complete lines from offset add up to, the offset after them)."""
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(MAX_READ)
    end = chunk.rfind(b"\n")
    if end < 0:
        return 0, offset                       # nothing, or a line that nginx is still writing
    total = 0
    for line in chunk[:end].split(b"\n"):
        line = line.strip()
        if line.isdigit():
            total += int(line)
    return total, offset + end + 1


def read_new(offsets: dict[str, int], directory: str = TRAFFIC_LOG_DIR) -> tuple[dict[int, int], dict[str, int], bool]:
    """({app id: bytes} since `offsets`, the new offsets {inode: offset}, whether nginx must open new files).
    Blocking: call from a thread."""
    counts: dict[int, int] = {}
    new_offsets: dict[str, int] = {}
    reopen = False
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return counts, dict(offsets), False
    now = time.time()
    for name in names:
        found = _FILE.match(name)
        if not found:
            continue
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
            inode = str(st.st_ino)
            offset = offsets.get(inode, 0)
            if offset > st.st_size:              # emptied by somebody: start again
                offset = 0
            n, offset = _read_from(path, offset)
        except OSError:
            continue
        app_id = int(found.group(1))
        if n:
            counts[app_id] = counts.get(app_id, 0) + n
        old = bool(found.group(2))
        if old and offset >= st.st_size and now - st.st_mtime > OLD_UNUSED_SECONDS:
            try:
                os.remove(path)                  # read to the end, and nobody writes to it any more
                continue
            except OSError:
                pass
        new_offsets[inode] = offset
        if not old and st.st_size > ROTATE_BYTES and not os.path.exists(path + ".old"):
            try:
                os.rename(path, path + ".old")    # same inode, so its offset stays valid
                reopen = True
            except OSError:
                pass
    return counts, new_offsets, reopen
