"""Creating a managed database: shared by the create endpoint and by the import."""

import json
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from dbs import engines as db_engines
from core.audit import log_audit
from core.env_crypto import encrypt_text
from core.models import Database, Node
from dbs.ops import _node_reachable, _spawn, _start_operation
from dbs.provision import NAME_RE, _new_credentials, _next_database_id, _validate_limits


async def create_database_now(db: AsyncSession, *, name_in, engine_in, version_in=None, node_id=None,
                              cpu_limit=None, memory_limit_mb=None, actor: str = "system"):
    """Validate, insert and start provisioning. Returns (database, node); raises HTTPException."""
    name = (name_in or "").strip().lower()
    if not NAME_RE.match(name):
        raise HTTPException(400, "Name must be 3–32 characters: lowercase letters, digits and dashes, starting with a letter")
    if name in db_engines.RESERVED_NAMES:
        raise HTTPException(400, f"'{name}' is reserved, pick another name")
    try:
        engine = db_engines.get_engine(engine_in)
    except ValueError as e:
        raise HTTPException(400, str(e))
    version = version_in or engine.versions[0]
    if version not in engine.versions:
        raise HTTPException(400, f"{engine.label} {version} is not offered. Choose one of: {', '.join(engine.versions)}")
    _validate_limits(engine.key, cpu_limit, memory_limit_mb)

    if node_id is not None:
        node = await db.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "Node not found")
    else:
        node = (await db.execute(select(Node).where(Node.is_local == True))).scalar_one_or_none()
        if node is None:
            raise HTTPException(409, "No primary node registered yet")
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline or disabled")

    database = Database(
        id=await _next_database_id(db),
        name=name,
        engine=engine.key,
        version=version,
        node_id=node.id,
        status="creating",
        desired_state="running",
        credentials=encrypt_text(json.dumps(_new_credentials(name, engine.key))),
        cpu_limit=cpu_limit,
        memory_limit_mb=memory_limit_mb,
    )
    db.add(database)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, f"A database named '{name}' already exists")
    await log_audit(db, "database.create", actor=actor, detail={
        "database_id": database.id, "name": name, "engine": engine.key, "version": version, "node": node.name,
    })
    await db.commit()

    _spawn(database.id, lambda: _start_operation(database.id, "ensure"))
    return database, node
