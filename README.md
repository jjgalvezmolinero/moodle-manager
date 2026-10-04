<div align="center">

# Moodle Manager

**Web interface to manage multiple Moodle development instances without touching a single environment variable.**

Built on top of [moodle-docker](https://github.com/moodlehq/moodle-docker) by MoodleHQ.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-required-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docker.com)
[![HTMX](https://img.shields.io/badge/HTMX-2.0-3D72D7?style=flat-square)](https://htmx.org)
[![Vibe Coded](https://img.shields.io/badge/vibe%20coded-Claude%20Sonnet-blueviolet?style=flat-square)](https://anthropic.com)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)](LICENSE)

</div>

---

> **Vibe coding disclosure** — This project was built with [Claude Code](https://claude.ai/code) (Anthropic) using a vibe coding approach: the architecture, features and code were developed through a conversational flow with an AI assistant. The codebase is fully readable and maintained by humans, but I want to be transparent about how it was created.

---

## What is this?

Moodle Manager is a web-based control panel that sits on top of [moodle-docker](https://github.com/moodlehq/moodle-docker). Instead of remembering which environment variables to set, which `.yml` files to combine, or which commands to run, everything is done from a clean browser interface.

It is designed for development teams working with multiple Moodle versions in parallel — different PHP versions, databases, testing configurations — all running simultaneously on the same machine.

**This project does not replace moodle-docker.** It requires it. moodle-docker must be cloned on the host machine and Moodle Manager will use it as the base to build and run all instances.

---

## Dependency: moodle-docker

This project is a wrapper around **[moodle-docker](https://github.com/moodlehq/moodle-docker)**, the official Docker setup for Moodle developers maintained by MoodleHQ.

moodle-docker provides:
- The `moodlehq/moodle-php-apache` Docker images for each PHP version
- A modular set of Compose files (`base.yml`, `db.pgsql.yml`, `service.mail.yml`, etc.)
- The `config.docker-template.php` used to configure each Moodle instance

Moodle Manager reads the path to your local moodle-docker clone and dynamically assembles the right `docker compose -f ...` commands and environment variables for each instance you create.

---

## Features

| Category | Feature |
|---|---|
| **Instances** | Create, edit and delete instances with a guided form |
| **Control** | Start, stop, destroy and restart containers with one click |
| **Monitoring** | Dashboard with real-time status, auto-refresh every 6 s |
| **Logs** | Live log streaming (SSE) for any service (webserver, db, selenium, mailpit) |
| **Terminal** | Interactive bash terminal to the webserver container from the browser |
| **Moodle actions** | Install database, init PHPUnit/Behat, purge caches |
| **Xdebug** | One-click ON/OFF toggle; off by default so browsing stays fast (2.x and 3.x handled automatically) |
| **Containers** | Active containers view with status and mapped ports |
| **Multi-instance** | Run as many instances as your machine allows, each fully isolated |

---

## Tech stack

```
Backend   →  Python 3.12 + FastAPI + Uvicorn
Frontend  →  HTMX 2.0 + Alpine.js + Tailwind CSS
Terminal  →  xterm.js (WebSocket)
Logs      →  Server-Sent Events (SSE)
Docker    →  Python docker SDK + Docker CLI
Data      →  JSON (data/instances.json)
```

---

## Requirements

- **Docker** with the Compose v2 plugin (`docker compose`)
- **[moodle-docker](https://github.com/moodlehq/moodle-docker)** cloned on the host machine
- Moodle source code available on the host (one directory per instance/version)

---

## Quick start

```bash
# 1. Clone this repository
git clone https://github.com/jjgalvezmolinero/moodle-manager.git
cd moodle-manager

# 2. Start the manager
docker compose up -d --build

# 3. Open in the browser
open http://localhost:9000
```

Then go to **Settings** and set the path to your moodle-docker clone. After that, create your first instance.

### Windows (Docker Desktop)

Requires Docker Desktop with the **WSL2 backend** (the default).

```powershell
git clone https://github.com/jjgalvezmolinero/moodle-manager.git
cd moodle-manager
powershell -ExecutionPolicy Bypass -File .\start-windows.ps1
```

The script writes a `.env` file (paths + `COMPOSE_FILE=compose.yml;compose.windows.yml`) and starts the container. After the first run, a plain `docker compose up -d` works as well.

Paths are entered and shown in Windows form (`C:\Users\me\moodle45`). By default only `C:\Users` is mounted into the manager, so your Moodle code must live under it. To use another drive or folder, add a line to the `volumes` section of [compose.windows.yml](compose.windows.yml), e.g. `- D:/projects:/run/desktop/mnt/host/d/projects`.

> Keeping the Moodle code inside the WSL filesystem is faster than on `C:\`, but it is not supported by the folder picker yet.

---

## Creating an instance

When you create an instance you need to provide:

| Field | Description | Example |
|---|---|---|
| **moodle-docker path** | Path to the moodle-docker repo on the host | `/home/user/moodle-docker` |
| **MOODLE_DOCKER_WWWROOT** | Path to the Moodle source code | `/home/user/moodle42` |
| **COMPOSE_PROJECT_NAME** | Unique prefix for the Docker containers | `moodle42` |
| **Web port** | HTTP port to access Moodle | `8042` |
| **PHP version** | PHP version to use | `8.3` |
| **Database** | Database engine | `pgsql`, `mariadb`, `mysql`... |

All other options (Xdebug, Selenium, PHPUnit external services, BBB mock, Mailpit, etc.) are optional and can be changed at any time by editing the instance.

---

## How Xdebug works

Xdebug is **off by default** and is switched on and off with the **Xdebug ON/OFF** button in the instance header. When it is off the extension is not loaded at all, so normal browsing runs at full speed.

- **On:** writes `zz-xdebug.ini` (mode, client host and port from the instance settings, `start_with_request = yes`) and reloads Apache. It takes well under a second. Recent `moodlehq/moodle-php-apache` images already ship `xdebug.so`. With older images it is installed from PECL the first time, using the right version for the PHP version:
  - PHP ≥ 8.0 → `xdebug` (3.x latest)
  - PHP 7.3–7.4 → `xdebug-3.1.6`
  - PHP 7.0–7.2 → `xdebug-2.9.8` (`remote_*` config)
  - PHP 5.6 → `xdebug-2.5.5` (`remote_*` config)
- **Off:** removes the ini file and reloads Apache.

> **Note:** the state lives in the container. It survives `stop`/`start`, and after a `down` the new container starts with Xdebug off.

## Performance: `$CFG->debug` in config.php

When `config.php` sets `$CFG->debug`, Moodle treats itself as being in developer mode before the database is available and skips its component cache. It then rescans every plugin directory on each request. On a Windows bind mount that adds about 8 s per page.

New instances get a `config.php` with that line commented out, and the **Instalar BD** action stores `DEBUG_DEVELOPER` in the database instead, so you still see every error. For existing instances, run **Optimizar config.php** in the *Acciones Moodle* tab. After adding new classes or plugins, purge caches.

---

## How it works

```
┌─────────────────────────────────────────────┐
│              Moodle Manager                 │
│          (Docker container)                 │
│                                             │
│  FastAPI ──► compose.py ──► docker compose  │
│                                    │        │
└────────────────────────────────────┼────────┘
                                     │ /var/run/docker.sock
                                     ▼
                          Docker daemon (host)
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                 ▼
              webserver_1       webserver_2       webserver_3
              db_1              db_2              db_3
              ...               ...               ...
```

The manager mounts the host Docker socket (`/var/run/docker.sock`), allowing it to run `docker compose` commands that execute directly on the host daemon. Paths (moodle-docker, wwwroot) are resolved on the host filesystem, so they must exist there.

The `/home` directory is also mounted inside the container so that all path checks and file operations work correctly against the host filesystem.

On Windows the daemon runs inside the Docker Desktop VM and sees drive `C:` as `/run/desktop/mnt/host/c`. `compose.windows.yml` mounts `C:\Users` at that same path inside the manager, and [app/paths.py](app/paths.py) translates `C:\...` paths to that form before using them, so the same invariant holds: the path Python reads is the path the daemon bind-mounts.

---

## Project structure

```
moodle-manager/
├── Dockerfile
├── compose.yml
├── requirements.txt
├── data/
│   ├── instances.json          # Instance persistence (auto-generated)
│   └── overrides/              # Generated compose files (legacy)
└── app/
    ├── main.py                 # FastAPI routes
    ├── models.py               # Pydantic models
    ├── store.py                # JSON CRUD
    ├── compose.py              # docker compose command builder + SSE logs
    ├── docker_ops.py           # Docker SDK (status, containers, exec)
    └── templates/
        ├── base.html           # Main layout + toasts + modal
        ├── index.html          # Dashboard
        ├── form.html           # Create / edit instance
        ├── instance.html       # Instance detail (tabs: containers, logs, terminal, actions)
        ├── settings.html       # Global settings
        └── fragments/
            └── containers.html # Containers table (HTMX fragment)
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `/data` | Directory where `instances.json` is persisted |
| `MOODLE_DOCKER_BUNDLED_PATH` | `${PWD}/moodle-docker` | moodle-docker path as seen by the host daemon |
| `HOST_OS` | `linux` | `windows` enables `C:\` path translation (set by `compose.windows.yml`) |
| `HOST_BROWSE_ROOT` | `/home` (`C:\Users` on Windows) | Folder the folder picker opens by default |
| `WINDOWS_MOUNT_PREFIX` | `/run/desktop/mnt/host` | Where Docker Desktop exposes Windows drives |

---

## Local development (without Docker)

```bash
cd app
pip install -r ../requirements.txt
DATA_DIR=../data uvicorn main:app --reload --port 9000
```

Requires `docker` CLI in PATH and access to the host Docker socket.

---

## Roadmap

### High priority
- [ ] **Authentication** — Login with username/password configured via environment variables
- [ ] **Async operations** — Job ID + real-time progress for `up` and `pull`
- [ ] **Operation history** — Action log per instance

### Interface
- [ ] **Dark mode** — Toggle with persistent preference
- [ ] **Search and filter** on the dashboard
- [ ] **Container metrics** — Real-time CPU and memory usage

### Persistence
- [ ] **SQLite migration** — To replace JSON as data volume grows
- [ ] **Backup/restore** — Export and import configurations as JSON

### Multi-machine *(future roadmap)*
- [ ] **Remote agents** — Lightweight API running on each remote host
- [ ] **Multi-host dashboard** — Unified view of instances across all machines
- [ ] **Remote deployment** — Start/stop instances on remote hosts

---

## License

AGPL-3.0 — see [LICENSE](LICENSE) for details.
