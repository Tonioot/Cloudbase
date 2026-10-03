"""Development helpers for running Cloudbase locally (used by scripts/dev.ps1 / dev.sh).

  python dev_seed.py credentials <password>   write the admin password if none exists yet
  python dev_seed.py demo                     add demo apps so every page has something to show

Run with HOME / USERPROFILE pointing at the isolated dev home, so nothing
touches a real ~/.cloudbase.
"""
import asyncio
import json
import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))


def write_credentials(password: str) -> None:
    import auth
    if auth.load_hashed_password():
        return
    auth.save_hashed_password(auth.hash_password(password))
    print(f"[dev] admin password set to: {password}")


async def seed_demo() -> None:
    from sqlalchemy import select
    from database import init_db, AsyncSessionLocal
    from models import Application, ApplicationReplica
    from routers.nodes import ensure_local_node
    from env_crypto import encrypt_env

    await init_db()
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(select(Application.name))).scalars())
        node = await ensure_local_node(db)

        demo = [
            dict(name="technasium-app", repo_url="https://github.com/example/technasium-app", app_type="nodejs",
                 start_command="npm start", build_command="npm run build", port=3000, domain="technasium.example.com",
                 env={"NODE_ENV": "production", "NEXT_PUBLIC_API_URL": "https://api.example.com", "DATABASE_URL": "postgres://demo"},
                 replicas=2),
            dict(name="api-server", repo_url="https://github.com/example/api-server", app_type="python",
                 start_command="uvicorn main:app --host 0.0.0.0 --port 8000", port=8000, domain="api.example.com",
                 env={"LOG_LEVEL": "info"}, replicas=1),
            dict(name="portfolio", repo_url="https://github.com/example/portfolio", app_type="static",
                 start_command="dist", build_command="npm run build", port=80, domain=None, env={}, replicas=1),
            dict(name="discord-bot", repo_url="https://github.com/example/discord-bot", app_type="nodejs",
                 start_command="node bot.js", port=None, domain=None, env={"DISCORD_TOKEN": "demo"},
                 replicas=1, no_web=True),
            # One app per remaining runtime, so every type icon has something to show
            dict(name="rails-shop", repo_url="https://github.com/example/rails-shop", app_type="ruby",
                 start_command="rails server -b 0.0.0.0 -p 3000", port=3000, domain=None, env={}, replicas=1),
            dict(name="go-gateway", repo_url="https://github.com/example/go-gateway", app_type="go",
                 start_command="go run .", port=8080, domain=None, env={}, replicas=1),
            dict(name="php-blog", repo_url="https://github.com/example/php-blog", app_type="php",
                 start_command="php -S 0.0.0.0:8000 -t public", port=8000, domain=None, env={}, replicas=1),
            dict(name="java-orders", repo_url="https://github.com/example/java-orders", app_type="java",
                 start_command="java -jar target/app.jar", port=8080, domain=None, env={}, replicas=1),
            dict(name="dotnet-billing", repo_url="https://github.com/example/dotnet-billing", app_type="dotnet",
                 start_command="dotnet Billing.dll", port=5000, domain=None, env={}, replicas=1),
            dict(name="rust-worker", repo_url="https://github.com/example/rust-worker", app_type="rust",
                 start_command="cargo run --release", port=8080, domain=None, env={}, replicas=1),
            dict(name="custom-image", repo_url="https://github.com/example/custom-image", app_type="unknown",
                 start_command="./run.sh", port=9000, domain=None, env={}, replicas=1),
        ]
        demo = [d for d in demo if d["name"] not in existing]
        if not demo:
            print("[dev] demo apps already present — seed skipped")
            return
        port = 10001 + len(existing) * 2
        for d in demo:
            app = Application(
                name=d["name"], repo_url=d["repo_url"], app_type=d["app_type"],
                start_command=d["start_command"], build_command=d.get("build_command"),
                port=d["port"], domain=d["domain"], nginx_enabled=bool(d["domain"]),
                no_web=d.get("no_web", False), env_vars=encrypt_env(d["env"]),
                extra_domains=json.dumps([]), redirect_domains=json.dumps([]),
                status="stopped", restart_policy="always", source_revision="a1f3c9e",
            )
            db.add(app)
            await db.flush()
            for _ in range(d["replicas"]):
                db.add(ApplicationReplica(app_id=app.id, node_id=node.id,
                                          external_port=None if d.get("no_web") else port, status="stopped"))
                port += 1
        await db.commit()
        print(f"[dev] added {len(demo)} demo apps (stopped — no containers run in dev)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "credentials":
        write_credentials(sys.argv[2])
    elif cmd == "demo":
        asyncio.run(seed_demo())
    else:
        print(__doc__)
        sys.exit(1)
