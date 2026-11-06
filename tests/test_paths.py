"""Moving modules must not break the files they locate relative to themselves."""
import os

from core import paths


def test_paths_point_at_real_files():
    assert os.path.isfile(os.path.join(paths.BACKEND_DIR, "main.py"))
    assert os.path.isfile(os.path.join(paths.BACKEND_DIR, "node_agent.py"))
    assert os.path.isdir(paths.FRONTEND_DIR) and os.path.isfile(os.path.join(paths.FRONTEND_DIR, "app.html"))
    assert os.path.isfile(paths.LOGO_FILE)
    assert os.path.isfile(paths.REPO_CONFIG_FILE)
    assert os.path.isfile(paths.VERSION_FILE)


def test_agent_state_stays_next_to_the_entry_point():
    # start.sh backs up and restores backend/agent_state.json
    assert os.path.dirname(paths.AGENT_STATE_FILE) == paths.BACKEND_DIR
    assert os.path.basename(paths.AGENT_STATE_FILE) == "agent_state.json"


def test_modules_use_the_shared_paths():
    from core import config
    from notify import mailer
    from agent import state as agent_state
    assert config._REPO_CONFIG_PATH == paths.REPO_CONFIG_FILE
    assert mailer._LOGO_PATH == paths.LOGO_FILE
    assert agent_state.STATE_PATH == paths.AGENT_STATE_FILE
