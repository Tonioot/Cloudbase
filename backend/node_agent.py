"""Entry point of the node agent.

    python node_agent.py                                          run the agent (what the service does)
    python node_agent.py --main-url ... --invite-code ... [--exit-after-registration]   register first

Install.sh and start.sh call this file directly, so it stays at this path. The agent
itself lives in the agent/ package.
"""
import asyncio
import argparse
import logging.handlers
import httpx
from agent.loop import start_agent
from agent.runner import run

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-url")
    parser.add_argument("--invite-code")
    parser.add_argument("--node-name")
    parser.add_argument("--public-host")
    parser.add_argument("--heartbeat-interval", type=int, default=15)
    parser.add_argument("--exit-after-registration", action="store_true")
    args = parser.parse_args()
    try:
        if not args.exit_after_registration:
            await run(args.main_url, args.invite_code, args.node_name, args.public_host, args.heartbeat_interval)
            return
        await start_agent(
            args.main_url,
            args.invite_code,
            args.node_name,
            args.public_host,
            args.heartbeat_interval,
            exit_after_registration=args.exit_after_registration
        )
    except httpx.HTTPStatusError as e:
        # The installer shows the last log lines; make the primary's reason the last one.
        try:
            detail = e.response.json().get("detail") or e.response.text
        except Exception:
            detail = e.response.text
        print(f"The primary refused the registration: {detail}")
        if "already exists" in str(detail):
            print("Choose another name with --node-name <name>, or remove the old node in the panel.")
        elif "used" in str(detail) or "expired" in str(detail):
            print("Click \"Add node\" in the panel again for a new invite code.")
        raise SystemExit(1)
    except httpx.HTTPError as e:
        print(f"Couldn't reach the primary at {args.main_url}: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    # Standalone (e.g. registration from the installer): also print to the console.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(main())
