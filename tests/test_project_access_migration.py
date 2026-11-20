"""An installation from before project_access: whoever was limited by a membership stays limited, nobody else is."""
import sqlalchemy as sa


def test_members_stay_limited_and_everyone_else_keeps_every_project(tmp_path):
    from core import database
    from core import models  # noqa: F401  (registers the tables)

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        database.Base.metadata.create_all(conn)
        conn.exec_driver_sql("ALTER TABLE users DROP COLUMN project_access")          # how it was before
        for user_id, name in ((1, "member"), (2, "free")):
            conn.exec_driver_sql(
                "INSERT INTO users (id, username, password_hash, role, session_version) VALUES (?, ?, 'x', 'Viewer', 0)",
                (user_id, name))
        conn.exec_driver_sql("INSERT INTO roles (id, name) VALUES (1, 'Viewer')")
        conn.exec_driver_sql("INSERT INTO projects (id, name, is_default, apps_connected) VALUES (1, 'P', 0, 0)")
        conn.exec_driver_sql("INSERT INTO project_members (project_id, user_id, role_id) VALUES (1, 1, 1)")

    with engine.begin() as conn:
        database._create_and_upgrade_tables(conn)
        access = dict(conn.exec_driver_sql("SELECT username, project_access FROM users").all())
        assert access == {"member": "selected", "free": "all"}

        # Running it again (the next start) changes nothing
        conn.exec_driver_sql("UPDATE users SET project_access = 'all' WHERE username = 'member'")
        database._create_and_upgrade_tables(conn)
        assert dict(conn.exec_driver_sql("SELECT username, project_access FROM users").all())["member"] == "all"
