import json
import logging
import os
import subprocess
from collections import deque
from containers.buildkit import _BUILD_SECRET_ID, buildkit_available
from containers.cleanup import _cleanup_before_build
from containers.client import _get_client
from containers.dockerfiles import _ARG_NAME_RE, _declared_build_args, ensure_dockerfile, has_custom_dockerfile
from containers.locks import _get_build_lock
from containers.naming import image_name

log = logging.getLogger("cloudbase.docker")


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


KEY_LABEL = "cloudbase.build_key"
REVISION_LABEL = "cloudbase.source_revision"
# Files Cloudbase writes into the working directory itself; they say nothing about the user's code
_GENERATED_FILES = ("Dockerfile", ".dockerignore")


def source_revision_of(app_dir: str) -> str | None:
    """The git commit the working directory is at, or None when it is not a clean checkout.

    Files that were edited by hand, or added, are not in any commit: then there is no revision to trust,
    and the image is always built."""
    try:
        head = subprocess.run(["git", "-C", app_dir, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
        if head.returncode != 0:
            return None
        status = subprocess.run(["git", "-C", app_dir, "status", "--porcelain"], capture_output=True, text=True, timeout=30)
        if status.returncode != 0:
            return None
        changed = [line for line in status.stdout.splitlines() if line[3:].strip() not in _GENERATED_FILES]
        return None if changed else head.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def build_key(app_type: str, start_command: str, port: int, build_command: str, build_env: dict | None,
              source_revision: str | None) -> str | None:
    """What an image was made from. The same key means the same image would come out of a build.

    That is the revision of the code plus everything else the build reads: the type, the commands, the port and
    the environment. None when the revision is unknown (then nothing is known to be current)."""
    if not source_revision:
        return None
    import hashlib
    env = {k: str(v) for k, v in (build_env or {}).items() if _ARG_NAME_RE.match(str(k))}
    blob = json.dumps([source_revision, app_type or "", start_command or "", port, build_command or "", _env_hash(env)])
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def image_key(app_id: int, app_name: str) -> str | None:
    """The key the image of this app was built with, or None when there is no such image (or no key on it)."""
    try:
        return (_get_client().images.get(image_name(app_id, app_name)).labels or {}).get(KEY_LABEL)
    except Exception:
        return None


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
    The image is labelled with the revision it was built from and a key of everything the build read
    (build_key); build_image_unless_current compares that key to decide whether a build is needed.
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
        revision = source_revision or source_revision_of(app_dir)
        key = build_key(app_type, start_command, port, build_command, build_env, revision)
        labels = {REVISION_LABEL: revision} if revision else {}
        if key:
            labels[KEY_LABEL] = key
        _run_build(
            app_id, app_dir, img, push_line_fn,
            buildargs=buildargs,
            labels=labels or None,
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


def build_image_unless_current(
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
) -> tuple[str, bool]:
    """Like build_image, but does nothing when the image on this machine was built from the same code and settings.

    Returns (image tag, whether it was built). Docker's own layer cache makes an unchanged build cheaper, but
    not free (the context is sent again, the image is exported again), and an emptied cache makes it as slow
    as a first build. Deploys use this; the Rebuild button uses build_image, which always builds."""
    revision = source_revision or source_revision_of(app_dir)
    key = build_key(app_type, start_command, port, build_command, build_env, revision)
    img = image_name(app_id, app_name)
    if key and image_key(app_id, app_name) == key:
        push_line_fn(app_id, f"[Docker] {img} is current (revision {revision[:8]}, same settings): not rebuilding.")
        return img, False
    build_image(app_id, app_name, app_dir, push_line_fn, app_type, start_command, port,
                build_command=build_command, build_env=build_env, source_revision=revision)
    return img, True
