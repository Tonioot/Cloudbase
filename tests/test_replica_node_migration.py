"""Every instance has a node (the primary is a node too); instances from before that get the primary."""
import sqlite3

from core.database import REPLICAS_WITHOUT_NODE_SQL


def _db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, is_local BOOLEAN)")
    db.execute("CREATE TABLE application_replicas (id INTEGER PRIMARY KEY, node_id INTEGER)")
    return db


def test_instances_without_a_node_get_the_primary_and_the_others_keep_theirs():
    db = _db()
    db.executemany("INSERT INTO nodes VALUES (?, ?)", [(1, 0), (2, 1)])
    db.executemany("INSERT INTO application_replicas VALUES (?, ?)", [(10, None), (11, 1), (12, None)])
    db.execute(REPLICAS_WITHOUT_NODE_SQL)
    assert dict(db.execute("SELECT id, node_id FROM application_replicas")) == {10: 2, 11: 1, 12: 2}


def test_without_a_primary_row_yet_nothing_changes():
    db = _db()
    db.execute("INSERT INTO application_replicas VALUES (10, NULL)")
    db.execute(REPLICAS_WITHOUT_NODE_SQL)
    assert dict(db.execute("SELECT id, node_id FROM application_replicas")) == {10: None}
