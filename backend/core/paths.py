"""Where things live on disk, resolved once. Nothing else should use __file__ for this."""
import os

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(BACKEND_DIR)

FRONTEND_DIR = os.path.join(REPO_DIR, "frontend")
ASSETS_DIR = os.path.join(BACKEND_DIR, "assets")
REPO_CONFIG_FILE = os.path.join(REPO_DIR, "config.yaml")
VERSION_FILE = os.path.join(REPO_DIR, "VERSION")
# Written next to the code (not in ~/.cloudbase): start.sh backs this file up and restores it.
AGENT_STATE_FILE = os.path.join(BACKEND_DIR, "agent_state.json")
# A node has no web server: its agent rewrites this file every few seconds (agent/alive.py), which is how
# start.sh, `cloudbase doctor` and the installer see that it lives.
AGENT_ALIVE_FILE = os.path.expanduser("~/.cloudbase/agent.alive")
LOGO_FILE = os.path.join(ASSETS_DIR, "cloudbase-mark.png")
