import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from typing import Optional

import config as _cfg

log = logging.getLogger("pdm.docker")

# ── Lazy Docker client ────────────────────────────────────────────────────────

_docker_client = None
_docker_lock = threading.Lock()
_build_locks: dict[int, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _get_client():
    global _docker_client
    if _docker_client is not None:
        return _docker_client
    with _docker_lock:
        if _docker_client is not None:
            return _docker_client
        import docker # type: ignore
        _docker_client = docker.from_env()
        return _docker_client


def _assert_image_local(client, img: str) -> None:
    """Raise RuntimeError if the image is not present locally, preventing docker-py
    from silently falling through to a Docker Hub pull attempt."""
    import docker # type: ignore
    try:
        client.images.get(img)
    except docker.errors.ImageNotFound:
        raise RuntimeError(
            f"Image '{img}' not found locally. Deploy the app first to build the image."
        )


def _get_build_lock(app_id: int) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(app_id)
        if lock is None:
            lock = threading.Lock()
            _build_locks[app_id] = lock
        return lock


def is_docker_available() -> bool:
    try:
        client = _get_client()
        client.ping()
        return True
    except Exception:
        return False


# ── Container naming ──────────────────────────────────────────────────────────

def container_name(app_id: int) -> str:
    return f"cloudbase-app-{app_id}"


def image_name(app_id: int, app_name: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]", "-", app_name.lower())
    return f"cloudbase/{safe}-{app_id}:latest"


def _stringify_build_error(error) -> str:
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message") or error.get("error") or error.get("detail")
        if message:
            return str(message)
        try:
            return json.dumps(error, ensure_ascii=False)
        except Exception:
            return str(error)
    try:
        return str(error)
    except Exception:
        return "Unknown Docker build error"


def _iter_build_events(log_stream):
    decoder = json.JSONDecoder()
    buffer = ""

    for raw in log_stream:
        if raw is None:
            continue
        if isinstance(raw, dict):
            yield raw
            continue

        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw)

        if not text:
            continue

        buffer += text
        while buffer:
            buffer = buffer.lstrip()
            if not buffer:
                break
            try:
                chunk, idx = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                newline_idx = buffer.find("\n")
                if newline_idx == -1:
                    break
                line = buffer[:newline_idx].strip()
                buffer = buffer[newline_idx + 1 :]
                if line:
                    yield {"stream": line}
                continue
            yield chunk
            buffer = buffer[idx:]

    trailing = buffer.strip()
    if trailing:
        try:
            yield decoder.raw_decode(trailing)[0]
        except Exception:
            yield {"stream": trailing}


def _emit_build_event(app_id: int, event: dict, push_line_fn) -> None:
    if "stream" in event:
        text = str(event["stream"]).rstrip()
        if text:
            push_line_fn(app_id, f"[Docker build] {text}")
        return

    if "error" in event or "errorDetail" in event:
        error = event.get("error") or event.get("errorDetail")
        raise RuntimeError(_stringify_build_error(error))

    status = event.get("status")
    if status:
        parts = [str(status)]
        if event.get("id"):
            parts.append(str(event["id"]))
        if event.get("progress"):
            parts.append(str(event["progress"]))
        push_line_fn(app_id, f"[Docker build] {' '.join(parts)}")


def _is_loopback_bind_log(line: str, internal_port: Optional[int]) -> bool:
    low = (line or "").lower()
    if "127.0.0.1" not in low and "localhost" not in low:
        return False

    markers = (
        "running on",
        "listening on",
        "started server",
        "server running",
        "uvicorn running",
    )
    if not any(m in low for m in markers):
        return False

    if internal_port is None:
        return True
    return f":{internal_port}" in low


# ── Dockerfile templates ──────────────────────────────────────────────────────

# First line of every Dockerfile Cloudbase writes. A Dockerfile without it
# belongs to the repository and is never overwritten.
GENERATED_MARKER = "# cloudbase:generated - edits are overwritten; commit your own Dockerfile to take over\n"

_DOCKERFILES: dict[str, str] = {
    "nodejs": """\
FROM node:20-alpine
WORKDIR /app
{nodejs_copy_pkg}COPY . .
{build_step}EXPOSE {port}
CMD {cmd_json}
""",
    "python": """\
FROM python:3.11-slim
WORKDIR /app
{python_copy_req}COPY . .
{build_step}EXPOSE {port}
CMD {cmd_json}
""",
    "ruby": """\
FROM ruby:3.2-slim
WORKDIR /app
{ruby_copy_gemfile}COPY . .
{build_step}EXPOSE {port}
CMD {cmd_json}
""",
    "go": """\
FROM golang:1.22-alpine AS builder
WORKDIR /app
{go_copy_mod}COPY . .
RUN go build -o app .

FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/app .
EXPOSE {port}
CMD ["./app"]
""",
    "php": """\
FROM php:8.2-cli
WORKDIR /app
COPY . .
{build_step}EXPOSE {port}
CMD {cmd_json}
""",
    "unknown": """\
FROM ubuntu:22.04
WORKDIR /app
RUN apt-get update && apt-get install -y curl wget && rm -rf /var/lib/apt/lists/*
COPY . .
{build_step}EXPOSE {port}
CMD {cmd_json}
""",
}


def _cmd_to_json(cmd: str) -> str:
    """Convert a shell command string to a JSON array for Dockerfile CMD."""
    import shlex
    # Exec-form CMD has no shell, so `a && b` would pass "&&" as a literal
    # argument to `a`. Commands with shell syntax must run through /bin/sh.
    if re.search(r"[;&|<>$`*?(){}\\]", cmd):
        return json.dumps(["/bin/sh", "-c", cmd])
    try:
        parts = shlex.split(cmd)
        return json.dumps(parts)
    except Exception:
        return json.dumps(["/bin/sh", "-c", cmd])


def _normalize_docker_start_command(app_type: str, start_command: str, port: int) -> str:
    """Best-effort normalization so containerized web apps bind on 0.0.0.0."""
    cmd = (start_command or "").strip()
    if not cmd:
        return cmd

    if app_type == "python":
        lower = cmd.lower()

        if "uvicorn" in lower:
            cmd = re.sub(r"--host(?:=|\s+)127\.0\.0\.1", "--host 0.0.0.0", cmd, flags=re.IGNORECASE)
            cmd = re.sub(r"--host(?:=|\s+)localhost", "--host 0.0.0.0", cmd, flags=re.IGNORECASE)
            if "--host" not in cmd:
                cmd += " --host 0.0.0.0"

        if "flask run" in lower and "--host" not in lower:
            cmd += " --host=0.0.0.0"

    return cmd


# Static sites: nginx serves the files directly. Cloudbase's own nginx routes
# to this container like any other app, so start/stop, health checks,
# maintenance pages and replicas keep working unchanged.
STATIC_INTERNAL_PORT = 80
_STATIC_DIR_CANDIDATES = ("dist", "build", "out", "public", "site", "www", "docs", "_site")
_STATIC_NGINX_CONF = (
    "server {",
    "    listen 80;",
    "    root /usr/share/nginx/html;",
    "    index index.html index.htm;",
    "    gzip on;",
    "    gzip_types text/css application/javascript application/json image/svg+xml;",
    "    location ~ /\\.(?!well-known) { deny all; }",
    "    location / { try_files $uri $uri/ $uri.html /index.html; }",
    "}",
)


def resolve_static_dir(app_dir: str, configured: str = "", built: bool = False) -> str:
    """Return the repo-relative directory to publish ("." for the repo root).

    An explicit value wins; otherwise use the root if it has an index.html,
    else the first common build/output folder that does. When a build command
    produces the output (built=True) the folder does not exist yet, so fall
    back to "dist" instead of publishing the whole repo.
    """
    cleaned = (configured or "").strip().replace("\\", "/").strip("/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned and cleaned != ".":
        if ".." in cleaned.split("/"):
            raise ValueError("Publish directory must stay inside the repository")
        return cleaned
    if cleaned == ".":
        return "."
    if not built and (not app_dir or os.path.exists(os.path.join(app_dir, "index.html"))):
        return "."
    for candidate in _STATIC_DIR_CANDIDATES:
        if os.path.exists(os.path.join(app_dir, candidate, "index.html")):
            return candidate
    return "dist" if built else "."


def _npm_install_cmd(has_file) -> str:
    # Include devDependencies: frameworks like Next.js need typescript etc.
    # for `next build` and next.config.ts.
    if has_file("package-lock.json"):
        return "npm ci --no-audit --no-fund || npm install --no-audit --no-fund"
    return "npm install --no-audit --no-fund"


_ARG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _build_step(build_command: str, build_env_keys) -> str:
    """Dockerfile lines that run the app's build once, at image build time.

    App env vars are exposed to the build as ARGs (values are passed as build
    args) because frameworks read some of them while building, e.g. Next.js
    NEXT_PUBLIC_*. Only names end up in the Dockerfile.
    """
    cmd = (build_command or "").strip()
    if not cmd:
        return ""
    keys = sorted(k for k in (build_env_keys or ()) if _ARG_NAME_RE.match(k))
    arg_lines = "".join(f"ARG {k}\n" for k in keys)
    return f"{arg_lines}RUN {cmd}\n"


def _generate_static_dockerfile(app_dir: str, publish_dir: str, build_command: str = "", build_env_keys=()) -> str:
    built = bool((build_command or "").strip())
    src = resolve_static_dir(app_dir, publish_dir, built=built)
    conf_args = " ".join(f"'{line}'" for line in _STATIC_NGINX_CONF)
    nginx_stage = (
        "FROM nginx:alpine\n"
        f"RUN printf '%s\\n' {conf_args} > /etc/nginx/conf.d/default.conf\n"
    )
    if not built:
        return (
            nginx_stage
            + f"COPY {src}/ /usr/share/nginx/html/\n"
            + f"EXPOSE {STATIC_INTERNAL_PORT}\n"
        )

    # Multi-stage: build with Node, ship only the output folder in nginx.
    def _has_file(name: str) -> bool:
        return bool(app_dir) and os.path.exists(os.path.join(app_dir, name))

    install = (
        f"COPY package*.json ./\nRUN {_npm_install_cmd(_has_file)}\n"
        if _has_file("package.json") else ""
    )
    return (
        "FROM node:20-alpine AS builder\n"
        "WORKDIR /app\n"
        f"{install}"
        "COPY . .\n"
        f"{_build_step(build_command, build_env_keys)}"
        "\n"
        + nginx_stage
        + f"COPY --from=builder /app/{src}/ /usr/share/nginx/html/\n"
        + f"EXPOSE {STATIC_INTERNAL_PORT}\n"
    )


def generate_dockerfile(
    app_type: str,
    start_command: str,
    port: int,
    app_dir: str = "",
    build_command: str = "",
    build_env_keys=(),
) -> str:
    if app_type == "static":
        # For static sites start_command holds the publish directory.
        return GENERATED_MARKER + _generate_static_dockerfile(app_dir, start_command, build_command, build_env_keys)

    template = _DOCKERFILES.get(app_type, _DOCKERFILES["unknown"])
    port = port or 8000
    start_command = _normalize_docker_start_command(app_type, start_command, port)

    def _has_file(name: str) -> bool:
        return bool(app_dir) and os.path.exists(os.path.join(app_dir, name))

    go_mod_line = "COPY go.mod go.sum ./" if (_has_file("go.mod") and _has_file("go.sum")) else (
        "COPY go.mod ./" if _has_file("go.mod") else ""
    )
    npm_install = _npm_install_cmd(_has_file)
    extras: dict[str, str] = {
        "build_step": _build_step(build_command, build_env_keys),
        "nodejs_copy_pkg": (
            f"COPY package*.json ./\nRUN {npm_install}\n"
            if _has_file("package.json") else ""
        ),
        "python_copy_req": (
            "COPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n"
            if _has_file("requirements.txt") else ""
        ),
        "ruby_copy_gemfile": (
            "COPY Gemfile* ./\nRUN bundle install\n"
            if _has_file("Gemfile") else ""
        ),
        "go_copy_mod": (
            f"{go_mod_line}\nRUN go mod download\n" if go_mod_line else ""
        ),
    }

    return GENERATED_MARKER + template.format(port=port, cmd_json=_cmd_to_json(start_command), **extras)


_DEFAULT_DOCKERIGNORE = """\
# Generated by Cloudbase — keeps the build context small so builds start fast.
.git
Dockerfile
.dockerignore
node_modules
.next
__pycache__
*.pyc
.venv
venv
"""


def ensure_dockerignore(app_dir: str) -> None:
    """Write a default .dockerignore unless the repo already ships its own.

    Without it the whole directory (including .git and host node_modules) is
    sent to the Docker daemon on every build, and a host node_modules would
    overwrite the one installed inside the image by `COPY . .`.
    """
    path = os.path.join(app_dir, ".dockerignore")
    if os.path.exists(path):
        return
    with open(path, "w") as f:
        f.write(_DEFAULT_DOCKERIGNORE)
    log.info("[docker] Generated .dockerignore at %s", path)


def _git_tracks(app_dir: str, rel_path: str) -> bool:
    if not os.path.isdir(os.path.join(app_dir, ".git")):
        return False
    try:
        res = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel_path],
            cwd=app_dir, capture_output=True, text=True, timeout=10,
        )
        return res.returncode == 0
    except Exception:
        return False


def has_custom_dockerfile(app_dir: str) -> bool:
    """True when the repo ships its own Dockerfile, which Cloudbase must not touch.

    Generated files carry GENERATED_MARKER. A Dockerfile without it is the
    user's when git tracks it, or when there is no .git at all (remote nodes
    receive source without .git; the primary already regenerated any stale
    generated Dockerfile — with marker — before packaging the source).
    Untracked files without the marker are legacy generated ones.
    """
    path = os.path.join(app_dir, "Dockerfile")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if f.read(len(GENERATED_MARKER)) == GENERATED_MARKER:
                return False
    except OSError:
        return False
    if not os.path.isdir(os.path.join(app_dir, ".git")):
        return True
    return _git_tracks(app_dir, "Dockerfile")


def ensure_dockerfile(
    app_dir: str,
    app_type: str,
    start_command: str,
    port: int,
    build_command: str = "",
    build_env_keys=(),
) -> str:
    """Write (or regenerate) the auto-managed Dockerfile in app_dir. Returns the path.

    A Dockerfile shipped in the repo is used as-is.
    """
    ensure_dockerignore(app_dir)
    dockerfile_path = os.path.join(app_dir, "Dockerfile")
    if has_custom_dockerfile(app_dir):
        log.info("[docker] Using the repository's own Dockerfile at %s", dockerfile_path)
        return dockerfile_path

    content = generate_dockerfile(
        app_type, start_command, port, app_dir=app_dir,
        build_command=build_command, build_env_keys=build_env_keys,
    )

    existing = ""
    if os.path.exists(dockerfile_path):
        with open(dockerfile_path, "r") as f:
            existing = f.read()

    if existing == content:
        log.info("[docker] Dockerfile unchanged at %s", dockerfile_path)
        return dockerfile_path

    with open(dockerfile_path, "w") as f:
        f.write(content)
    action = "Updated" if existing else "Generated"
    log.info("[docker] %s Dockerfile for %s app at %s", action, app_type, dockerfile_path)
    return dockerfile_path


# ── Build ─────────────────────────────────────────────────────────────────────

def build_image(
    app_id: int,
    app_name: str,
    app_dir: str,
    push_line_fn,
    app_type: str = "unknown",
    start_command: str = "",
    port: int = 8000,
    build_command: str = "",
    build_env: dict | None = None,
) -> str:
    """Build Docker image, streaming build output via push_line_fn. Returns image tag.

    build_command runs once inside the image build (e.g. `npm run build`);
    build_env is passed as build args so the build can read app env vars.
    """
    build_lock = _get_build_lock(app_id)
    if not build_lock.acquire(blocking=False):
        msg = "Docker build already in progress for this app"
        push_line_fn(app_id, f"[Docker] {msg}.")
        raise RuntimeError(msg)

    img = image_name(app_id, app_name)
    build_env = {k: str(v) for k, v in (build_env or {}).items() if _ARG_NAME_RE.match(str(k))}
    try:
        ensure_dockerfile(
            app_dir, app_type, start_command, port,
            build_command=build_command, build_env_keys=build_env.keys(),
        )
        if has_custom_dockerfile(app_dir):
            push_line_fn(app_id, "[Docker] Using the Dockerfile from the repository.")

        push_line_fn(app_id, f"[Docker] Building image {img} …")
        client = _get_client()
        # Use the low-level API here: it streams build output incrementally and
        # avoids the docker-py 7.x high-level decode issues around dict chunks.
        log_stream = client.api.build(
            path=app_dir,
            tag=img,
            rm=True,
            forcerm=True,
            decode=False,
            buildargs=build_env or None,
        )
        for event in _iter_build_events(log_stream):
            _emit_build_event(app_id, event, push_line_fn)
    except RuntimeError:
        raise
    except Exception as e:
        push_line_fn(app_id, f"[Docker] Build failed: {e}")
        raise RuntimeError(str(e)) from e
    finally:
        build_lock.release()

    push_line_fn(app_id, f"[Docker] Image built: {img}")
    return img


def _restart_policy_config(policy: str | None) -> dict:
    policy = (policy or "no").strip().lower()
    if policy == "always":
        return {"Name": "always"}
    if policy == "unless-stopped":
        return {"Name": "unless-stopped"}
    if policy == "on-failure":
        return {"Name": "on-failure", "MaximumRetryCount": 5}
    return {"Name": "no"}


def _replica_restart_policy_config(docker_options: dict) -> dict:
    """Replica containers always use 'unless-stopped' so Docker auto-restarts them
    after a node reboot. Explicit 'always' or 'on-failure' from docker_options is honoured."""
    policy = (docker_options.get("restart_policy") or "").strip().lower()
    if policy in ("always", "on-failure"):
        return _restart_policy_config(policy)
    return {"Name": "unless-stopped"}


# ── Port allocation ───────────────────────────────────────────────────────────

EXTERNAL_PORT_START = _cfg.get_port("instance_min")
EXTERNAL_PORT_END   = _cfg.get_port("instance_max")


def pick_free_external_port(used_ports: set[int]) -> int:
    """Return the lowest unused host port in the configured instance range that is also free on the OS."""
    import psutil
    try:
        active = {c.laddr.port for c in psutil.net_connections(kind="inet") if c.laddr}
    except Exception:
        active = set()

    for p in range(EXTERNAL_PORT_START, EXTERNAL_PORT_END + 1):
        if p not in used_ports and p not in active:
            return p
    raise RuntimeError(
        f"No free external port available in range {EXTERNAL_PORT_START}–{EXTERNAL_PORT_END}"
    )


# ── Run / start ───────────────────────────────────────────────────────────────

def run_container(
    app_id: int,
    app_name: str,
    img: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None,
    push_line_fn,
) -> str:
    """Create and start a container. Maps external_port → internal_port. Returns container ID."""
    client = _get_client()
    cname = container_name(app_id)
    docker_options = docker_options or {}

    # Remove any leftover container with the same name
    try:
        old = client.containers.get(cname)
        old.remove(force=True)
        push_line_fn(app_id, "[Docker] Removed old container.")
    except Exception:
        pass

    port_bindings = {}
    if internal_port and external_port:
        port_bindings[f"{internal_port}/tcp"] = external_port

    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": port_bindings,
        "environment": env_vars,
        "restart_policy": _restart_policy_config(docker_options.get("restart_policy")),
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.app_name": app_name,
            "cloudbase.internal_port": str(internal_port),
            "cloudbase.external_port": str(external_port),
        },
    }

    cpu_limit = docker_options.get("cpu_limit")
    if cpu_limit:
        run_kwargs["nano_cpus"] = int(float(cpu_limit) * 1_000_000_000)

    memory_limit_mb = docker_options.get("memory_limit_mb")
    if memory_limit_mb:
        run_kwargs["mem_limit"] = int(memory_limit_mb) * 1024 * 1024

    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True

    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        tmpfs_size_mb = docker_options.get("tmpfs_size_mb")
        if tmpfs_size_mb:
            tmpfs_opts.append(f"size={int(tmpfs_size_mb)}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}

    _assert_image_local(client, img)
    container = client.containers.run(
        img,
        **run_kwargs,
    )
    push_line_fn(app_id, f"[Docker] Container started: {container.short_id} (:{external_port} → :{internal_port})")
    return container.id


# ── Stop / remove ─────────────────────────────────────────────────────────────

def stop_container(app_id: int, push_line_fn=None) -> bool:
    client = _get_client()
    cname = container_name(app_id)
    try:
        c = client.containers.get(cname)
        c.stop(timeout=10)
        c.remove()
        if push_line_fn:
            push_line_fn(app_id, "[Docker] Container stopped and removed.")
        return True
    except Exception as e:
        if push_line_fn:
            push_line_fn(app_id, f"[Docker] Stop error: {e}")
        return False


def remove_image(app_id: int, app_name: str) -> None:
    client = _get_client()
    img = image_name(app_id, app_name)
    try:
        client.images.remove(img, force=True)
        log.info("[docker] Removed image %s", img)
    except Exception:
        pass


# ── Status ────────────────────────────────────────────────────────────────────

def is_container_running(app_id: int) -> bool:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        c.reload()
        return c.status == "running"
    except Exception:
        return False


def get_container_id(app_id: int) -> Optional[str]:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        return c.id
    except Exception:
        return None


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_container_stats(app_id: int) -> dict:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        raw = c.stats(stream=False)

        # CPU %
        cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw["precpu_stats"].get("system_cpu_usage", 0)
        num_cpus = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1])
        cpu_percent = (cpu_delta / system_delta * num_cpus * 100.0) if system_delta > 0 else 0.0

        # Memory — cgroups v2: usage may be 0; fall back to anon+file from stats dict
        mem_stats = raw.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_inner = mem_stats.get("stats", {})
        if mem_usage == 0 and mem_inner:
            # cgroups v2: anon = anonymous pages (true RSS), file = page cache
            rss = mem_inner.get("anon", 0)
            mem_usage = rss + mem_inner.get("file", 0)
            log.info("app_id=%d cgroups-v2 fallback: anon=%d file=%d rss=%d",
                     app_id, rss, mem_inner.get("file", 0), rss)
        else:
            mem_cache = mem_inner.get("inactive_file", mem_inner.get("cache", 0))
            rss = max(mem_usage - mem_cache, 0)

        c.reload()
        started_at = c.attrs.get("State", {}).get("StartedAt", "")
        uptime = 0
        if started_at:
            import datetime
            try:
                started = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                uptime = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
            except Exception:
                pass

        # Network I/O (cumulative since container start)
        networks = raw.get("networks") or {}
        net_rx = sum(v.get("rx_bytes", 0) for v in networks.values())
        net_tx = sum(v.get("tx_bytes", 0) for v in networks.values())

        # Disk I/O (cumulative block bytes)
        blkio = raw.get("blkio_stats", {})
        io_list = blkio.get("io_service_bytes_recursive") or []
        disk_read  = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "read")
        disk_write = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "write")

        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(rss / 1024 / 1024, 2),
            "memory_vms_mb": round(mem_usage / 1024 / 1024, 2),
            "uptime_seconds": uptime,
            "status": c.status,
            "num_threads": 0,
            "num_connections": 0,
            "net_rx_mb": round(net_rx / 1024 / 1024, 2),
            "net_tx_mb": round(net_tx / 1024 / 1024, 2),
            "disk_read_mb": round(disk_read / 1024 / 1024, 2),
            "disk_write_mb": round(disk_write / 1024 / 1024, 2),
        }
    except Exception:
        log.warning("get_container_stats failed for app_id=%d", app_id, exc_info=True)
        return {}


# ── Log streaming ─────────────────────────────────────────────────────────────

def attach_container_log_tailer(
    app_id: int,
    log_buffers: dict,
    push_line_fn,
    main_loop,
    cname: Optional[str] = None,
) -> None:
    """Stream container logs to the in-memory buffer in a background thread."""
    if app_id not in log_buffers:
        log_buffers[app_id] = deque(maxlen=5000)

    resolved_cname = cname or container_name(app_id)

    def _reader():
        try:
            for _ in range(40):
                try:
                    _get_client().containers.get(resolved_cname)
                    break
                except Exception:
                    pass
                time.sleep(0.25)

            client = _get_client()
            c = client.containers.get(resolved_cname)
            labels = (c.attrs.get("Config", {}) or {}).get("Labels", {}) or {}
            raw_internal_port = labels.get("cloudbase.internal_port")
            internal_port = int(raw_internal_port) if str(raw_internal_port or "").isdigit() else None
            warned_loopback_bind = False
            # Seed an empty buffer with recent history; otherwise only follow new
            # output, so re-attaching never replays the whole container log again.
            seed = 300 if not log_buffers[app_id] else 0
            for raw in c.logs(stream=True, follow=True, timestamps=False, tail=seed):
                line = raw.decode("utf-8", errors="replace").rstrip()
                log_buffers[app_id].append(line)
                if main_loop and not main_loop.is_closed():
                    main_loop.call_soon_threadsafe(
                        lambda l=line: None  # push_line_fn called below
                    )
                push_line_fn(app_id, line)
                if not warned_loopback_bind and _is_loopback_bind_log(line, internal_port):
                    warned_loopback_bind = True
                    target = f"port {internal_port}" if internal_port else "the exposed app port"
                    push_line_fn(
                        app_id,
                        f"[Docker] Warning: app appears to bind to localhost/127.0.0.1 inside the container for {target}. "
                        "Use host 0.0.0.0 so Docker port mapping and nginx can reach it.",
                    )
        except Exception as e:
            log.debug("[docker] Log tailer ended for app %d: %s", app_id, e)

    threading.Thread(target=_reader, daemon=True).start()


def get_recent_container_logs(app_id: int, lines: int = 300) -> list[str]:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        raw = c.logs(tail=lines, timestamps=False)
        return [l.decode("utf-8", errors="replace").rstrip() for l in raw.splitlines()]
    except Exception:
        return []


def get_recent_container_logs_by_name(container_name_str: str, lines: int = 300) -> list[str]:
    try:
        client = _get_client()
        c = client.containers.get(container_name_str)
        raw = c.logs(tail=lines, timestamps=False)
        return [l.decode("utf-8", errors="replace").rstrip() for l in raw.splitlines()]
    except Exception:
        return []


# ── Blue-green / zero-downtime helpers ───────────────────────────────────────

def slot_image_name(app_id: int, app_name: str, slot: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]", "-", app_name.lower())
    return f"cloudbase/{safe}-{app_id}:{slot}"


def slot_container_name(app_id: int, slot: str) -> str:
    return f"cloudbase-app-{app_id}-{slot}"


def build_image_for_slot(
    app_id: int,
    app_name: str,
    app_dir: str,
    push_line_fn,
    app_type: str,
    start_command: str,
    port: int,
    slot: str,
) -> str:
    """Build image tagged with the given slot name. Returns image tag."""
    build_lock = _get_build_lock(app_id)
    if not build_lock.acquire(blocking=False):
        raise RuntimeError("Docker build already in progress for this app")
    img = slot_image_name(app_id, app_name, slot)
    try:
        ensure_dockerfile(app_dir, app_type, start_command, port)
        push_line_fn(app_id, f"[ZD] Building image {img} for slot {slot}…")
        client = _get_client()
        log_stream = client.api.build(path=app_dir, tag=img, rm=True, forcerm=True, decode=False)
        for event in _iter_build_events(log_stream):
            _emit_build_event(app_id, event, push_line_fn)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(str(e)) from e
    finally:
        build_lock.release()
    push_line_fn(app_id, f"[ZD] Image built: {img}")
    return img


def run_container_for_slot(
    app_id: int,
    app_name: str,
    slot: str,
    img: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None,
    push_line_fn,
) -> str:
    """Start a slot container on a temporary port. Returns container ID."""
    client = _get_client()
    cname = slot_container_name(app_id, slot)
    docker_options = docker_options or {}
    try:
        old = client.containers.get(cname)
        old.remove(force=True)
    except Exception:
        pass
    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": {f"{internal_port}/tcp": external_port},
        "environment": env_vars,
        "restart_policy": {"Name": "no"},
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.app_name": app_name,
            "cloudbase.slot": slot,
        },
    }
    cpu_limit = docker_options.get("cpu_limit")
    if cpu_limit:
        run_kwargs["nano_cpus"] = int(float(cpu_limit) * 1_000_000_000)
    memory_limit_mb = docker_options.get("memory_limit_mb")
    if memory_limit_mb:
        run_kwargs["mem_limit"] = int(memory_limit_mb) * 1024 * 1024
    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True
    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        if docker_options.get("tmpfs_size_mb"):
            tmpfs_opts.append(f"size={int(docker_options['tmpfs_size_mb'])}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}
    _assert_image_local(client, img)
    container = client.containers.run(img, **run_kwargs)
    push_line_fn(app_id, f"[ZD] Slot container started: {container.short_id} on :{external_port}")
    return container.id


def stop_slot_container(app_id: int, slot: str) -> None:
    client = _get_client()
    cname = slot_container_name(app_id, slot)
    try:
        c = client.containers.get(cname)
        c.stop(timeout=10)
        c.remove()
    except Exception:
        pass


# ── Pull (rebuild on git pull) ────────────────────────────────────────────────

def rebuild_image(
    app_id: int,
    app_name: str,
    app_dir: str,
    push_line_fn,
    app_type: str = "unknown",
    start_command: str = "",
    port: int = 8000,
) -> str:
    """Remove old image and build fresh. Returns new image tag."""
    remove_image(app_id, app_name)
    return build_image(app_id, app_name, app_dir, push_line_fn, app_type, start_command, port)


# ── Replica helpers ───────────────────────────────────────────────────────────

def replica_container_name(app_id: int, replica_id: int) -> str:
    return f"cloudbase-app-{app_id}-replica-{replica_id}"


def run_replica_container(
    app_id: int,
    replica_id: int,
    app_name: str,
    img: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None,
    push_line_fn,
) -> str:
    """Start a replica container. Returns container ID."""
    client = _get_client()
    cname = replica_container_name(app_id, replica_id)
    docker_options = docker_options or {}
    import time as _time
    import docker as _docker_mod  # type: ignore

    try:
        old = client.containers.get(cname)
        old.reload()
        if old.status == "running":
            # Container survived a node outage and is still healthy — reuse it
            push_line_fn(app_id, f"[Replica] Container {replica_id} already running, reusing (recovered from outage).")
            return old.id
        old.remove(force=True)
        push_line_fn(app_id, f"[Replica] Removed old container for replica {replica_id}.")
        # Wait for Docker to fully release the container name and port bindings
        for _ in range(10):
            try:
                client.containers.get(cname)
                _time.sleep(0.5)
            except Exception:
                break
    except Exception:
        pass

    port_bindings = {}
    if internal_port and external_port:
        port_bindings[f"{internal_port}/tcp"] = external_port

    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": port_bindings,
        "environment": env_vars,
        "restart_policy": _replica_restart_policy_config(docker_options),
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.replica_id": str(replica_id),
            "cloudbase.app_name": app_name,
            "cloudbase.internal_port": str(internal_port),
            "cloudbase.external_port": str(external_port),
        },
    }

    cpu_limit = docker_options.get("cpu_limit")
    if cpu_limit:
        run_kwargs["nano_cpus"] = int(float(cpu_limit) * 1_000_000_000)

    memory_limit_mb = docker_options.get("memory_limit_mb")
    if memory_limit_mb:
        run_kwargs["mem_limit"] = int(memory_limit_mb) * 1024 * 1024

    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True

    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        tmpfs_size_mb = docker_options.get("tmpfs_size_mb")
        if tmpfs_size_mb:
            tmpfs_opts.append(f"size={int(tmpfs_size_mb)}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}

    _assert_image_local(client, img)
    # Retry on transient Docker errors: port still releasing or container name
    # still being removed after a force-remove.
    for attempt in (1, 2, 3):
        try:
            container = client.containers.run(img, **run_kwargs)
            break
        except _docker_mod.errors.APIError as exc:
            err = str(exc)
            if attempt == 3:
                raise
            if "port is already allocated" in err:
                push_line_fn(app_id, f"[Replica] Port {external_port} still releasing, retrying in 2 s…")
                _time.sleep(2)
            elif "already in use by container" in err:
                # Previous force-remove hasn't completed yet — wait and retry
                push_line_fn(app_id, f"[Replica] Container name still releasing, retrying in 2 s…")
                try:
                    client.containers.get(cname).remove(force=True)
                except Exception:
                    pass
                _time.sleep(2)
            else:
                raise
    push_line_fn(app_id, f"[Replica] Container {replica_id} started: {container.short_id} (:{external_port} → :{internal_port})")
    return container.id


def stop_replica_container(app_id: int, replica_id: int, push_line_fn=None) -> bool:
    import docker  # type: ignore
    client = _get_client()
    cname = replica_container_name(app_id, replica_id)
    try:
        c = client.containers.get(cname)
        c.stop(timeout=10)
        c.remove()
        if push_line_fn:
            push_line_fn(app_id, f"[Replica] Container {replica_id} stopped and removed.")
        return True
    except docker.errors.NotFound:
        # Container already gone — treat as success.
        return True
    except Exception as e:
        if push_line_fn:
            push_line_fn(app_id, f"[Replica] Stop error for replica {replica_id}: {e}")
        return False


def is_replica_container_running(app_id: int, replica_id: int) -> bool:
    try:
        client = _get_client()
        c = client.containers.get(replica_container_name(app_id, replica_id))
        c.reload()
        return c.status == "running"
    except Exception:
        return False


def get_container_stats_by_name(container_name_str: str) -> dict:
    """Like get_container_stats but accepts an explicit container name instead of app_id."""
    try:
        client = _get_client()
        c = client.containers.get(container_name_str)
        raw = c.stats(stream=False)

        cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw["precpu_stats"].get("system_cpu_usage", 0)
        num_cpus = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1])
        cpu_percent = (cpu_delta / system_delta * num_cpus * 100.0) if system_delta > 0 else 0.0

        mem_stats = raw.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_inner = mem_stats.get("stats", {})
        if mem_usage == 0 and mem_inner:
            rss = mem_inner.get("anon", 0)
            mem_usage = rss + mem_inner.get("file", 0)
        else:
            mem_cache = mem_inner.get("inactive_file", mem_inner.get("cache", 0))
            rss = max(mem_usage - mem_cache, 0)

        c.reload()
        started_at = c.attrs.get("State", {}).get("StartedAt", "")
        uptime = 0
        if started_at:
            import datetime
            try:
                started = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                uptime = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
            except Exception:
                pass

        networks = raw.get("networks") or {}
        net_rx = sum(v.get("rx_bytes", 0) for v in networks.values())
        net_tx = sum(v.get("tx_bytes", 0) for v in networks.values())

        blkio = raw.get("blkio_stats", {})
        io_list = blkio.get("io_service_bytes_recursive") or []
        disk_read  = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "read")
        disk_write = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "write")

        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(rss / 1024 / 1024, 2),
            "memory_vms_mb": round(mem_usage / 1024 / 1024, 2),
            "uptime_seconds": uptime,
            "status": c.status,
            "num_threads": 0,
            "num_connections": 0,
            "net_rx_mb": round(net_rx / 1024 / 1024, 2),
            "net_tx_mb": round(net_tx / 1024 / 1024, 2),
            "disk_read_mb": round(disk_read / 1024 / 1024, 2),
            "disk_write_mb": round(disk_write / 1024 / 1024, 2),
        }
    except Exception as _e:
        try:
            import docker as _docker  # type: ignore
            if isinstance(_e, _docker.errors.NotFound):
                log.debug("get_container_stats_by_name: container not found: %s", container_name_str)
                return {}
        except ImportError:
            pass
        log.warning("get_container_stats_by_name failed for %s", container_name_str, exc_info=True)
        return {}


# ── Orphan cleanup ─────────────────────────────────────────────────────────────

def list_replica_containers() -> list[dict]:
    """Return all local replica containers (running or not) with their parsed
    app_id / replica_id. Used by the orphan cleaner to reconcile against the DB."""
    import re
    client = _get_client()
    pattern = re.compile(r"^cloudbase-app-(\d+)-replica-(\d+)$")
    out: list[dict] = []
    for c in client.containers.list(all=True):
        m = pattern.match(c.name)
        if not m:
            continue
        out.append({
            "name": c.name,
            "app_id": int(m.group(1)),
            "replica_id": int(m.group(2)),
            "status": c.status,
        })
    return out


def remove_container_by_name(cname: str) -> bool:
    """Force-stop and remove a container by name. Returns True if it's gone."""
    import docker  # type: ignore
    client = _get_client()
    try:
        c = client.containers.get(cname)
        c.remove(force=True)
        log.info("[docker] Removed orphan container %s", cname)
        return True
    except docker.errors.NotFound:
        return True
    except Exception:
        log.warning("[docker] Failed to remove container %s", cname, exc_info=True)
        return False
