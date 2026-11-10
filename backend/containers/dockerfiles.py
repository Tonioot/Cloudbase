import json
import logging
import os
import re
import subprocess
from containers.buildkit import _BUILD_SECRET_ID, _run, buildkit_available

log = logging.getLogger("cloudbase.docker")


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
# maintenance pages and instances keep working unchanged.
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
