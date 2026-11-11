"""The settings in config.yaml that the System settings page may change, and their limits. Also used by a settings
import, so a file can never set what the page could not."""
from fastapi import HTTPException

from core import config as _cfg
from mesh import addressing


def clean_system_settings(body: dict) -> dict:
    """The config.yaml sections the System settings page (and a settings import) may change, within their limits.
    Raises HTTPException(400) on an invalid node network address."""
    def _int(val, lo, hi, default):
        try:
            v = int(val)
            if lo <= v <= hi:
                return v
        except (TypeError, ValueError):
            pass
        return default

    auth_cfg = body.get("auth",   {})
    ports    = body.get("ports",  {})
    limits   = body.get("limits", {})
    wireguard = body.get("wireguard", {})
    sections = {}
    if auth_cfg:
        sections["auth"] = {
            "token_expire_seconds": _int(auth_cfg.get("token_expire_seconds"), 60, 2592000, _cfg.get_auth("token_expire_seconds")),
        }
    if ports:
        sections["ports"] = {
            "instance_min": _int(ports.get("instance_min"), 1024, 65000, _cfg.get_port("instance_min")),
            "instance_max": _int(ports.get("instance_max"), 1024, 65000, _cfg.get_port("instance_max")),
        }
    if limits:
        sections["limits"] = {
            "max_apps":                _int(limits.get("max_apps"),                1, 100000, _cfg.get_limit("max_apps")),
            "max_instances":           _int(limits.get("max_instances"),           1, 100000, _cfg.get_limit("max_instances")),
            "max_nodes":               _int(limits.get("max_nodes"),               1, 10000,  _cfg.get_limit("max_nodes")),
            "max_restarts_per_window": _int(limits.get("max_restarts_per_window"), 1, 1000,   _cfg.get_limit("max_restarts_per_window")),
            "restart_window_seconds":  _int(limits.get("restart_window_seconds"),  10, 86400, _cfg.get_limit("restart_window_seconds")),
        }
    if wireguard:
        endpoint = str(wireguard.get("endpoint") or "").strip()
        if endpoint and not (addressing.is_valid_endpoint(endpoint if ":" in endpoint else endpoint + ":51820")):
            raise HTTPException(400, "The node network address must be a host name or IP address, optionally with a port")
        sections["wireguard"] = {"endpoint": endpoint}
    return sections
