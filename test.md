# Lions Studio AI — Infrastructure Documentation

**Last updated:** 2026-05-01
**Status:** Production
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
7. [CI/CD pipeline](#7-cicd-pipeline)
8. [nginx reverse proxy](#8-nginx-reverse-proxy)
9. [Backups (5-layer protection)](#9-backups-5-layer-protection)
10. [Migrations — the most important section](#10-migrations--the-most-important-section)
11. [Secrets and credentials map](#11-secrets-and-credentials-map)
12. [File-by-file reference](#12-file-by-file-reference)
13. [Operations runbook](#13-operations-runbook)
14. [Open work](#14-open-work)
15. [Glossary](#15-glossary)

---

## 1. What this document is

This document explains everything about how **Lions Studio AI** runs in production. After reading this, you should be able to:

- Deploy a new version of any app
- Recover from a database failure
- Add a new migration safely
- Rotate secrets
- Diagnose problems

It assumes basic knowledge of Linux, Docker, Git, and HTTP. No prior knowledge of this project is needed.

---

## 2. The big picture

A **single Ubuntu server** runs everything. Three apps run inside Docker containers, one app is served as static files. PostgreSQL runs directly on the host (not in Docker). nginx is the reverse proxy that routes requests to the right app. GitHub Actions auto-deploys on every push to the `main` branch. A daily encrypted backup of the database goes to Cloudflare R2.

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
              ▼          ▼               ▼           ▼
        Docker      Docker           Docker      Static files
        port 4001   port 3004        port 8001    /var/www/.../web
        Next.js     Next.js+MUI      FastAPI      Flutter web
        (main)      (admin)
                                      │
                                      ▼
                                 Postgres 14
                              (host, 127.0.0.1:5432)
                                      │
                                      ▼ daily 02:00 UTC
                            pg_dump → gpg → rclone
                                      │
                                      ▼
                            Cloudflare R2 (private bucket)
```

**Why this shape?**
- Single server keeps it cheap (one VM)
- Docker for apps gives reproducible deploys + version control via image tags
- Postgres on host keeps it simple — no docker-postgres networking, easy backups
- nginx routes by URL path so all apps share one domain
- Cloudflare R2 because egress is free (no surprise bill if you restore)

---

## 3. The server

| Property | Value |
|----------|-------|
| Hostname | `style-ai-vnic` |
| OS | Ubuntu 22.04 ARM64 (`aarch64`) |
| Disk | 97 GB total, 78 GB free |
| Memory | (check with `free -h`) |
| TLS | Let's Encrypt via certbot (auto-renewing) |
| SSH access | as `root` and `ubuntu` users |

**Important consequence of ARM64:** Docker images must be built for `linux/arm64`. Dev laptops are usually `x86_64`, so:
- Local builds need `docker buildx --platform linux/arm64`
- GitHub Actions uses QEMU emulation (slow ~10-15 min) or `ubuntu-24.04-arm` runner (paid, fast)
- A regular `docker build` from Windows/Mac produces an x86 image that **fails on the server** with `no matching manifest for linux/arm64/v8`

---

## 4. Apps overview

| URL path | Tech | Container name | Host port → Container port | Source repo |
|----------|------|----------------|----------------------------|-------------|
| `/` | Next.js 16 + Tailwind v4 | `lionstudio-test` | 4001 → 3000 | mobilions/lionstudio-ai |
| `/admin` | Next.js + MUI v9 | `admin-test` | 3004 → 3002 | mobilions/style_ai_admin |
| `/api` | FastAPI 3.12 + asyncpg | `backend-test` | 8001 → 8000 (`--network host`) | mobilions/style_ai_backend |
| `/app` | Flutter (web) | n/a — static | n/a | mobilions/style_ai_frontend |
| `/grafana` | Grafana | `grafana` (existing) | 3000 → 3000 | n/a (already-running) |

**Why the `-test` suffix?** Historical artifact from the manual proof phase. Will be renamed during Phase F (blue-green automation).

**Branch convention:**
- `main` = production. Push here triggers auto-deploy.
- `development` (or `dev`) = ongoing work. Force-push allowed here, NOT on main.

---

## 5. Database

### Setup
- **PostgreSQL 14.22** on Ubuntu 22.04 ARM64
- **Host install** (NOT in Docker — locked decision)
- **Port:** `127.0.0.1:5432` (loopback only — not exposed externally)
- **Authentication:** `peer` for postgres OS user, `md5` for `mobilions` user

### Databases
| Name | Owner | Purpose | Size |
|------|-------|---------|------|
| `lionstudio_test` | `mobilions` | Production data | 11 MB |
| `lionstudio_dev` | `postgres` | Unused | empty |

### Connection from FastAPI
```
postgresql+asyncpg://mobilions:PASSWORD@localhost:5432/lionstudio_test
```

Stored in `/var/www/html/style_ai_backend/.env`. The Docker container also uses `localhost:5432` because it runs with `--network host` (shares the host's network namespace), so `localhost` inside the container = the actual host's localhost.

### Why Postgres is NOT in Docker
1. Backups are simpler (just `pg_dump` from host)
2. No Docker networking complexity for connecting from app containers
3. No volume management for the data directory
4. Single source of truth: one Postgres instance, no risk of accidentally running two

---

## 6. Container registry (GHCR)

We use **GitHub Container Registry** because it's free for org-owned repos.

| Image | Tags |
|-------|------|
| `ghcr.io/mobilions/lionstudio-ai` | `<commit-sha>`, `latest` |
| `ghcr.io/mobilions/style_ai_admin` | `<commit-sha>`, `latest` |
| `ghcr.io/mobilions/style_ai_backend` | `<commit-sha>`, `latest` |

**Authentication for push (CI):** Personal Access Token (PAT) with `write:packages` scope, stored as `GHCR_TOKEN` in each repo's GitHub Secrets.

**Authentication for pull (server):** The deploy script does `docker login ghcr.io` inside the SSH session each deploy, using the same `GHCR_TOKEN` passed via env var. Not persistent on the server.

**Tagging strategy:**
- Every push to `main` produces an image tagged with its commit SHA — this is what gets deployed
- `latest` always points to the most recent main build (used for manual `docker pull` debugging only)

---

## 7. CI/CD pipeline

Each app repo has `.github/workflows/cd.yml` with the same 2-job structure.

### Trigger
```yaml
on:
  push:
    branches: [main]
  workflow_dispatch:    # manual trigger from GitHub UI
```

Triggered by **push to `main`**. The `dev` branch does NOT trigger deploys.

### Job 1: build-and-push

Runs on `ubuntu-latest`. Total ~10-15 minutes.

```
Steps:
1. Checkout code
2. Set up QEMU (linux/arm64 emulation on x86 runner)
3. Set up Docker Buildx
4. Log in to GHCR (using GHCR_TOKEN secret)
5. Compute image tag = ghcr.io/mobilions/<repo>:<commit-sha>
6. Build image with platforms: linux/arm64
7. Push to GHCR with tags: <commit-sha> + latest
8. Cache layers via type=gha (faster subsequent builds)
```

### Job 2: deploy

Runs on `ubuntu-latest`. Total ~30 seconds (image already built and pushed).

```
Steps:
1. Set up SSH (write SSH_PRIVATE_KEY to ~/.ssh/deploy_key, chmod 600)
2. ssh -i deploy_key ubuntu@SSH_HOST bash << EOF
     # All this runs on the server:
     docker login ghcr.io -u mobilions  (uses GHCR_TOKEN passed via env)
     docker pull <new-image>
     docker run --rm <new-image> alembic upgrade head   # backend only
     docker stop <old-container>
     docker run -d --name <new-container> ... <new-image>
     # Wait up to 40s for healthcheck (healthy)
     # Smoke test: curl /api/health (or / for frontends)
   EOF
```

If migration fails (backend only): exit non-zero, old container keeps serving. No partial deploy.

### GitHub Secrets per repo

All 3 app repos (admin, main, backend) share the same secret values:

| Secret | Value | Used for |
|--------|-------|----------|
| `SSH_HOST` | server IP/hostname | `ssh` target |
| `SSH_PRIVATE_KEY` | contents of `/root/.ssh/github_actions_deploy` | SSH authentication |
| `GHCR_TOKEN` | PAT with `write:packages` scope | docker login |

### Server-side prep (one-time, already done)

```
/root/.ssh/github_actions_deploy.pub  →  copied to  /home/ubuntu/.ssh/authorized_keys
ubuntu user                            →  added to  docker group  (no sudo for docker)
```

---

## 8. nginx reverse proxy

### Files
- Active config: `/etc/nginx/sites-enabled/lionstudio_dev` (symlink)
- Source: `/etc/nginx/sites-available/lionstudio_dev`
- Tracked copy: `/opt/lionstudio_infra/nginx/lionstudio_dev.conf` (in lionstudio_infra repo)

### Routes
```
/api/*           → 127.0.0.1:8001  (Docker backend)
/admin/*         → 127.0.0.1:3004  (Docker admin)
/admin/<bad>     → 302 redirect to https://dev.lionstudioai.com/  (via @admin_404_redirect named location)
/                → 127.0.0.1:4001  (Docker main site)
/app/*           → static files at /var/www/html/style-ai-fe/web/
/grafana/*       → 127.0.0.1:3000  (existing Grafana)
```

### TLS
- Issuer: Let's Encrypt
- Renewal: certbot via systemd timer (auto-renews 30 days before expiry)

### Reload nginx after config changes
```bash
sudo nginx -t              # validate syntax
sudo systemctl reload nginx  # zero-downtime reload (keeps existing connections alive)
```

---

## 9. Backups (5-layer protection)

The plan is **5 layers of data protection**, each catching different failure modes. Today layers 1-4 are wired up; layer 5 (WAL archiving) is planned.

### Layer 1 — Expand-contract migrations ✅ ACTIVE
**Catches:** Bad schema changes that would lose data
**How:** Forward-only migrations. Every migration must be safe to run while old code is still running. See [Section 10](#10-migrations--the-most-important-section).

### Layer 2 — Shadow tables ⬜ PLANNED
**Catches:** Accidentally dropping a column you still need
**How:** Instead of `DROP COLUMN x`, helper creates `_shadow_<table>_<col>_<date>` table with the data first, then drops the column. Nightly cron deletes shadows older than 30 days. Lets you recover dropped data weeks later.

### Layer 3 — Pre-deploy snapshot ⬜ PLANNED
**Catches:** Migration that breaks the schema
**How:** Before each deploy, `pg_dump` to `/var/backups/lionstudio/pre_deploy_<timestamp>.sql.gz`. 24h retention. Lets us instantly restore if a migration corrupted the DB.

### Layer 4 — Daily encrypted backup to R2 ✅ ACTIVE
**Catches:** Server destroyed, drive failure, accidental DROP DATABASE
**How:** Cron at 02:00 UTC runs `pg_dump | gpg --symmetric AES256 | rclone copy to R2`.
- Local retention: 2 days
- R2 retention: 30 days

### Layer 5 — WAL archiving for PITR ⬜ PLANNED
**Catches:** Bad UPDATE/DELETE that ran 10 minutes ago
**How:** Postgres archives every committed transaction (WAL = Write-Ahead Log) to R2 in real-time. Restore to any point in time within the last 7 days. Bounds worst-case data loss to **seconds** even in catastrophic failure.

---

## 10. Migrations — the most important section

### Why migrations are dangerous

Code changes and Postgres schema changes happen at very different speeds:
- Code: ~5 seconds to swap a Docker container
- Schema: minutes (e.g., big `ALTER TABLE` on a large table)

During the gap, OLD code can hit NEW schema (or vice versa). Naive migrations cause downtime + data loss.

### Our discipline: expand-contract

> **Rule: every migration must be safe to run while old code is still running.**

#### ✅ SAFE (1 deploy)
- Add a new nullable column
- Add a new table
- Add an index `CONCURRENTLY` (doesn't lock the table)

#### ❌ DESTRUCTIVE (must be split into multiple deploys)
- Drop a column the old code reads
- Rename a column (= drop + add from DB's perspective)
- Make a column NOT NULL
- Change a column's type if old code writes the old type

### Example: rename `email` → `email_address` (4 deploys, the safe way)

| Deploy | Migration | Code change |
|--------|-----------|-------------|
| **1. Expand** | Add nullable `email_address` column | Code dual-writes to BOTH `email` and `email_address`. Reads from `email`. |
| **2. Backfill + switch reads** | `UPDATE users SET email_address = email WHERE email_address IS NULL` | Code reads from `email_address`, still writes both |
| **3. Stop old write** | `ALTER ... NOT NULL` on `email_address` | Code only reads + writes `email_address`. Stop writing `email`. |
| **4. Contract** | `shadow_drop_column('users', 'email')` | (no code change) |

Between each deploy, the old code can keep running safely. **This is how zero downtime works for schema changes.**

If you do this in 1 deploy, the moment you `ALTER TABLE ... RENAME`, every old container still hitting `email` crashes.

### Where migrations live

- Repo: `style_ai_backend`
- Path: `alembic/versions/*.py`
- Currently 24 migrations, latest = head (DB is up to date)
- `alembic/env.py` reads `DATABASE_URL` from `app/config.py` settings

### When migrations run during deploy

GitHub Actions cd.yml deploy job sequence:

```
1. Pull new image
2. Run alembic upgrade head in throwaway container   ← if FAILS, exit, don't swap
3. Stop old container
4. Start new container (same volumes, same env)
5. Wait up to 40 sec for (healthy) status
6. Smoke test: curl /api/health
```

If migration fails, the OLD container keeps running. Manual recovery: SSH in, fix migration, push again.

### Adding a new migration

On dev machine in `style_ai_backend`:
```bash
alembic revision -m "add user phone column"
# edit the generated file in alembic/versions/
git add alembic/versions/*.py
git commit -m "Migration: add user phone column"
git push origin main
# CI/CD runs `alembic upgrade head` as part of the deploy
```

### CI safety guards (planned, not yet implemented)

- Pre-commit hook that scans new migrations for `drop_column`, `drop_table`, `nullable=False`
- If found, blocks merge unless `# SAFE: reviewed forward-compat` comment is present
- Migration replay test: in CI, spin up empty Postgres → run all migrations → compare schema to SQLAlchemy models

---

## 11. Secrets and credentials map

| Secret | Where stored | Used by |
|--------|--------------|---------|
| GHCR PAT | `~/.git-credentials` (server) + GitHub Secrets in 3 repos as `GHCR_TOKEN` | `git push`, `docker login ghcr.io` |
| SSH deploy key | `/root/.ssh/github_actions_deploy{,.pub}` (server) + GitHub Secrets as `SSH_PRIVATE_KEY` | GitHub Actions ssh ubuntu@server |
| Postgres password | `.env` and `.env.docker` in `/var/www/html/style_ai_backend/` (mode 600 ubuntu:ubuntu) | FastAPI app reads via DATABASE_URL |
| R2 API token | `/root/.config/rclone/rclone.conf` (server, mode 600 root) | Backup script via rclone |
| GPG passphrase | `/etc/lionstudio_backup/gpg_passphrase` (mode 600 root) + offline (password manager) | Backup script encrypts dumps |
| Firebase service account | `/var/www/html/style_ai_backend/firebase_service_account.json` | FastAPI app at startup |
| GCP service account (Vertex AI) | `/var/www/html/style_ai_backend/gcp-agent-studio.json` | FastAPI app for google-genai |
| TLS certs | `/etc/letsencrypt/live/dev.lionstudioai.com/` (auto-renewing) | nginx |

### ⚠️ Known leaks (rotate ASAP)

- **GHCR PAT** (`ghp_ydodMUI...`) was pasted in chat multiple times during setup
- **GPG passphrase** initial value was leaked once. May still be on the active R2 backup.

### How to rotate the GHCR PAT
1. Go to https://github.com/settings/tokens, regenerate the token
2. Update `~/.git-credentials` on server with new token
3. Update each repo's `GHCR_TOKEN` GitHub Secret (admin, main, backend repos)
4. Test: trigger workflow_dispatch on any repo's CD workflow

### How to rotate the GPG passphrase
```bash
# Generate new
openssl rand -base64 32 | sudo tee /etc/lionstudio_backup/gpg_passphrase

# Save offline (password manager — do NOT paste in chat)
sudo cat /etc/lionstudio_backup/gpg_passphrase

# Delete old R2 backup encrypted with old key
rclone delete r2:lionstudio/db_backups/<old-file>.dump.gpg

# Run new backup with new passphrase
sudo /usr/local/bin/backup_lionstudio_db.sh
```

---

## 12. File-by-file reference

### `/var/www/html/style_ai_backend/Dockerfile`
Multi-stage Python 3.12-slim build.
- **Builder stage**: installs `gcc, libpq-dev, libffi-dev, libjpeg-dev, zlib1g-dev, libssl-dev`, creates a venv at `/opt/venv`, installs `requirements.txt` + `gunicorn`
- **Runtime stage**: copies just `/opt/venv` and the app code, creates non-root `appuser` (UID 999), exposes port 8000, healthcheck on `/api/health`
- Final size: ~650 MB / ~130 MB compressed

### `/var/www/html/style_ai_backend/.dockerignore`
Excludes from build context:
```
.git, venv/, __pycache__, *.pyc, .env*, *_service_account.json,
media/, logs/, .vscode/, .idea/, build artifacts
```
Keeps build context to ~1.25 MB and prevents secrets from being baked into the image.

### `/var/www/html/style_ai_backend/docker-entrypoint.sh`
Single-line: runs `gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --workers $WORKERS --bind 0.0.0.0:$PORT --timeout $TIMEOUT`. Defaults: PORT=8000, WORKERS=2, TIMEOUT=300. Logs go to stdout (Docker captures).

### `/var/www/html/style_ai_backend/.env.docker`
Whitespace-stripped copy of `.env`. Required because Docker's `--env-file` is strict about `KEY=VALUE` format (no spaces around `=`). Owned `ubuntu:ubuntu`, mode 600. Gitignored. Contains all 25 env vars from `.env`.

### `/var/www/html/style_ai_backend/.github/workflows/cd.yml`
GitHub Actions workflow. 2 jobs (build-and-push, deploy). Triggers on push to main + workflow_dispatch. ~131 lines.

### `/var/www/html/style_ai_admin/.github/workflows/cd.yml`
Same shape as backend, but no migration step. Container name `admin-test`, port 3004→3002.

### `/var/www/html/lionstudio-ai/.github/workflows/cd.yml`
Same shape. Container name `lionstudio-test`, port 4001→3000.

### `/usr/local/bin/backup_lionstudio_db.sh`
Daily backup script.
1. `pg_dump -Fc` (custom format with built-in compression)
2. Pipe through `gpg --symmetric --cipher-algo AES256` with passphrase from `/etc/lionstudio_backup/gpg_passphrase`
3. `rclone copy` to `r2:lionstudio/db_backups/`
4. Verify upload via `rclone lsf | grep`
5. Local cleanup: `find ... -mtime +2 -delete`
6. R2 cleanup: `rclone delete --min-age 30d`

### `/etc/cron.d/lionstudio_backup`
```
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 2 * * * root /usr/local/bin/backup_lionstudio_db.sh >> /var/log/lionstudio_backup_cron.log 2>&1
```
Runs daily at 02:00 UTC.

### `/root/.config/rclone/rclone.conf`
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
Mode 600. Token scoped to `lionstudio` bucket only (read+write).

### `/etc/lionstudio_backup/gpg_passphrase`
45-character random base64 string. Mode 600, root-only. **Critical**: also saved in offline password manager — losing this = losing access to all R2 backups.

### `/etc/nginx/sites-available/lionstudio_dev`
Main nginx config. Reverse-proxies to all the apps. Has `location @admin_404_redirect { return 302 https://dev.lionstudioai.com/; }` so invalid `/admin/*` URLs redirect home.

### `/etc/systemd/system/lionstudio_dev.service`
**DISABLED** systemd unit that used to run gunicorn directly on port 8000. Kept around for 48h as fallback in case Docker has issues. Will be deleted on **2026-05-03**.

### `/var/log/lionstudio_backup.log`
Append-only log of every backup run. Includes timestamps, dump sizes, R2 upload result, cleanup actions.

### `/var/log/lionstudio_backup_cron.log`
Cron's stdout/stderr capture (one line per backup run + any error output).

---

## 13. Operations runbook

### Deploy a new version
```bash
# On dev machine
git push origin main
# GitHub Actions auto-runs (~10-15 min)
# Watch: github.com/mobilions/<repo>/actions
```

### Manual deploy (skip CI/CD)
```bash
ssh ubuntu@server
docker pull ghcr.io/mobilions/<image>:<tag>
docker stop <container-name>
docker rm <container-name>
docker run -d --name <container-name> ... ghcr.io/mobilions/<image>:<tag>
```

### Rollback to previous version
```bash
ssh ubuntu@server
# Find the previous SHA
docker images | grep mobilions
# Pull and run it
docker pull ghcr.io/mobilions/<image>:<previous-sha>
docker stop <container>
docker run -d --name <container> ... ghcr.io/mobilions/<image>:<previous-sha>
```

### Restore from R2 backup (full DB recovery)
```bash
ssh root@server

# 1. Download backup
cd /tmp
rclone copy r2:lionstudio/db_backups/lionstudio_<timestamp>.dump.gpg .

# 2. Decrypt
gpg --batch --quiet --passphrase-file /etc/lionstudio_backup/gpg_passphrase \
    --decrypt lionstudio_<timestamp>.dump.gpg > restore.dump

# 3. Restore (DESTRUCTIVE — overwrites current DB)
sudo -u postgres pg_restore --clean --if-exists -d lionstudio_test restore.dump

# 4. Cleanup
rm -f restore.dump lionstudio_<timestamp>.dump.gpg
```

### Run a backup manually
```bash
ssh root@server
/usr/local/bin/backup_lionstudio_db.sh
```

### Add a new migration
On dev machine in `style_ai_backend`:
```bash
alembic revision -m "description"
# edit the generated file in alembic/versions/
git add alembic/versions/*.py
git commit -m "Migration: <description>"
git push origin main
# CI/CD runs `alembic upgrade head` as part of the deploy
```

### Check container health
```bash
ssh ubuntu@server
docker ps --filter name=backend-test
docker logs backend-test --tail 50
docker inspect --format='{{.State.Health.Status}}' backend-test
```

### View nginx access logs
```bash
ssh root@server
tail -f /var/log/nginx/access.log
```

### Renew TLS cert manually
```bash
sudo certbot renew --dry-run    # test
sudo certbot renew              # actual renewal
sudo systemctl reload nginx
```

### Switch nginx upstream port (for blue-green deploys)
```bash
sudo cp /etc/nginx/sites-available/lionstudio_dev /etc/nginx/sites-available/lionstudio_dev.bak.$(date +%Y%m%d_%H%M)
sudo sed -i 's|proxy_pass http://127.0.0.1:8001;|proxy_pass http://127.0.0.1:8002;|' /etc/nginx/sites-available/lionstudio_dev
sudo nginx -t && sudo systemctl reload nginx
```

---

## 14. Open work

| Priority | Item | When | Why |
|----------|------|------|-----|
| 🔴 Critical | Rotate leaked GHCR PAT | This week | Was pasted in chat multiple times |
| 🔴 Critical | Rotate leaked GPG passphrase | This week | Initial value leaked in chat |
| 🟠 High | Phase B — branch protection on main + Dependabot for all 4 repos | This week | Stops Tushar's force-push pattern |
| 🟠 High | Cleanup: delete `lionstudio_dev.service` unit, chown logs/media to appuser | 2026-05-03 (after 48h Docker stability) | Currently 777 perms, lazy fix |
| 🟡 Medium | Phase E — Flutter web pipeline (`style_ai_frontend`) | Next 2 weeks | Last app without CI/CD |
| 🟡 Medium | Phase F — Blue-green automation, auto-rollback | Next month | Today's deploy stops old before verifying new |
| 🟡 Medium | Layer 5 — WAL archiving to R2 (PITR) | Next month | Sub-second data loss bound |
| 🟡 Medium | Layer 3 — Pre-deploy snapshot script | Next month | Restore point if migration breaks DB |
| 🟢 Low | CI safety guards for migrations (pre-commit + replay test) | When time permits | Catch destructive migrations before merge |
| 🟢 Low | Move backup script + cron to `lionstudio_infra` repo | When time permits | Version control for infra scripts |
| 🟢 Low | Monitoring/alerting: alert if no backup in 25h | When time permits | Currently we'd find out only by checking |

---

## 15. Glossary

| Term | Meaning |
|------|---------|
| **GHCR** | GitHub Container Registry. Free Docker image hosting for org-owned repos. |
| **QEMU buildx** | Cross-architecture Docker build. We build ARM64 images on x86 GitHub runners using QEMU emulation. |
| **Expand-contract** | Migration pattern. Split destructive schema changes across multiple deploys so old code can keep running. |
| **Shadow table** | Pre-drop snapshot table (`_shadow_<table>_<col>_<date>`). Lets us recover dropped data weeks later. |
| **WAL** | Write-Ahead Log. Postgres's transaction log. Archived for point-in-time recovery. |
| **PITR** | Point-In-Time Recovery. Restore DB to any second within retention window. |
| **R2** | Cloudflare's S3-compatible object storage. Free egress (unlike AWS S3). |
| **GPG symmetric** | Encryption with shared passphrase (no public/private key pair). |
| **alembic** | SQLAlchemy migration tool. Tracks applied migrations in `alembic_version` table. |
| **gunicorn** | Python WSGI/ASGI server. We run with `uvicorn.workers.UvicornWorker` for async support. |
| **PAT** | Personal Access Token. GitHub credential with scoped permissions. |
| **rclone** | Multi-cloud sync tool. We use it for R2 (S3-compatible). |
| **buildx** | Docker plugin for multi-platform builds. |
| **healthcheck** | Docker feature: container periodically runs a command to verify it's serving. Status visible in `docker ps`. |
| **Let's Encrypt** | Free TLS certificate authority. Certs renew automatically via certbot. |

---

## Appendix: Commit reference

| Repo | Latest commit (2026-05-01) | What it contains |
|------|---------------------------|------------------|
| `lionstudio_infra` | `0880ef8` | Initial bootstrap (nginx config, deploy skeletons, docs/) |
| `style_ai_admin` | `f92fe00` | Dockerfile, .dockerignore, .gitignore (.env), 25+ source files for /admin/ path prefix |
| `lionstudio-ai` | `90899d9` | Dev branch merged to main; Dockerfile, postcss.config.mjs, .github/workflows/cd.yml |
| `style_ai_backend` | `0dabf92` | Dockerfile, .dockerignore, docker-entrypoint.sh, .github/workflows/cd.yml |

---

**End of document.**

For questions, contact: `mobilionsteam@gmail.com`
