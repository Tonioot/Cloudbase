# Cloudbase

**Original Cloudbase project by Tonioot (2026)**

Cloudbase is a self-hosted deployment and operations platform for managing applications across multiple servers.

It turns a Git repository into running Docker containers on one or more of your own servers, puts NGINX in front of them, and gives you one panel to deploy, scale, route and observe everything.

> Full documentation is built into the panel under **Guides** (`/guides`). This README is a summary.

> **Early-stage project.** Cloudbase is new and is not a professional or managed service. It comes without any guarantee of uptime, support or fitness for a particular purpose, and you use it at your own risk. See [Disclaimer](#disclaimer).

---

## Philosophy

Cloudbase is designed with a focus on simplicity and clarity.

The system prioritizes:

* A minimal and understandable operational model
* Explicit control over infrastructure behavior
* Predictable deployment and scaling semantics
* Avoidance of unnecessary complexity in orchestration

The goal is to keep multi-server application management transparent and easy to reason about.

---

## Features

**Apps & builds**

* Web services, static sites and background workers
* Uses your own `Dockerfile`, or generates one for Node.js, Python, Ruby, Go, PHP or a generic image
* Separate build and start commands, Docker build caching
* Encrypted environment variables, `.env` import, available during the build
* Private repositories through saved GitHub tokens

**Deploys**

* Rolling and blue/green deploys with health checks and automatic rollback on failure
* Auto-deploy on push: polls the branch every 1, 5 or 15 minutes — no webhook needed
* Deploy history with full logs and one-click rollbacks to any earlier commit

**Instances & scaling**

* Run any number of instances of an app, spread over any node
* Restart policies, crash-loop protection and start on boot
* CPU-based autoscaling with a minimum and maximum
* Per-instance CPU and memory limits, read-only root filesystem

**Networking**

* NGINX load balancing over every healthy instance
* Domain wizard with live DNS check and free, self-renewing Let's Encrypt certificates
* Automatic app subdomains (`<app>.apps.example.com`) with HTTPS per app
* Panel domain with HTTPS, redirects, `www` aliases, Cloudflare support
* Downtime, update, starting and restart pages — template or custom HTML

**Operations**

* Live logs per instance, build and deploy output
* CPU, memory, network and disk metrics with seven days of history
* Activity per app and a global audit log
* Users and custom roles with fine-grained permissions
* App export/import and full CLI backups

---

## How it works

Cloudbase has four building blocks.

| Concept  | What it is |
| -------- | ---------- |
| Primary  | The server running the panel. Stores all configuration, runs NGINX and is the single entry point for traffic and for you. It is also a node itself. |
| Node     | Any server that runs app containers. Extra servers join as remote nodes, controlled by a small agent. |
| App      | A definition: repository, build and start command, port, environment variables, domains, resource limits. |
| Instance | One running copy of an app — one Docker container on one node. |

### From repository to container

The primary clones the repository to `~/.cloudbase/apps/<name>` and builds a Docker image. Remote nodes download the source from the primary and build the same image locally, so they don't need access to your Git host.

### How a request reaches your app

```
Visitor ─► NGINX on the primary ─► instance on the primary (127.0.0.1:10000–19999)
                               └─► tunnel port (127.0.0.1:20000–29999) ─► agent on a node ─► instance
```

All public traffic enters through NGINX on the primary, which spreads requests over every healthy instance. The agent on a remote node keeps an outbound WebSocket to the primary and opens a tunnel per instance, so remote nodes need no open inbound ports, public IP or VPN.

### Node outages

When a node goes offline, its instances are marked `node offline` and taken out of the load balancer; instances elsewhere keep serving. When it reconnects, surviving containers are reattached and missing ones are started again — without manual action.

---

## Setup

### Requirements

* Linux with systemd (Ubuntu, Debian, Fedora, RHEL, Arch, openSUSE)
* Root access (`sudo`)
* Ports 80 and 443 reachable from the internet for domains and HTTPS

The installer adds Python 3, Git, Docker, NGINX and certbot if they're missing.

### Installation

```bash
curl -fsSL https://raw.githubusercontent.com/Tonioot/Cloudbase/main/install.sh | sudo bash
```

The installer checks the system, asks whether the server becomes the primary or a node, and puts the code in `/opt/cloudbase`. Cloudbase runs as the user that ran `sudo`, with its data in that user's `~/.cloudbase`. It starts on boot.

Prefer a clone? `git clone https://github.com/Tonioot/Cloudbase && sudo bash Cloudbase/install.sh` installs from that directory instead. Run `install.sh --help` for all options.

### After installation

1. Store the admin password the installer shows.
2. Open the panel at `http://<server-ip>:7823` and log in.
3. Connect a domain to the panel under **Settings → Domain & SSL** for HTTPS, then close port 7823.
4. Optionally set up app subdomains, so every app gets an address with HTTPS automatically.

### Adding a node

Click **Add node** in the panel and run the command it shows on the new server. It installs Cloudbase and connects it in one go:

```bash
curl -fsSL https://raw.githubusercontent.com/Tonioot/Cloudbase/main/install.sh | sudo bash -s -- --node --main-url https://panel.example.com --invite-code <code>
```

A node only runs the agent: no panel, no nginx, no open ports. To turn a node into a primary later, run `cloudbase disconnect` on it.

### Updating

```bash
cloudbase update
```

Run it on the primary and on every remote node. Running apps keep running.

---

## CLI

```bash
# Service
cloudbase start | stop | restart | status | logs
cloudbase enable                 # systemd service + start on boot
cloudbase disable
cloudbase update
cloudbase uninstall [--keep-data]

# Information
cloudbase apps
cloudbase nodes
cloudbase password               # set a new admin password

# Nodes
cloudbase connect --main-url <url> --invite-code <code> [--node-name <name>]
cloudbase disconnect             # leave the primary and become a primary
cloudbase node-status

# NGINX
cloudbase nginx <domain>
cloudbase nginx show
cloudbase nginx disable
cloudbase nginx permissions [user]

# Certificates
cloudbase cert add <path> [name]
cloudbase cert list
cloudbase cert path

# Backup and restore (database + credentials)
cloudbase export [file]
cloudbase import <file>
```

---

## Data storage

| Path                        | Contains |
| --------------------------- | -------- |
| `~/.cloudbase/config.yaml`  | Ports, limits, session length |
| `~/.cloudbase/apps/`        | App sources, cloned from Git |
| `~/.cloudbase/logs/`        | Log files, including the node agent's |
| `~/.cloudbase/`             | Also the database and credentials — include it in server backups |

Cloudbase listens on port `7823`. Instances use host ports `10000–19999`, tunnels use `20000–29999` (local only). All are configurable under **Settings → System**.

---

## Technology stack

* Python control plane, agent and CLI
* Docker for building and isolating instances
* NGINX for routing, TLS termination and load balancing
* certbot / Let's Encrypt for certificates
* Git for source retrieval and deployments

---

## Disclaimer

Cloudbase is a new, independent project in active development. It is not a professional, commercial or managed hosting service.

* **No guarantees.** Cloudbase is provided "as is". There is no guarantee that it works correctly, stays available, or is free of bugs or security issues.
* **No liability.** The authors and contributors are not responsible for any damage or loss caused by using Cloudbase, including downtime, data loss, security incidents or costs.
* **No support obligation.** Help is given on a best-effort basis, without any promised response time.
* **Your responsibility.** You operate your own servers and apps. Keep independent backups of your data, test updates before relying on them, and review Cloudbase yourself before using it for anything important.

This comes on top of the warranty disclaimer and limitation of liability in the license (sections 15 and 16 of the AGPL-3.0).

---

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).

You are free to use, modify, and distribute this software under the terms of the license.
If you modify and distribute the software, you must publish your changes.
If you run it as a service, you must provide source code access to users.

See the LICENSE file for full terms.

---

## Attribution

Cloudbase was originally created by Tonioot.

If you use or modify this project, you must retain attribution and preserve the original copyright notice.

This repository is considered the canonical upstream source of Cloudbase.
