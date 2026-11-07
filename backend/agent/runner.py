"""The node agent as a process of its own: `python node_agent.py`.

A node used to run the whole Cloudbase web app just to host its agent. Now the agent is the process:
no web server, no database, none of the panel's code. It only needs the Docker functions it calls.
"""
import asyncio

from agent import alive
from agent.loop import start_agent
from containers import processes as pm
from core import system_logs


async def run(*registration) -> None:
    """Run the agent for as long as the process lives. `registration` is what start_agent needs to register
    a node that has no saved state yet (main url, invite code, name, public host, heartbeat)."""
    system_logs.setup_logging()
    pm.set_main_loop(asyncio.get_running_loop())      # container log tailers report to this loop
    keep_fresh = asyncio.create_task(alive.keep_fresh())
    try:
        await start_agent(*registration)
    finally:
        keep_fresh.cancel()
        alive.remove()
