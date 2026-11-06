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

log = logging.getLogger("cloudbase.docker")

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


_network_lock = threading.Lock()


def ensure_network(name: str) -> None:
    """Create the user-defined bridge network if it does not exist yet."""
    import docker  # type: ignore
    client = _get_client()
    with _network_lock:
        try:
            client.networks.get(name)
        except docker.errors.NotFound:
            # A fixed bridge name makes firewall rules for the database proxies simple.
            client.networks.create(
                name, driver="bridge", labels={"cloudbase.managed": "true"},
                options={"com.docker.network.bridge.name": f"{name[:10]}0"},
            )
            log.info("[docker] Created network %s", name)


def _host_port(external_port: int, docker_options: dict):
    """Docker port binding: on localhost only when docker_options asks for it."""
    bind_host = (docker_options or {}).get("bind_host")
    return (bind_host, external_port) if bind_host else external_port


def _apply_linked_databases(run_kwargs: dict, docker_options: dict) -> None:
    """Attach the container to the database network and add the URLs of linked
    databases: direct on the database's node, through a local proxy elsewhere.
    The app's own env vars win when a name is set in both places."""
    linked = docker_options.get("linked_databases") or []
    if linked:
        import db_proxy
        linked_env, hosts = db_proxy.resolve_links(linked)
        run_kwargs["environment"] = {**linked_env, **(run_kwargs.get("environment") or {})}
        if hosts:
            run_kwargs["extra_hosts"] = hosts
    network = docker_options.get("network")
    if network:
        ensure_network(network)
        run_kwargs["network"] = network


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
{build_step}{expose}CMD {cmd_json}
""",
    "python": """\
FROM python:3.11-slim
WORKDIR /app
{python_copy_req}COPY . .
{build_step}{expose}CMD {cmd_json}
""",
    "ruby": """\
FROM ruby:3.2-slim
WORKDIR /app
{ruby_copy_gemfile}COPY . .
{build_step}{expose}CMD {cmd_json}
""",
    "go": """\
FROM golang:1.22-alpine AS builder
WORKDIR /app
{go_copy_mod}COPY . .
{go_build}
FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/app .
{expose}CMD ["./app"]
""",
    "php": """\
FROM php:8.2-cli
WORKDIR /app
COPY . .
{build_step}{expose}CMD {cmd_json}
""",
    "unknown": """\
FROM ubuntu:22.04
WORKDIR /app
RUN apt-get update && apt-get install -y curl wget && rm -rf /var/lib/apt/lists/*
COPY . .
{build_step}{expose}CMD {cmd_json}
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

# ── BuildKit ──────────────────────────────────────────────────────────────────
# BuildKit skips the intermediate container the classic builder starts for every
# instruction (each ARG/ENV/WORKDIR cost a container + layer commit), builds
# independent stages in parallel and supports persistent cache mounts, so package
# downloads survive across builds. It needs the docker CLI with the buildx plugin.
# Set CLOUDBASE_BUILDKIT=0 to force the classic builder.

_buildkit_available: Optional[bool] = None


def buildkit_available() -> bool:
    global _buildkit_available
    if _buildkit_available is not None:
        return _buildkit_available
    result = False
    if os.environ.get("CLOUDBASE_BUILDKIT", "1").strip().lower() not in ("0", "false", "no", "off"):
        try:
            result = subprocess.run(
                ["docker", "buildx", "version"],
                capture_output=True, timeout=15,
            ).returncode == 0
        except Exception:
            result = False
    _buildkit_available = result
    log.info("[docker] BuildKit %s", "enabled" if result else "unavailable, using the classic builder")
    return result


_BUILD_SECRET_ID = "buildenv"


def _run(cmd: str, cache_dirs=()) -> str:
    """`RUN cmd`, with persistent cache mounts when BuildKit is available."""
    mounts = "".join(f"--mount=type=cache,target={d} " for d in cache_dirs) if buildkit_available() else ""
    return f"RUN {mounts}{cmd}\n"


def _build_step(build_command: str, build_env_keys) -> str:
    """Dockerfile lines that run the app's build once, at image build time.

    App env vars are exposed to the build as ARGs (values are passed as build
    args) because frameworks read some of them while building, e.g. Next.js
    NEXT_PUBLIC_*. Only names end up in the Dockerfile.
    """
    cmd = (build_command or "").strip()
    if not cmd:
        return ""
    keys = [k for k in (build_env_keys or ()) if _ARG_NAME_RE.match(k)]
    # Next.js keeps its incremental compile cache in .next/cache
    cache = ("/app/.next/cache",) if "next" in cmd.lower() else ()
    if not keys or not buildkit_available():
        # Without BuildKit there is no leak-free way to hand env vars to the
        # build (ARG values end up in the image history), so none are passed.
        return _run(cmd, cache)
    # The env vars arrive as a BuildKit secret file: never stored in a layer,
    # the image history or the process list. Secrets are not part of the cache
    # key, so CB_ENV_HASH (a hash, not the values) re-runs the step on change.
    mounts = "".join(f"--mount=type=cache,target={d} " for d in cache)
    return (
        "ARG CB_ENV_HASH\n"
        f"RUN --mount=type=secret,id={_BUILD_SECRET_ID} {mounts}"
        f". /run/secrets/{_BUILD_SECRET_ID} && ( {cmd} )\n"
    )


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
        "COPY package*.json ./\n" + _run(_npm_install_cmd(_has_file), ("/root/.npm",))
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
    # 0 = no port: workers and cron jobs serve nothing, so the image declares none
    no_port = port == 0
    port = port or 8000
    expose = "" if no_port else f"EXPOSE {port}\n"
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
            "COPY package*.json ./\n" + _run(npm_install, ("/root/.npm",))
            if _has_file("package.json") else ""
        ),
        "python_copy_req": (
            "COPY requirements.txt .\n" + (
                _run("pip install -r requirements.txt", ("/root/.cache/pip",))
                if buildkit_available()
                else _run("pip install --no-cache-dir -r requirements.txt")
            )
            if _has_file("requirements.txt") else ""
        ),
        "ruby_copy_gemfile": (
            "COPY Gemfile* ./\nRUN bundle install\n"
            if _has_file("Gemfile") else ""
        ),
        "go_copy_mod": (
            f"{go_mod_line}\n" + _run("go mod download", ("/go/pkg/mod",)) if go_mod_line else ""
        ),
        "go_build": _run("go build -o app .", ("/go/pkg/mod", "/root/.cache/go-build")),
    }

    return GENERATED_MARKER + template.format(expose=expose, cmd_json=_cmd_to_json(start_command), **extras)


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
coverage
*.log
.DS_Store
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

def _declared_build_args(dockerfile_path: str) -> set[str]:
    """Names declared with `ARG NAME` / `ARG NAME=default` in a Dockerfile."""
    names: set[str] = set()
    try:
        with open(dockerfile_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[0].upper() == "ARG":
                    names.add(parts[1].split("=", 1)[0].strip())
    except OSError:
        pass
    return names


# Process variables a build arg must never overwrite: the docker CLI reads them.
_PROTECTED_ENV = {
    "PATH", "HOME", "USERPROFILE", "APPDATA", "TEMP", "TMP", "SYSTEMROOT",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "XDG_RUNTIME_DIR",
}


def _env_hash(build_env: dict) -> str:
    """Stable fingerprint of the build env. A hash is all that reaches the image."""
    import hashlib
    blob = json.dumps(sorted(build_env.items()), ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _write_secret_file(build_env: dict) -> str:
    """Env vars as `export K='v'` lines in a 0600 temp file (BuildKit secret source)."""
    import shlex
    import tempfile
    fd, path = tempfile.mkstemp(prefix="cb-buildenv-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for k, v in build_env.items():
            f.write(f"export {k}={shlex.quote(v)}\n")
    return path


def _build_with_cli(
    app_id: int, app_dir: str, img: str, push_line_fn,
    buildargs=None, labels=None, secret_env=None,
) -> None:
    """Build through `docker build` so BuildKit does the work; stream its plain log.

    Secret values never appear on the command line: env vars go in as a BuildKit
    secret file and ARGs of a repository's own Dockerfile are read from the
    process environment (`--build-arg NAME` without a value).
    """
    cmd = ["docker", "build", "--progress=plain", "-t", img]
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    for k, v in (buildargs or {}).items():
        if k == "CB_ENV_HASH":
            cmd += ["--build-arg", f"{k}={v}"]   # a hash, not sensitive
        elif k in _PROTECTED_ENV or k.startswith("DOCKER_"):
            log.warning("[docker] Skipping build arg %s: name is reserved", k)
        else:
            cmd += ["--build-arg", k]
            env[k] = v
    for k, v in (labels or {}).items():
        cmd += ["--label", f"{k}={v}"]

    secret_path = None
    try:
        if secret_env:
            secret_path = _write_secret_file(secret_env)
            cmd += ["--secret", f"id={_BUILD_SECRET_ID},src={secret_path}"]
        cmd.append(app_dir)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env,
        )
        tail: deque = deque(maxlen=15)
        for raw in proc.stdout:
            line = raw.rstrip()
            if line:
                tail.append(line)
                push_line_fn(app_id, f"[Docker build] {line}")
        if proc.wait() != 0:
            raise RuntimeError(f"docker build failed (exit {proc.returncode}): " + " | ".join(list(tail)[-5:]))
    finally:
        if secret_path:
            try:
                os.remove(secret_path)
            except OSError:
                pass


def _run_build(
    app_id: int, app_dir: str, img: str, push_line_fn,
    buildargs=None, labels=None, secret_env=None,
) -> None:
    if buildkit_available():
        try:
            return _build_with_cli(app_id, app_dir, img, push_line_fn, buildargs, labels, secret_env)
        except FileNotFoundError:
            pass  # docker CLI vanished: fall back to the classic builder below
    client = _get_client()
    # Use the low-level API here: it streams build output incrementally and
    # avoids the docker-py 7.x high-level decode issues around dict chunks.
    log_stream = client.api.build(
        path=app_dir,
        tag=img,
        rm=True,
        forcerm=True,
        decode=False,
        buildargs=buildargs or None,
        labels=labels or None,
    )
    for event in _iter_build_events(log_stream):
        _emit_build_event(app_id, event, push_line_fn)


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
    source_revision: str | None = None,
) -> str:
    """Build Docker image, streaming build output via push_line_fn. Returns image tag.

    build_command runs once inside the image build (e.g. `npm run build`);
    build_env reaches the build as a BuildKit secret (not as build args, which
    would be readable in the image history).
    source_revision is stored as the `cloudbase.source_revision` label, which
    node agents compare to decide whether an image is still current.
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

        # Only pass build args the Dockerfile declares: Docker warns about (and
        # prints the names of) every unused one, which leaks all env var names.
        declared = _declared_build_args(os.path.join(app_dir, "Dockerfile"))
        buildargs = {k: v for k, v in build_env.items() if k in declared}

        # Generated Dockerfiles read the env from a BuildKit secret instead of
        # ARGs, so the values stay out of the image history and layers.
        use_secret = bool(build_env) and buildkit_available()
        if "CB_ENV_HASH" in declared:
            buildargs["CB_ENV_HASH"] = _env_hash(build_env)
        if build_env and not use_secret and not buildargs:
            push_line_fn(
                app_id,
                "[Docker] Build env vars are not passed to the build: BuildKit "
                "(docker buildx) is required to do that without leaking them.",
            )

        _cleanup_before_build(app_id, push_line_fn)
        push_line_fn(app_id, f"[Docker] Building image {img} …")
        _run_build(
            app_id, app_dir, img, push_line_fn,
            buildargs=buildargs,
            labels={"cloudbase.source_revision": source_revision} if source_revision else None,
            secret_env=build_env if use_secret else None,
        )
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
    if internal_port and external_port and docker_options.get("publish_port", True):
        port_bindings[f"{internal_port}/tcp"] = _host_port(external_port, docker_options)

    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": port_bindings,
        # setuid binaries (sudo, su) inside the image cannot raise privileges
        "security_opt": ["no-new-privileges:true"],
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

    _apply_linked_databases(run_kwargs, docker_options)
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


# ── Disk cleanup ──────────────────────────────────────────────────────────────
# Every rebuild retags `:latest`, so the previous image is left behind untagged
# ("dangling"), and BuildKit keeps a build cache that only grows. Neither is
# needed to run or restart an app: apps start from their tagged image, which is
# never touched here. Volumes (database data) are never touched either.

_cleanup_lock = threading.Lock()
BUILD_CACHE_KEEP_BYTES = 5 * 1024 ** 3          # routine: cache above this is evicted, oldest first
BUILD_CACHE_KEEP_BYTES_LOW_DISK = 1 * 1024 ** 3  # disk (almost) full
CLEANUP_PRE_BUILD_DISK_PERCENT = 85.0            # clean first when a build starts on a fuller disk


def disk_percent() -> Optional[float]:
    try:
        import psutil
        return float(psutil.disk_usage("/").percent)
    except Exception:
        return None


def _fmt_bytes(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def cleanup_docker(aggressive: bool = False, own_app_id: Optional[int] = None) -> dict:
    """Remove superseded images and trim the build cache. Returns a summary.

    Routine: untagged images older than a day, build cache above 5 GB.
    aggressive (disk nearly full): all untagged images, build cache above 1 GB.
    Skipped while another app is building, so a running build never loses layers.
    """
    if not _cleanup_lock.acquire(blocking=False):
        return {"skipped": "A cleanup is already running"}
    try:
        with _build_locks_guard:
            busy = [a for a, lk in _build_locks.items() if lk.locked() and a != own_app_id]
        if busy:
            return {"skipped": "A build is in progress"}

        client = _get_client()
        disk_before = disk_percent()
        result = {"images_removed": 0, "images_bytes": 0, "cache_bytes": 0, "errors": []}

        try:
            filters = {"dangling": True}
            if not aggressive:
                filters["until"] = "24h"
            pruned = client.images.prune(filters=filters)
            result["images_removed"] = len(pruned.get("ImagesDeleted") or [])
            result["images_bytes"] = int(pruned.get("SpaceReclaimed") or 0)
        except Exception as e:
            result["errors"].append(f"images: {e}")

        try:
            keep = BUILD_CACHE_KEEP_BYTES_LOW_DISK if aggressive else BUILD_CACHE_KEEP_BYTES
            try:
                pruned = client.api.prune_builds(keep_storage=keep)
            except TypeError:  # older docker-py without keep_storage
                pruned = client.api.prune_builds()
            result["cache_bytes"] = int(pruned.get("SpaceReclaimed") or 0)
        except Exception as e:
            result["errors"].append(f"build cache: {e}")

        result["disk_percent_before"] = disk_before
        result["disk_percent_after"] = disk_percent()
        result["summary"] = (
            f"Removed {result['images_removed']} old image layer(s) "
            f"({_fmt_bytes(result['images_bytes'])}) and {_fmt_bytes(result['cache_bytes'])} of build cache"
        )
        log.info("[docker] Cleanup%s: %s", " (low disk)" if aggressive else "", result["summary"])
        return result
    finally:
        _cleanup_lock.release()


def _cleanup_before_build(app_id: int, push_line_fn) -> None:
    """Free space first when the disk is nearly full, so the build doesn't fail on it."""
    try:
        pct = disk_percent()
        if pct is None or pct < CLEANUP_PRE_BUILD_DISK_PERCENT:
            return
        push_line_fn(app_id, f"[Docker] Disk is {pct:.0f}% full, cleaning up old images and build cache first…")
        res = cleanup_docker(aggressive=True, own_app_id=app_id)
        push_line_fn(app_id, f"[Docker] {res.get('summary') or res.get('skipped') or 'Cleanup done'}.")
    except Exception as e:
        log.warning("[docker] Pre-build cleanup failed: %s", e)


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
    if internal_port and external_port and docker_options.get("publish_port", True):
        port_bindings[f"{internal_port}/tcp"] = _host_port(external_port, docker_options)

    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": port_bindings,
        # setuid binaries (sudo, su) inside the image cannot raise privileges
        "security_opt": ["no-new-privileges:true"],
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

    _apply_linked_databases(run_kwargs, docker_options)
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
                push_line_fn(app_id, "[Replica] Container name still releasing, retrying in 2 s…")
                try:
                    client.containers.get(cname).remove(force=True)
                except Exception:
                    pass
                _time.sleep(2)
            else:
                raise
    ports = f" (:{external_port} → :{internal_port})" if port_bindings else " (no port: background worker)"
    push_line_fn(app_id, f"[Replica] Container {replica_id} started: {container.short_id}{ports}")
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


# ── Cron jobs ─────────────────────────────────────────────────────────────────
# A run is one container that does its work and exits; it is removed afterwards.

JOB_OUTPUT_LIMIT = 64 * 1024  # bytes of output kept per run (the end of it)


def job_container_name(app_id: int, run_id: int) -> str:
    return f"cloudbase-cron-{app_id}-{run_id}"


def image_exists(img: str) -> bool:
    import docker  # type: ignore
    try:
        _get_client().images.get(img)
        return True
    except docker.errors.ImageNotFound:
        return False


def run_job_container(
    app_id: int,
    run_id: int,
    app_name: str,
    img: str,
    command: str,
    env_vars: dict,
    docker_options: dict | None,
    timeout_seconds: int,
    on_line=None,
) -> dict:
    """Run one cron job to completion. Blocking (call from a thread).

    Returns {"exit_code", "output", "timed_out"}; output is the last
    JOB_OUTPUT_LIMIT bytes. An empty command runs the image's own CMD.
    """
    import time as _time
    client = _get_client()
    docker_options = docker_options or {}
    cname = job_container_name(app_id, run_id)
    remove_container_by_name(cname)

    run_kwargs = {
        "detach": True,
        "name": cname,
        "security_opt": ["no-new-privileges:true"],
        "environment": env_vars,
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.app_name": app_name,
            "cloudbase.cron_run_id": str(run_id),
        },
    }
    if command:
        run_kwargs["command"] = json.loads(_cmd_to_json(command))
    if docker_options.get("cpu_limit"):
        run_kwargs["nano_cpus"] = int(float(docker_options["cpu_limit"]) * 1_000_000_000)
    if docker_options.get("memory_limit_mb"):
        run_kwargs["mem_limit"] = int(docker_options["memory_limit_mb"]) * 1024 * 1024
    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True
    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        if docker_options.get("tmpfs_size_mb"):
            tmpfs_opts.append(f"size={int(docker_options['tmpfs_size_mb'])}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}
    _apply_linked_databases(run_kwargs, docker_options)
    _assert_image_local(client, img)

    container = client.containers.run(img, **run_kwargs)
    output = bytearray()
    timed_out = False
    deadline = _time.monotonic() + max(1, int(timeout_seconds or 3600))

    def _follow():
        pending = b""
        try:
            for chunk in container.logs(stream=True, follow=True):
                output.extend(chunk)
                if len(output) > JOB_OUTPUT_LIMIT * 2:
                    del output[:len(output) - JOB_OUTPUT_LIMIT]
                if on_line:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        on_line(line.decode("utf-8", "replace").rstrip("\r"))
        except Exception:
            pass
        if on_line and pending:
            on_line(pending.decode("utf-8", "replace"))

    follower = threading.Thread(target=_follow, daemon=True)
    follower.start()
    try:
        exit_code = None
        while exit_code is None:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    container.stop(timeout=10)
                except Exception:
                    pass
                break
            try:
                exit_code = container.wait(timeout=min(remaining, 30)).get("StatusCode")
            except Exception:
                container.reload()
                if container.status in ("exited", "dead"):
                    exit_code = container.attrs.get("State", {}).get("ExitCode")
        if exit_code is None:
            try:
                container.reload()
                exit_code = container.attrs.get("State", {}).get("ExitCode")
            except Exception:
                pass
        follower.join(timeout=5)
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
    text = bytes(output[-JOB_OUTPUT_LIMIT:]).decode("utf-8", "replace")
    return {"exit_code": exit_code, "output": text, "timed_out": timed_out}


def list_job_containers() -> list[str]:
    """Names of cron job containers on this machine (left over after a restart)."""
    client = _get_client()
    return [c.name for c in client.containers.list(all=True) if c.name.startswith("cloudbase-cron-")]
