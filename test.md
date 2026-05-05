 Lions Studio AI — Infrastructure Documentation

**Last updated:** 2026-05-05
**Status:** Production with full blue-green CI/CD
**Domain:** dev.lionstudioai.com
**GitHub org:** mobilions

---

## Table of Contents

1. [What this document is](#1-what-this-document-is)
2. [The big picture](#2-the-big-picture)
3. [The server](#3-the-server)
4. [Apps overview](#4-apps-overview)
5. [Database](#5-database)
6. [Container registry (GHCR)](#6-container-registry-ghcr)
7. [CI/CD pipeline with blue-green](#7-cicd-pipeline-with-blue-green)
8. [nginx reverse proxy](#8-nginx-reverse-proxy)
9. [Backups (5-layer protection)](#9-backups-5-layer-protection)
10. [Migrations](#10-migrations)
11. [Secrets and credentials map](#11-secrets-and-credentials-map)
12. [File-by-file reference](#12-file-by-file-reference)
13. [Operations runbook](#13-operations-runbook)
14. [Known gotchas](#14-known-gotchas)
15. [Open work](#15-open-work)
16. [Glossary](#16-glossary)

---

## 1. What this document is

This document explains how **Lions Studio AI** runs in production. After reading it you should be able to:

- Deploy a new version of any app
- Recover from a database failure or bad deploy
- Add a new migration safely
- Diagnose problems
- Rotate secrets

It assumes basic Linux, Docker, Git, and HTTP knowledge. No prior project knowledge needed.

---

## 2. The big picture

Single Ubuntu ARM64 server runs everything. Three Next.js/FastAPI apps run inside Docker containers using **blue-green deployment** for zero downtime. One Flutter app is served as static files. PostgreSQL runs directly on the host. nginx is the reverse proxy with **atomic symlink switching** for blue-green. GitHub Actions auto-deploys on every push to `main`. A daily encrypted backup of the database goes to Cloudflare R2. Backend has additional pre-deploy DB snapshots before each migration.

```
                          dev.lionstudioai.com
                                  │
                                  ▼
                              ┌───────┐
                              │ nginx │  HTTPS via Let's Encrypt
                              └───┬───┘
              ┌──────────┬───────┴───────┬───────────┐
              ▼          ▼               ▼           ▼
            /        /admin            /api         /app
              │          │               │           │
   nginx active symlinks select blue/green color
              │          │               │           │
              ▼          ▼               ▼           ▼
    lionstudio-blue  admin-blue    backend-blue  Static files
    OR -green        OR -green     OR -green     /var/www/.../web
    4001 OR 4002     3004 OR 3005  8001 OR 8002  Flutter web
                                      │
                                      ▼
                                 Postgres 14
                              (host, 127.0.0.1:5432)
                              DB: lionstudio_dev
                                      │
                                      ▼ daily 02:00 UTC
                            pg_dump → gpg → rclone
                                      │
                                      ▼
                            Cloudflare R2 (private bucket)
```

**Why this shape?**
- Single server keeps it cheap
- Docker for apps + blue-green = zero-downtime deploys with instant rollback
- Postgres on host keeps backups simple
- nginx routes by URL path so all apps share one domain
- Cloudflare R2 because egress is free

---

## 3. The server

| Property | Value |
|----------|-------|
| Hostname | `style-ai-vnic` |
| OS | Ubuntu 22.04 ARM64 (`aarch64`) |
| Disk | 97 GB total, 78 GB free |
| TLS | Let's Encrypt via certbot (auto-renewing) |
| SSH access | as `root` and `ubuntu` users |

**ARM64 implications:** Docker images must be built for `linux/arm64`. We use:
- GitHub Actions: QEMU emulation (`docker/setup-qemu-action@v3` + `platforms: linux/arm64`) — slow ~10-15 min
- Local builds: `docker buildx --platform linux/arm64`

Do not push x86_64 images — they fail with `no matching manifest for linux/arm64/v8`.

---

## 4. Apps overview

| URL path | Tech | Active container (blue or green) | Blue port | Green port | Container internal | Repo |
|----------|------|----------------------------------|-----------|------------|---------------------|------|
| `/` | Next.js 16 + Tailwind v4 | `lionstudio-blue` or `lionstudio-green` | 4001 | 4002 | 3000 | mobilions/lionstudio-ai |
| `/admin` | Next.js + MUI v9 | `admin-blue` or `admin-green` | 3004 | 3005 | 3002 | mobilions/style_ai_admin |
| `/api` | FastAPI 3.12 + asyncpg | `backend-blue` or `backend-green` | 8001 | 8002 | 8000 | mobilions/style_ai_backend |
| `/app` | Flutter (web) | n/a — static files | n/a | n/a | n/a | mobilions/style_ai_frontend |
| `/grafana` | Grafana | `grafana` (existing, untouched) | 3000 | n/a | 3000 | n/a |

**At any moment**, exactly one color per app is active (serving production traffic) and the other is stopped (kept for instant rollback).

**Branch convention:**
- `main` = production. Push triggers auto-deploy.
- `development` (or `dev`) = ongoing work. Force-push allowed. NOT triggering deploys.

---

## 5. Database

### Setup
- **PostgreSQL 14.22** on Ubuntu 22.04 ARM64
- **Host install** (NOT in Docker — locked decision)
- **Port:** `127.0.0.1:5432` (loopback only)
- Connection from FastAPI: `postgresql+asyncpg://mobilions:PASSWORD@localhost:5432/lionstudio_dev`

### Databases
| Name | Owner | Purpose | Active |
|------|-------|---------|--------|
| `lionstudio_dev` | `postgres` | Production data (current) | ✅ Yes |
| `lionstudio_test` | `mobilions` | Earlier production DB | ❌ No (kept as snapshot) |

**Important context:** Production switched from `lionstudio_test` to `lionstudio_dev` on 2026-05-05. Container reads `DATABASE_URL` from `.env.docker` file on the server. Both DBs exist at ~11 MB; `_test` is preserved as historical reference.

### How the container connects to host Postgres
Container runs with `--network host` (uses host's network namespace), so `localhost:5432` inside the container = the host's actual localhost. No Docker bridge networking, no `host.docker.internal` needed.

### Why Postgres is NOT in Docker
1. Backups simpler (just `pg_dump` from host)
2. No Docker networking complexity
3. No volume management for the data directory
4. One source of truth, no risk of two Postgres instances

---

## 6. Container registry (GHCR)

GitHub Container Registry (free for org-owned repos).

| Image | Tags |
|-------|------|
| `ghcr.io/mobilions/lionstudio-ai` | `<commit-sha>`, `latest` |
| `ghcr.io/mobilions/style_ai_admin` | `<commit-sha>`, `latest` |
| `ghcr.io/mobilions/style_ai_backend` | `<commit-sha>`, `latest` |

**Authentication for push (CI):** PAT with `write:packages` scope, stored as `GHCR_TOKEN` secret in each repo.

**Authentication for pull (server):** `docker login ghcr.io` cached for `root` and `ubuntu` users. Re-login when token rotates.

**Tagging strategy:**
- Push to `main` produces image tagged with commit SHA — this is what gets deployed
- `latest` always points to most recent main build (used for manual `docker pull` only)

---

## 7. CI/CD pipeline with blue-green

Each app repo has `.github/workflows/cd.yml` with the same 2-job structure.

### Trigger
```yaml
on:
  push:
    branches: [main]
  workflow_dispatch:
```

### Job 1: build-and-push (~10-15 min)

```
1. Checkout code
2. Set up QEMU (linux/arm64 emulation on x86 runner)
3. Set up Docker Buildx
4. Log in to GHCR using GHCR_TOKEN
5. Compute image tag = ghcr.io/mobilions/<repo>:<commit-sha>
6. Build image with platforms: linux/arm64
7. Push to GHCR with tags: <commit-sha> + latest
8. Cache layers via type=gha (faster subsequent builds)
```

**Build args (admin only):** `NEXT_PUBLIC_API_BASE_URL=https://dev.lionstudioai.com` is passed as Docker build arg so Next.js bakes the production URL into compiled JS at build time. Without this, browser runs into mixed-content errors with hardcoded LAN IP fallback.

### Job 2: deploy (~1.5 min)

A single SSH call into the blue-green script:

```yaml
- name: Deploy via blue-green
  run: |
    ssh -i deploy_key ubuntu@$SSH_HOST \
      "sudo /usr/local/bin/blue_green_deploy.sh <app> $IMAGE"
```

The script (`/usr/local/bin/blue_green_deploy.sh <app> <image>`) does all the work.

### What the deploy script does

```
1. Determine current color from /etc/nginx/active/<app>.conf symlink
   (blue or green, target = the OTHER color)

2. Pull new image
3. (backend only) pg_dump snapshot to /var/backups/lionstudio/pre_deploy_*.dump
4. (backend only) Run alembic upgrade head in throwaway container
   - If migration fails → exit, old container keeps serving (no swap)

5. Start NEW container on the inactive color's port
   (e.g., if currently on blue:8001, start green:8002)

6. Wait for healthcheck = healthy (max 2 min)
   - If unhealthy → cleanup, exit (no swap)

7. Direct smoke test on new container's port
   - If fails → cleanup, exit

8. ATOMIC SWITCH: ln -sfn /etc/nginx/active/<app>-<new-color>.conf
                  /etc/nginx/active/<app>.conf
9. nginx reload (preserves existing connections)

10. Production smoke test through nginx
    - If fails → ROLLBACK (revert symlink, reload, cleanup new container)

11. 30s grace period for in-flight requests on old container
12. Stop old container (kept for ~24h for rollback)
```

### Auto-rollback triggers

The script automatically rolls back (revert symlink + reload nginx + remove failed container) if any of these fail:
- New container doesn't reach `healthy` within 2 min
- Direct smoke test on new port fails
- nginx config validation fails after symlink switch
- Production smoke test through nginx fails

Old container stays running throughout — production traffic NEVER goes to a broken new version.

### GitHub Secrets per repo

All 3 app repos share the same secret values:

| Secret | Value | Used for |
|--------|-------|----------|
| `SSH_HOST` | server IP | `ssh` target |
| `SSH_USER` (admin) | `ubuntu` | hardcoded for backend/main |
| `SSH_PRIVATE_KEY` | contents of `/root/.ssh/github_actions_deploy` | SSH auth |
| `GHCR_TOKEN` | PAT with `write:packages` | docker login + git ops |

### Server-side prep (one-time, already done)

```
/root/.ssh/github_actions_deploy.pub  →  /home/ubuntu/.ssh/authorized_keys
ubuntu user → docker group (no sudo for docker)
sudoers entry: ubuntu ALL=(ALL) NOPASSWD: /usr/local/bin/blue_green_deploy.sh
```

### Why blue-green via nginx symlink

Alternatives like Docker Swarm, Kubernetes, or HAProxy would work but add complexity. The symlink approach:
- Pure nginx + Docker (no new tools)
- 1-second rollback (just flip a symlink + nginx reload)
- Atomic switch (no half-routed state)
- Easy to debug (`ls -la` shows current color)
- Survives nginx reloads cleanly

---

## 8. nginx reverse proxy

### Files
- Active config: `/etc/nginx/sites-enabled/lionstudio_dev` (symlink to sites-available)
- Source: `/etc/nginx/sites-available/lionstudio_dev`
- Tracked copy: `/opt/lionstudio_infra/nginx/lionstudio_dev.conf`

### Active color selection (the magic of blue-green)

Each app has 3 files in `/etc/nginx/active/`:

```
admin-blue.conf      :  set $admin_upstream "127.0.0.1:3004";
admin-green.conf     :  set $admin_upstream "127.0.0.1:3005";
admin.conf           →  symlink to either admin-blue.conf OR admin-green.conf
```

Same pattern for `main-*` (4001/4002) and `backend-*` (8001/8002).

The main nginx config does:
```nginx
location ^~ /admin/ {
    include /etc/nginx/active/admin.conf;
    proxy_pass http://$admin_upstream;
    ...
}
```

To switch colors: `ln -sfn /etc/nginx/active/admin-green.conf /etc/nginx/active/admin.conf` + `nginx -s reload`. Atomic.

### Routes
```
/api/*           → 127.0.0.1:$backend_upstream  (Docker backend)
/admin/*         → 127.0.0.1:$admin_upstream    (Docker admin)
/admin/<bad>     → 302 redirect to https://dev.lionstudioai.com/  (via @admin_404_redirect)
/                → 127.0.0.1:$main_upstream     (Docker main site)
/_next/*         → 127.0.0.1:$main_upstream     (main site Next.js chunks)
/admin/_next/*   → 127.0.0.1:$admin_upstream    (admin Next.js chunks, uses $request_uri)
/app/*           → static files at /var/www/html/style-ai-fe/web/
/grafana/*       → 127.0.0.1:3000
```

### Critical pattern: variable proxy_pass with $request_uri

`proxy_pass http://$variable;` — passes original full request URI to backend ✅
`proxy_pass http://$variable/path/;` — does NOT extend with location-match path; backend gets only `/path/` ❌
`proxy_pass http://$variable$request_uri;` — explicitly passes original URI ✅✅ (safest with variables)

We use the third form for `/admin/_next/`, `/main/_next/`, and `/api/` blocks because the location match has a path prefix that the variable form would mishandle.

### TLS
- Issuer: Let's Encrypt
- Renewal: certbot via systemd timer (auto-renews 30 days before expiry)

### Reload nginx
```bash
sudo nginx -t              # validate syntax
sudo systemctl reload nginx  # zero-downtime reload
```

---

## 9. Backups (5-layer protection)

### Layer 1 — Expand-contract migrations ✅ ACTIVE
Discipline: every migration must be safe to run while old code is still running. See [Section 10](#10-migrations).

### Layer 2 — Pre-deploy snapshot ✅ ACTIVE (backend only)
Before each backend deploy, the script does:
```bash
sudo -u postgres pg_dump -Fc lionstudio_dev > /var/backups/lionstudio/pre_deploy_<timestamp>.dump
```
Snapshots accumulate in `/var/backups/lionstudio/`. No automatic cleanup yet — manual purge recommended once weekly.

### Layer 3 — Shadow tables ⬜ PLANNED
Instead of `DROP COLUMN x`, helper creates `_shadow_<table>_<col>_<date>` table with the data first. Lets us recover dropped data weeks later. 30-day cron-driven cleanup.

### Layer 4 — Daily encrypted backup to R2 ✅ ACTIVE
Cron at 02:00 UTC runs `/usr/local/bin/backup_lionstudio_db.sh`:
```
pg_dump → gpg AES-256 (passphrase from /etc/lionstudio_backup/gpg_passphrase)
        → rclone copy to r2:lionstudio/db_backups/
```
Local retention: 2 days. R2 retention: 30 days.

### Layer 5 — WAL archiving for PITR ⬜ PLANNED
Postgres archives every committed transaction to R2 in real-time. Restore to any point within 7 days. Bounds worst-case data loss to seconds.

---

## 10. Migrations

### Why migrations are dangerous

Code swap = ~5 sec. Schema change = potentially minutes. Naive migrations cause downtime + data loss.

### Discipline: expand-contract

> **Rule: every migration must be safe to run while old code is still running.**

#### ✅ SAFE (1 deploy)
- Add a new nullable column
- Add a new table
- Add an index `CONCURRENTLY`

#### ❌ DESTRUCTIVE (must be split into multiple deploys)
- Drop a column the old code reads
- Rename a column (= drop + add from DB's perspective)
- Make a column NOT NULL when old code might insert NULL
- Change a column's type if old code writes the old type

### Example: rename `email` → `email_address` (4 deploys)

| Deploy | Migration | Code change |
|--------|-----------|-------------|
| **1. Expand** | Add nullable `email_address` column | Code writes BOTH columns. Reads from `email`. |
| **2. Backfill + switch reads** | `UPDATE users SET email_address = email WHERE email_address IS NULL` | Code reads from `email_address`, still writes both. |
| **3. Stop old write** | Make `email_address` NOT NULL | Code only reads + writes `email_address`. |
| **4. Contract** | `shadow_drop_column('users', 'email')` | (no code change) |

If you do this in 1 deploy, the moment `ALTER ... RENAME` runs, every old container hits the missing `email` column and crashes.

### Where migrations live

- Repo: `style_ai_backend`
- Path: `alembic/versions/*.py`
- Currently 24 migrations
- `alembic/env.py` reads `DATABASE_URL` from `app/config.py`

### When migrations run during deploy

Backend deploy script sequence:
```
1. Pull new image
2. pg_dump snapshot to /var/backups/lionstudio/pre_deploy_*.dump
3. Run alembic upgrade head in throwaway container
   - If FAILS → exit, old container keeps serving
4. Start new container on inactive port
5. Healthcheck, smoke tests, atomic nginx switch
6. 30s grace period
7. Stop old container
```

Migrations run BEFORE container swap, with `pg_dump` as safety net first.

### Adding a new migration

```bash
alembic revision -m "add user phone column"
# edit the generated file
git add alembic/versions/*.py
git commit -m "Migration: add user phone column"
git push origin main
# CI runs alembic upgrade head as part of deploy
```

### CI safety guards (planned, not yet implemented)

- Pre-commit hook scanning new migrations for `drop_column`, `drop_table`, `nullable=False`
- Blocks merge unless `# SAFE: reviewed forward-compat` comment present
- Migration replay test: empty Postgres → run all migrations → compare to SQLAlchemy models

---

## 11. Secrets and credentials map

| Secret | Where stored | Used by |
|--------|--------------|---------|
| GHCR PAT | `~/.git-credentials` (server) + GitHub Secrets `GHCR_TOKEN` in 3 repos | `git push`, `docker login ghcr.io` |
| SSH deploy key | `/root/.ssh/github_actions_deploy{,.pub}` + GitHub `SSH_PRIVATE_KEY` | GitHub Actions ssh ubuntu@server |
| Postgres password | `.env` and `.env.docker` in `/var/www/html/style_ai_backend/` (mode 600 ubuntu:ubuntu) | FastAPI app |
| R2 API token | `/root/.config/rclone/rclone.conf` (mode 600 root) | Backup script |
| GPG passphrase | `/etc/lionstudio_backup/gpg_passphrase` (mode 600 root) + offline | Backup encryption |
| Firebase service account | `/var/www/html/style_ai_backend/firebase_service_account.json` | FastAPI startup |
| GCP service account (Vertex AI) | `/var/www/html/style_ai_backend/gcp-agent-studio.json` | FastAPI google-genai |
| JWT secret | `JWT_SECRET_KEY` in `.env.docker` | FastAPI auth |
| Gemini API key | `GEMINI_API_KEY` in `.env.docker` | FastAPI AI calls |
| Razorpay test keys | `RAZORPAY_*` in `.env.docker` | FastAPI payments |
| SMTP (Gmail app password) | `SMTP_PASSWORD` in `.env.docker` | FastAPI email |
| iOS shared secret | `IOS_SHARED_SECRET` in `.env.docker` | iOS receipt validation |
| TLS certs | `/etc/letsencrypt/live/dev.lionstudioai.com/` (auto-renewing) | nginx |

### ⚠️ Known leaks (rotate ASAP)

Several secrets were pasted in chat during setup. Rotate them before deploying any sensitive new code:
- GHCR PAT
- GPG passphrase
- JWT_SECRET_KEY (rotation invalidates all sessions)
- GEMINI_API_KEY (Google billing risk)
- DATABASE_URL password
- SMTP Gmail app password
- IOS_SHARED_SECRET

Rotation procedure for each is in [Section 13](#13-operations-runbook).

---

## 12. File-by-file reference

### Per-app repo files

#### `style_ai_admin/`
- `Dockerfile` — Node 22 alpine, multi-stage, output:standalone, non-root nextjs user, port 3002, healthcheck on `/admin/login`
- `.dockerignore` — excludes `.env`, secrets, node_modules, .git
- `next.config.js` — `basePath: '/admin'`, `assetPrefix: '/admin'`, `output: 'standalone'`
- `src/utils/axios.js` — uses `process.env.NEXT_PUBLIC_API_BASE_URL` (baked at build time via Docker build-arg)
- `.github/workflows/cd.yml` — build with QEMU, push to GHCR with build-arg `NEXT_PUBLIC_API_BASE_URL`, deploy via `blue_green_deploy.sh admin`

#### `lionstudio-ai/`
- `Dockerfile` — Node 22 alpine, multi-stage, output:standalone, port 3000, healthcheck on `/`
- `.dockerignore`, `postcss.config.mjs` (for Tailwind v4)
- `next.config.ts` — `output: "standalone"`, `images: { minimumCacheTTL: 60 }`
- `.github/workflows/cd.yml` — same as admin minus build-args

#### `style_ai_backend/`
- `Dockerfile` — Python 3.12-slim, multi-stage, gunicorn + uvicorn workers, non-root appuser, port 8000, healthcheck on `/api/health`
- `.dockerignore` — excludes `venv/`, secrets `.env*` and `*_service_account.json`, `media/`, `logs/`
- `docker-entrypoint.sh` — runs gunicorn with PORT/WORKERS/TIMEOUT env overrides
- `.env` — source-of-truth env file (used for development)
- `.env.docker` — whitespace-stripped copy used by `--env-file` (Docker is strict about `KEY=VALUE` format)
- `alembic.ini`, `alembic/env.py`, `alembic/versions/*.py` (24 migrations)
- `.github/workflows/cd.yml` — build, deploy via `blue_green_deploy.sh backend`

### Server-side files

#### `/etc/nginx/active/`
```
admin-blue.conf       : set $admin_upstream "127.0.0.1:3004";
admin-green.conf      : set $admin_upstream "127.0.0.1:3005";
admin.conf            → symlink to one of the above
main-blue.conf        : set $main_upstream "127.0.0.1:4001";
main-green.conf       : set $main_upstream "127.0.0.1:4002";
main.conf             → symlink to one of the above
backend-blue.conf     : set $backend_upstream "127.0.0.1:8001";
backend-green.conf    : set $backend_upstream "127.0.0.1:8002";
backend.conf          → symlink to one of the above
```

#### `/etc/nginx/sites-available/lionstudio_dev`
Main nginx config. Uses `include /etc/nginx/active/<app>.conf;` + `proxy_pass http://$<app>_upstream;` pattern in each location block.

Backups in same directory: `lionstudio_dev.bak.<YYYYMMDD>_<HHMM>` from each major edit.

#### `/usr/local/bin/blue_green_deploy.sh`
Single deploy script handling all 3 apps (140 lines). Reads color from active symlink, deploys to opposite color, healthchecks, atomic switch, auto-rollback on failure. Backend gets pre-deploy snapshot + alembic.

Mode 755, owned by root.

#### `/usr/local/bin/backup_lionstudio_db.sh`
Daily backup script. `pg_dump -Fc | gpg --symmetric AES256 | rclone copy` to R2.

#### `/etc/cron.d/lionstudio_backup`
```
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 2 * * * root /usr/local/bin/backup_lionstudio_db.sh >> /var/log/lionstudio_backup_cron.log 2>&1
```

#### `/etc/sudoers.d/blue_green_deploy`
```
ubuntu ALL=(ALL) NOPASSWD: /usr/local/bin/blue_green_deploy.sh
```
Lets GitHub Actions invoke the deploy script as ubuntu without password.

#### `/etc/lionstudio_backup/gpg_passphrase`
45-char random base64. Mode 600, root-only. **Critical** — must also exist in offline password manager.

#### `/root/.config/rclone/rclone.conf`
```ini
[r2]
type = s3
provider = Cloudflare
access_key_id = <REDACTED>
secret_access_key = <REDACTED>
endpoint = https://<account-id>.r2.cloudflarestorage.com
acl = private
no_check_bucket = true
```
Mode 600. Token scoped to `lionstudio` bucket only.

#### `/var/backups/lionstudio/`
- `pre_deploy_<timestamp>.dump` — backend deploy snapshots (no automatic cleanup)
- `lionstudio_<timestamp>.dump.gpg` is intermediate, deleted after R2 upload

#### `/var/www/html/style_ai_backend/`
- `media/` — user uploads (mounted as Docker volume in containers)
- `logs/` — rotating app logs (mounted as Docker volume)
- `firebase_service_account.json` — mounted read-only in containers
- `gcp-agent-studio.json` — mounted read-only in containers

#### `/var/log/`
- `lionstudio_backup.log` — append-only log of every backup run
- `lionstudio_backup_cron.log` — cron stdout/stderr per run

#### `/etc/systemd/system/lionstudio_dev.service`
**DISABLED** systemd unit that used to run gunicorn directly on port 8000. Kept as fallback. Slated for deletion 2026-05-03 (overdue).

---

## 13. Operations runbook

### Deploy a new version
```bash
# On dev machine
git push origin main
# GitHub Actions auto-runs (~12 min build + ~1.5 min deploy)
# Watch: https://github.com/mobilions/<repo>/actions
```

### Manual deploy (skip GitHub Actions)
```bash
ssh ubuntu@server
sudo /usr/local/bin/blue_green_deploy.sh admin ghcr.io/mobilions/style_ai_admin:<sha-or-latest>
sudo /usr/local/bin/blue_green_deploy.sh main ghcr.io/mobilions/lionstudio-ai:<sha-or-latest>
sudo /usr/local/bin/blue_green_deploy.sh backend ghcr.io/mobilions/style_ai_backend:<sha-or-latest>
```

### Manual rollback (instant)
```bash
ssh ubuntu@server

# Find current color
ls -la /etc/nginx/active/admin.conf
# e.g., admin.conf -> admin-green.conf

# Flip to opposite color
sudo ln -sfn /etc/nginx/active/admin-blue.conf /etc/nginx/active/admin.conf
sudo nginx -t && sudo systemctl reload nginx

# Verify
curl -s -o /dev/null -w "%{http_code}\n" https://dev.lionstudioai.com/admin/login
```

⚠️ Rollback only works if the OLD container is still running. The deploy script keeps it for ~24h (it's stopped, not removed). After 24h or after the next deploy, you'd need to `docker run` the previous image manually.

### Rollback via re-deploy of older image
```bash
# Find older image SHA from git log or GitHub UI
ssh ubuntu@server
sudo /usr/local/bin/blue_green_deploy.sh admin ghcr.io/mobilions/style_ai_admin:<older-sha>
```

### Restore from R2 backup
```bash
ssh root@server

cd /tmp
rclone copy r2:lionstudio/db_backups/lionstudio_<timestamp>.dump.gpg .

gpg --batch --quiet --passphrase-file /etc/lionstudio_backup/gpg_passphrase \
    --decrypt lionstudio_<timestamp>.dump.gpg > restore.dump

sudo -u postgres pg_restore --clean --if-exists -d lionstudio_dev restore.dump

rm -f restore.dump lionstudio_<timestamp>.dump.gpg
```

### Restore from pre-deploy snapshot (after a bad migration)
```bash
ssh root@server

# Find the most recent pre-deploy snapshot
ls -lt /var/backups/lionstudio/pre_deploy_*.dump | head -3

# Restore it
sudo -u postgres pg_restore --clean --if-exists -d lionstudio_dev /var/backups/lionstudio/pre_deploy_<timestamp>.dump

# Then deploy the previous (working) image to put the right code with the right schema
sudo /usr/local/bin/blue_green_deploy.sh backend ghcr.io/mobilions/style_ai_backend:<previous-sha>
```

### Run a backup manually
```bash
ssh root@server
/usr/local/bin/backup_lionstudio_db.sh
```

### Add a new migration
```bash
# In style_ai_backend on dev machine
alembic revision -m "description"
# edit alembic/versions/*.py
git add alembic/versions/*.py
git commit -m "Migration: <description>"
git push origin main
# CI runs alembic upgrade head as part of deploy (snapshot taken first)
```

### Check container health
```bash
ssh ubuntu@server

# All containers and their colors
docker ps -a --filter name=admin --format "{{.Names}}: {{.Status}}"
docker ps -a --filter name=lionstudio --format "{{.Names}}: {{.Status}}"
docker ps -a --filter name=backend --format "{{.Names}}: {{.Status}}"

# Active color symlinks
ls -la /etc/nginx/active/*.conf

# Specific container
docker logs admin-green --tail 50
docker inspect --format='{{.State.Health.Status}}' admin-green
```

### View nginx logs
```bash
ssh root@server
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log
```

### Renew TLS cert manually
```bash
sudo certbot renew --dry-run    # test
sudo certbot renew              # actual
sudo systemctl reload nginx
```

### Rotate GHCR PAT
1. Revoke leaked PAT at https://github.com/settings/tokens
2. Generate new PAT with `write:packages` scope, save offline
3. Update `~/.git-credentials` on server with new token
4. Update each repo's `GHCR_TOKEN` GitHub Secret (admin, main, backend repos)
5. Re-login as both server users:
   ```bash
   echo "<NEW_PAT>" | docker login ghcr.io -u mobilions --password-stdin
   sudo -u ubuntu bash -c 'echo "<NEW_PAT>" | docker login ghcr.io -u mobilions --password-stdin'
   ```
6. Trigger workflow_dispatch on any repo's CD workflow to verify

### Rotate GPG passphrase
```bash
# 1. Generate new passphrase, save offline
openssl rand -base64 32 | sudo tee /etc/lionstudio_backup/gpg_passphrase

# 2. Save offline (paste from terminal to password manager — NOT chat)
sudo cat /etc/lionstudio_backup/gpg_passphrase
# Then: clear

# 3. Delete old R2 backups (encrypted with old passphrase)
rclone delete r2:lionstudio/db_backups/

# 4. Run new backup
sudo /usr/local/bin/backup_lionstudio_db.sh
```

### Rotate JWT_SECRET_KEY
⚠️ Invalidates all current user sessions — they'll need to re-login.
```bash
NEW_SECRET=$(openssl rand -hex 32)
# Edit .env and .env.docker, replace JWT_SECRET_KEY=...
sudo nano /var/www/html/style_ai_backend/.env.docker

# Restart backend so new secret takes effect (will swap blue↔green via deploy)
sudo /usr/local/bin/blue_green_deploy.sh backend ghcr.io/mobilions/style_ai_backend:latest
```

### Rotate Postgres password
```bash
ssh root@server
sudo -u postgres psql -c "ALTER USER mobilions WITH PASSWORD 'NEW_PASS';"
# Edit DATABASE_URL in .env.docker
sudo nano /var/www/html/style_ai_backend/.env.docker

# Redeploy
sudo /usr/local/bin/blue_green_deploy.sh backend ghcr.io/mobilions/style_ai_backend:latest
```

### Rotate Gemini API key
1. https://makersuite.google.com/app/apikey → delete leaked key → generate new
2. Update `GEMINI_API_KEY` in `.env.docker`
3. Redeploy backend

### Switch active DB (e.g., test → dev)
```bash
# 1. Verify target DB has the data you want (see Section 5)
sudo -u postgres psql -d lionstudio_dev -c "SELECT COUNT(*) FROM users;"

# 2. Snapshot the current DB
sudo -u postgres pg_dump -Fc lionstudio_test > /tmp/lionstudio_test_$(date +%Y%m%d).dump

# 3. Update DATABASE_URL in .env and .env.docker

# 4. Redeploy backend
sudo /usr/local/bin/blue_green_deploy.sh backend ghcr.io/mobilions/style_ai_backend:latest

# 5. Verify
docker exec backend-green printenv DATABASE_URL | sed -E 's|//[^@]+@|//USER:PASS@|'
```

---

## 14. Known gotchas

### Heredoc paste indents content with 2 leading spaces
Pattern: pasting `cat > FILE <<'EOF' ... EOF` blocks into the user's terminal adds leading 2 spaces to body lines. Breaks shell scripts (shebang line), cron entries (silently ignored), .dockerignore patterns (literal pattern instead of glob).

**Fix:** after creating any file via heredoc, run `sed -i 's/^  //' FILE` to strip the leading 2 spaces. Verify shebangs with `head -1 FILE` showing `#!/bin/bash` (no leading space).

### nginx variable proxy_pass with URI doesn't extend path
`proxy_pass http://$variable/path/;` — backend gets only `/path/`, not `/path/<rest-of-URI>`.

**Fix:** always use `proxy_pass http://$variable$request_uri;` to explicitly pass the original request URI. Caused a 308 redirect loop on `/admin/_next/static/chunks/*` — cost ~30 min to debug.

### Browser caches 308 Permanent Redirects forever
Even hard refresh (Ctrl+Shift+R) doesn't always clear them.

**Fix:** test in incognito mode to bypass cache. Or DevTools → Network → Disable cache + Empty Cache & Hard Reload. Document this clearly for any user-facing cutover where 308s might appear.

### Docker `--rm` flag prevents rollback
Some old workflows used `docker run -d --rm`. When the container stops, Docker auto-removes it — can't roll back.

**Fix:** never use `--rm` for production containers. Use `--restart unless-stopped` so they survive reboots, and let the deploy script `docker rm` them only when intentional.

### bash `set -e` doesn't catch failures inside `$(...)`
`var=$(failing-cmd)` succeeds with empty value even with `set -e` enabled. Caused the deploy script to silently exit when `readlink` was called on a non-existent symlink.

**Fix:** check explicitly with `[ -L "$file" ] || exit 1` before reading via `readlink`.

### Docker `--env-file` is strict about `KEY=VALUE` format
Spaces around `=` (e.g., `BASE_URL = https://...`) cause `docker: invalid env file: variable contains whitespaces`.

**Fix:** use a separate `.env.docker` file. Strip spaces with `sed -E 's/^([A-Z_][A-Z0-9_]*)[[:space:]]*=[[:space:]]*/\1=/'`. Pydantic-settings tolerates the spaces; Docker doesn't.

### YAML indentation must be consistent within a workflow file
Some pasted YAML had `build-and-push:` at 4 spaces but `deploy:` at 2 spaces. GitHub silently rejects parts of it.

**Fix:** keep all top-level job keys at the same indent level under `jobs:`. Validate with `python -c "import yaml; yaml.safe_load(open('cd.yml'))"`.

### NEXT_PUBLIC_* env vars bake at BUILD time, not runtime
If `NEXT_PUBLIC_API_BASE_URL` isn't set during `next build`, the compiled JS uses the source code's fallback. We baked in `http://192.168.0.120:8001` (LAN IP) by accident, causing browser mixed-content errors.

**Fix:** pass as Docker build-arg in the workflow:
```yaml
- name: Build and push
  uses: docker/build-push-action@v5
  with:
    build-args: |
      NEXT_PUBLIC_API_BASE_URL=https://dev.lionstudioai.com
```
And in Dockerfile, declare ARG + ENV before `RUN npm run build`.

### `lionstudio_dev.service` systemd unit reincarnates gunicorn
After Phase D, an unrelated systemd unit kept restarting gunicorn on port 8000 every time we killed it.

**Fix:** `sudo systemctl disable lionstudio_dev.service`. Don't delete the unit file — kept as fallback for 48h.

---

## 15. Open work

| Priority | Item | When | Why |
|----------|------|------|-----|
| 🔴 Critical | Rotate leaked GHCR PAT, GPG passphrase, JWT secret, Gemini API key, DB password, SMTP password, iOS shared secret | This week | Multiple secrets pasted in chat during setup |
| 🔴 Critical | Delete old `lionstudio_dev.service` systemd unit + cleanup `chmod 777` on logs/media | 2026-05-03 (overdue) | Was kept as 48h fallback |
| 🟠 High | Phase B — branch protection on main + Dependabot for all 4 repos | This week | Stops Tushar's force-push pattern |
| 🟠 High | Cleanup: simplify backend cd.yml deploy step (remove debug `set -x` / `ssh -v`) | When stable | Currently noisy logs |
| 🟡 Medium | Phase E — Flutter web pipeline (`style_ai_frontend`) | Next 2 weeks | Last app without CI/CD |
| 🟡 Medium | Layer 5 — WAL archiving to R2 (PITR) | Next month | Sub-second data loss bound |
| 🟡 Medium | Layer 3 — shadow-table helpers in alembic | Next month | Rollback after destructive migrations |
| 🟡 Medium | CI safety guards for migrations (pre-commit + replay test) | When time permits | Catch destructive migrations before merge |
| 🟢 Low | Auto-cleanup of `pre_deploy_*.dump` snapshots (>7 days) | When time permits | Local disk fills slowly |
| 🟢 Low | Move backup script + cron + deploy script + nginx active configs to `lionstudio_infra` repo | When time permits | Version control for infra |
| 🟢 Low | Monitoring: alert if no backup in 25h | When time permits | Currently silent failures |

---

## 16. Glossary

| Term | Meaning |
|------|---------|
| **Blue-green** | Deploy strategy: run two production environments (blue + green); swap traffic atomically when new version verified. |
| **GHCR** | GitHub Container Registry. Free Docker image hosting for org-owned repos. |
| **QEMU buildx** | Cross-architecture Docker build. We build ARM64 images on x86 GitHub runners. |
| **Expand-contract** | Migration pattern. Split destructive schema changes across multiple deploys so old code stays working. |
| **Shadow table** | Pre-drop snapshot table (`_shadow_<table>_<col>_<date>`). Lets us recover dropped data weeks later. |
| **WAL** | Write-Ahead Log. Postgres's transaction log. Archived for point-in-time recovery. |
| **PITR** | Point-In-Time Recovery. Restore DB to any second within retention window. |
| **R2** | Cloudflare's S3-compatible object storage. Free egress (unlike AWS S3). |
| **GPG symmetric** | Encryption with shared passphrase (no public/private key pair). |
| **alembic** | SQLAlchemy migration tool. Tracks applied migrations in `alembic_version` table. |
| **gunicorn** | Python WSGI/ASGI server. Run with `uvicorn.workers.UvicornWorker` for async support. |
| **PAT** | Personal Access Token. GitHub credential with scoped permissions. |
| **rclone** | Multi-cloud sync tool. We use it for R2. |
| **buildx** | Docker plugin for multi-platform builds. |
| **healthcheck** | Docker feature: container periodically runs a command to verify it's serving. Status visible in `docker ps`. |
| **Atomic switch** | A change that cannot be observed in a half-applied state. Symlink rename + `nginx -s reload` is atomic from any client's perspective. |
| **Auto-rollback** | If a deploy fails any health gate, automatically revert (no human action needed). |
| **Grace period** | Wait time after switching traffic to new color, before stopping old. Lets in-flight requests complete on the old color. |

---

## Appendix: Latest commit references

| Repo | Latest commit (2026-05-05) | What it contains |
|------|---------------------------|------------------|
| `lionstudio_infra` | `0880ef8` | Initial bootstrap (nginx config, deploy skeletons, docs/) |
| `style_ai_admin` | `<latest-after-blue-green-update>` | Dockerfile, axios fix, build-arg config, blue-green workflow |
| `lionstudio-ai` | `<latest-after-blue-green-update>` | Dockerfile, postcss.config.mjs, blue-green workflow |
| `style_ai_backend` | `<latest-after-blue-green-update>` | Dockerfile, .dockerignore, docker-entrypoint.sh, blue-green workflow |

---

**End of document.**

For questions, contact: `mobilionsteam@gmail.com`
