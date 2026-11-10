"""Every instance has a node (the primary is a node too); instances from before that get the primary."""
import sqlite3

from core.database import INSTANCES_WITHOUT_NODE_SQL


def _db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, is_local BOOLEAN)")
    db.execute("CREATE TABLE application_instances (id INTEGER PRIMARY KEY, node_id INTEGER)")
    return db


def test_instances_without_a_node_get_the_primary_and_the_others_keep_theirs():
    db = _db()
    db.executemany("INSERT INTO nodes VALUES (?, ?)", [(1, 0), (2, 1)])
    db.executemany("INSERT INTO application_instances VALUES (?, ?)", [(10, None), (11, 1), (12, None)])
    db.execute(INSTANCES_WITHOUT_NODE_SQL)
    assert dict(db.execute("SELECT id, node_id FROM application_instances")) == {10: 2, 11: 1, 12: 2}


def test_without_a_primary_row_yet_nothing_changes():
    db = _db()
    db.execute("INSERT INTO application_instances VALUES (10, NULL)")
    db.execute(INSTANCES_WITHOUT_NODE_SQL)
    assert dict(db.execute("SELECT id, node_id FROM application_instances")) == {10: None}


def test_statuses_from_older_versions_become_starting():
    from core.database import OLD_INSTANCE_STATUSES_SQL
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE application_instances (id INTEGER PRIMARY KEY, status TEXT)")
    db.executemany("INSERT INTO application_instances VALUES (?, ?)", [(1, "restarting"), (2, "deploying"), (3, "running")])
    db.execute(OLD_INSTANCE_STATUSES_SQL)
    assert dict(db.execute("SELECT id, status FROM application_instances")) == {1: "starting", 2: "starting", 3: "running"}
