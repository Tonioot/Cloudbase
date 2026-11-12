

# key: (label, group, severity). The order is the order in the panel.
EVENTS: dict[str, tuple[str, str, str]] = {
    "deploy.failed":          ("Deploy failed",                "Deploys",   "critical"),
    "deploy.rolled_back":     ("New version rolled back",      "Deploys",   "warning"),
    "deploy.succeeded":       ("Deploy succeeded",             "Deploys",   "info"),
    "instance.failed":        ("Instance crashed or failed",   "Apps",      "warning"),
    "uptime.down":            ("App not reachable",            "Apps",      "critical"),
    "node.offline":           ("Node offline",                 "Nodes",     "critical"),
    "node.disk_high":         ("Disk almost full",             "Nodes",     "warning"),
    "database.down":          ("Database down",                "Databases", "critical"),
    "backup.failed":          ("Backup failed",                "Databases", "critical"),
    "backup.offsite_failed":  ("Off-site upload failed",       "Databases", "warning"),
    "cert.expiring":          ("Certificate not renewing",     "Domains",   "warning"),
    "cron.failed":            ("Cron job failed",              "Cron jobs", "critical"),
    "cron.missed":            ("Cron job skipped a run",       "Cron jobs", "warning"),
}


# Problems: they stay open and send the all-clear when they're fixed
PROBLEM_EVENTS = {"instance.failed", "uptime.down", "node.offline", "node.disk_high", "database.down",
                  "backup.failed", "backup.offsite_failed", "cert.expiring", "cron.failed"}


# Sensible defaults for a new channel
DEFAULT_EVENTS = ["deploy.failed", "deploy.rolled_back", "instance.failed", "uptime.down", "node.offline",
                  "node.disk_high", "database.down", "backup.failed", "backup.offsite_failed", "cert.expiring",
                  "cron.failed", "cron.missed"]


RETENTION_DAYS = 30


_BACKOFF = [30, 120, 600, 1800, 3600]   # seconds before attempt 2, 3, …; then give up


_DISPATCH_IDLE_SECONDS = 10


_WATCH_SECONDS = 30


_DISK_HYSTERESIS = 5.0                 # a disk alert clears 5 points below the threshold


_CERT_CHECK_SECONDS = 6 * 3600
