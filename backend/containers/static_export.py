"""Taking a static site out of the image it was built in, without running anything.

The image of a static site holds the site in /usr/share/nginx/html (containers/dockerfiles.py). A container is
created from it but never started, and `docker cp` streams that folder as a tar: Docker reads its own image, nothing
of the site runs and nothing is written to this server's disk except the archive. The archive is checked again where
it is unpacked (apps/static_sites.py); here it is only capped in size.
"""
import gzip
import secrets
import subprocess

SITE_PATH = "/usr/share/nginx/html"


class ExportFailed(RuntimeError):
    pass


def export_site(image: str, dest_path: str, max_bytes: int) -> int:
    """Write the site in `image` to `dest_path` as a .tar.gz (top folder: html/). Returns the size of the tar stream."""
    name = f"cloudbase-site-export-{secrets.token_hex(6)}"
    created = subprocess.run(["docker", "create", "--name", name, "--network", "none", image],
                             capture_output=True, text=True)
    if created.returncode != 0:
        raise ExportFailed(f"could not open the image of the site: {(created.stderr or created.stdout).strip()[:300]}")
    try:
        total = 0
        with gzip.open(dest_path, "wb", compresslevel=5) as out:
            proc = subprocess.Popen(["docker", "cp", f"{name}:{SITE_PATH}", "-"], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            try:
                while True:
                    chunk = proc.stdout.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        proc.kill()
                        raise ExportFailed(f"the built site is larger than {max_bytes // (1 << 20)} MB")
                    out.write(chunk)
            finally:
                proc.stdout.close()
                err = proc.stderr.read().decode(errors="replace").strip()
                proc.stderr.close()
                rc = proc.wait()
        if rc != 0:
            if "Could not find" in err or "No such" in err:
                raise ExportFailed(f"the image has no {SITE_PATH}: a Dockerfile of your own must put the site there")
            raise ExportFailed(f"could not copy the site out of the image: {err[:300]}")
        return total
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
