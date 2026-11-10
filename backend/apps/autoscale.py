"""An app's autoscale settings and their limits, in one place for the settings endpoint and the import.
The autoscaler itself is workers/autoscaler.py."""

# name → (lowest, highest)
LIMITS = {
    "autoscale_min_instances": (1, 100),
    "autoscale_max_instances": (1, 100),
    "autoscale_cpu_target": (10, 95),             # % CPU per instance
    "autoscale_memory_target_mb": (16, 1_048_576),  # MB per instance
    "autoscale_up_seconds": (30, 600),            # scale up when the average over this long is above target
    "autoscale_down_minutes": (1, 60),            # scale down when the peak over this long is well below it
    "autoscale_max_step_up": (1, 100),            # at most this many added per step
    "autoscale_max_step_down": (1, 100),          # at most this many removed per step
}
# 0 (or empty) turns these off: no memory target, no limit per step
OPTIONAL = {"autoscale_memory_target_mb", "autoscale_max_step_up", "autoscale_max_step_down"}
FIELDS = tuple(LIMITS) + ("autoscale_enabled",)


def clean(name: str, value):
    """The value to store: within the limits; None for an optional setting that is turned off."""
    if name == "autoscale_enabled":
        return bool(value)
    if name in OPTIONAL and not value:
        return None
    low, high = LIMITS[name]
    value = max(low, min(high, float(value)))
    return value if name == "autoscale_cpu_target" else int(value)
