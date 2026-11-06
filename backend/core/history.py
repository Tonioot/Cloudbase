

# ── Cluster & node history (dashboard sparklines, node charts) ────────────────
# Sampled in the background and stored in the DB (7 days), so charts are full
# the moment a page opens and survive a Cloudbase restart.
_OVERVIEW_SAMPLE_SECONDS = 30


_HISTORY_RETENTION_DAYS = 7


_HISTORY_MAX_POINTS = 240   # longer ranges are averaged down to this many points


def _downsample(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """Average consecutive rows into at most _HISTORY_MAX_POINTS buckets."""
    if len(rows) <= _HISTORY_MAX_POINTS:
        return rows
    size = -(-len(rows) // _HISTORY_MAX_POINTS)   # ceil division
    out = []
    for i in range(0, len(rows), size):
        chunk = rows[i:i + size]
        point = {"ts": chunk[-1]["ts"]}
        for k in keys:
            vals = [r[k] for r in chunk if r.get(k) is not None]
            point[k] = round(sum(vals) / len(vals), 1) if vals else None
        out.append(point)
    return out


def _ts_ms(dt) -> int:
    import datetime as _dt
    return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
