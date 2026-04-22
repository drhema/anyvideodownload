# pyvid

Browser-intercept video downloader. Opens a real Chromium via Playwright, lets
the page load and authenticate naturally, classifies the network traffic, and
downloads whatever stream it finds using the browser's own cookies + headers.

## Status

| Transport / Site | Status |
|---|---|
| Progressive (plain MP4/WebM/MOV/MKV) | ✅ **tested on archive.org** |
| HLS (`.m3u8`, incl. AES-128 via ffmpeg) | ✅ **tested on Apple fMP4 sample** |
| DASH (`.mpd`, VOD) | ✅ **tested on dash.akamaized.net BBB 4K** |
| Vimeo adaptive (`playlist.json`) | ✅ **tested on vimeo.com** |
| TikTok (unwatermarked via webapp bitrateInfo) | ✅ **tested on tiktok.com**, verified no watermark |
| Twitch live / VOD (HLS via existing transport) | ✅ **tested on twitch.tv**, 1080p live stream captured |
| Twitter / X (HLS via existing transport) | ✅ **tested on x.com**, 1080p 1h48m SpaceX replay captured |
| YouTube UMP / SABR | ✅ **end-to-end working**, 100% duration capture (format-bucket reassembly) |
| Instagram (public reels/posts) | ✅ **tested on instagram.com**, 720p H.264 |
| Facebook | ⚠️ module shipped (same fbcdn logic); requires URL that plays without login |
| DRM (Widevine/PlayReady/FairPlay) | ❌ not supported, will not be |

## Install

```bash
cd /Users/dribrahimm/0-video-ai-platform/playwright/pyvid
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

`ffmpeg` must be on PATH (`brew install ffmpeg`).

## Two ways to use it

1. **`pyvid` CLI** — one-shot download from the terminal (below)
2. **`pyvid-api` HTTP service** — submit jobs, poll, stream files ([API section](#http-api))

## CLI usage

```bash
# simplest: open page, grab the best stream, save to ./downloads/output.mp4
pyvid "https://example.com/some-video-page"

# just see what the sniffer finds, don't download
pyvid "https://example.com/..." --dry-run

# force a transport
pyvid "https://example.com/..." --format hls

# longer capture window for slow pages
pyvid "https://example.com/..." --idle-ms 8000 --max-ms 300000
```

Output goes to `./downloads/output.mp4` by default (`-o` to change).

## Architecture

```
src/pyvid/
  core/
    session.py        Playwright wrapper, records every response
    sniffer.py        Classifies captures into Candidates
    orchestrator.py   Picks candidate, dispatches to transport
    mux.py            ffmpeg wrappers (concat / remux / mux-tracks)
    types.py
  transports/
    progressive.py    Single-URL download, range-aware
    hls.py            m3u8 parser + segment fetcher + ffmpeg
    dash.py           mpd parser + SegmentTemplate/List/Base + segment fetcher
    base.py           shared session-headers helper
  sites/
    youtube.py        (stub — plan in file)
  cli.py              `pyvid` entry point
```

Adding a new site is usually ~50 lines in `sites/<host>.py` — the transports do
the real work.

## Roadmap

### YouTube — known limits
Works via browser-intercept reassembly (lets Chromium handle PoToken /
n-param / cipher). Format-bucket grouping in [sites/youtube.py](src/pyvid/sites/youtube.py)
classifies streams by container signature (fMP4 vs WebM) and concatenates
all same-format streams in stream_id order — producing 100% duration
capture for the short videos tested. Known caveats:
- **Quality**: whatever ABR selected, typically 240p–480p AV1/VP9. Explicit
  format selection would require `/youtubei/v1/player` enumeration.
- **Longer videos**: short videos (tested 19s) capture fully in one UMP
  response. For longer videos where the browser fetches in multiple HTTP
  responses, 2x playback-to-end should still capture everything since
  each response is demuxed independently — but not yet validated.
- **Ads**: pre-roll ads may pollute stream IDs. Not yet filtered.

### Instagram / Facebook — known limits
Both serve MP4 from `*.fbcdn.net` with `bytestart`/`byteend` byte-range
params in the URL. Site modules strip those params so the progressive
transport fetches the full file.
- **Instagram**: public reels/posts work. Private content requires login.
- **Facebook**: most videos require login to render in Chromium. Page
  loads but the player never surfaces an MP4 URL anonymously. Module
  will work when a URL that plays without login is encountered. For
  authenticated content, future work: persist a browser profile
  (`--user-data-dir`) across runs.

### Sites
- [x] Vimeo
- [x] TikTok
- [x] Twitch (live + VOD — no site module needed, HLS transport handles it)
- [x] Twitter / X (no site module needed, HLS transport handles it)
- [x] YouTube (browser-intercept UMP reassembly, 100% duration)
- [x] Instagram public (fbcdn MP4 with byte-range strip)
- [x] Facebook (same module as Instagram; works when content renders anonymously)

### Other
- [ ] Live (dynamic) DASH support
- [ ] Subtitle extraction (VTT / TTML / in-manifest renditions)
- [ ] Per-host rate limiting + retry
- [ ] Tests against recorded HAR fixtures (no network in CI)
- [x] HTTP API (FastAPI) — see below

## What this tool will NOT do

- Bypass DRM (Widevine / PlayReady / FairPlay). These are used by Netflix,
  Prime Video, Disney+, HBO Max, Apple TV+, Hulu, Spotify, and many other paid
  services. Bypassing them is a DMCA §1201 violation and technically requires
  extracting keys from a device's secure enclave. Not happening.
- Work against `<encrypted-media>` streams with a license server URL.

Non-DRM "secure" streams (token-gated, signed URLs, signature ciphers executed
by the page's JS, session cookies) are fine — the browser handles all of that
and we inherit the authenticated context.

## HTTP API

Start the server:

```bash
pyvid-api
# or with config:
PYVID_API_TOKENS="my-secret-1,my-secret-2" \
PYVID_STORAGE="./downloads" \
PYVID_CONCURRENCY=1 \
PYVID_RATE_LIMIT=10 \
PYVID_HOST=127.0.0.1 \
PYVID_PORT=8000 \
pyvid-api
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | liveness (no auth) |
| `POST` | `/download` | submit a URL, returns job id |
| `GET`  | `/jobs` | list all jobs |
| `GET`  | `/jobs/{id}` | job status JSON |
| `GET`  | `/jobs/{id}/file` | stream the downloaded MP4 |
| `DELETE` | `/jobs/{id}` | remove job + its files |

OpenAPI docs at `http://127.0.0.1:8000/docs`.

### Example flow

```bash
# submit
curl -X POST http://127.0.0.1:8000/download \
  -H 'Authorization: Bearer my-secret-1' \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.tiktok.com/@user/video/123", "max_ms": 45000}'
# → {"id":"a7a0db04a275","status":"queued", ...}

# poll
curl -H 'Authorization: Bearer my-secret-1' \
  http://127.0.0.1:8000/jobs/a7a0db04a275
# → {"status":"completed","file_size_bytes":1329589, ...}

# download the MP4
curl -L -o video.mp4 \
  -H 'Authorization: Bearer my-secret-1' \
  http://127.0.0.1:8000/jobs/a7a0db04a275/file
```

### Config env vars

| Var | Default | Notes |
|---|---|---|
| `PYVID_API_TOKENS` | *(empty)* | comma-separated bearer tokens; empty = auth disabled |
| `PYVID_STORAGE` | `./pyvid-storage` | where downloads are saved |
| `PYVID_CONCURRENCY` | `1` | parallel download workers. Chromium is heavy — raise carefully |
| `PYVID_RATE_LIMIT` | `10` | requests/min per token (0 = disabled) |
| `PYVID_HOST` | `127.0.0.1` | bind host. Use `0.0.0.0` to expose externally |
| `PYVID_PORT` | `8000` | bind port |
| `PYVID_CHROMIUM_ARGS` | *(empty)* | extra Chromium args (e.g., `--no-sandbox` in Docker) |

### Operational notes

- **State is in-process memory.** Jobs and files persist on disk but the job
  index is lost on restart. Files remain in `PYVID_STORAGE/{job_id}/` and can
  be re-indexed by scanning that directory (not implemented yet).
- **Concurrency is dangerous.** Each worker launches a full Chromium; 3–4
  concurrent workers will saturate typical laptops. Default 1.
- **No auto-cleanup.** Jobs + files persist until `DELETE`d. Add a cron job
  or periodic sweep if you deploy long-running.
- **Single-process only.** The rate limiter and job index are per-process.
  Running multiple uvicorn workers (`--workers 2`) would duplicate state.
  Swap the in-memory limiter for Redis if you need multi-worker scaling.

### Docker

```bash
# Local dev on Mac or Linux — builds image, starts server on :8000
docker compose up --build

# Or with env overrides
PYVID_API_TOKENS="secret1,secret2" \
PYVID_HOST_PORT=8000 \
PYVID_CONCURRENCY=1 \
docker compose up -d --build
```

Downloads land in `./downloads/` on the host (mounted to `/data` inside
the container).

**What's inside the image** (`Dockerfile`):

- `mcr.microsoft.com/playwright/python:v1.58.0-noble` base — Ubuntu Noble +
  Chromium + all the shared libs Chrome needs on Linux.
- `ffmpeg` for muxing + `xvfb` so Chromium runs "headed" on a virtual X
  display (headed mode avoids the bot-detection many sites fingerprint).
- `tini` as PID 1 so SIGTERM propagates cleanly on `docker stop`.
- Runs as the non-root `pwuser` account provided by the Playwright image.
- `--no-sandbox --disable-dev-shm-usage` baked in via `PYVID_CHROMIUM_ARGS`
  (required when Chromium runs inside a container).

**Required compose settings**:

```yaml
shm_size: "1gb"        # Chromium crashes on the default 64MB /dev/shm
```

### Deploy to Ubuntu (production)

End-to-end recipe to go from a fresh Ubuntu 22.04/24.04 server to a
running, TLS-protected pyvid API. Tested on Ubuntu 24.04 LTS.

#### 0. Prerequisites

| Requirement | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 2 GB | 4 GB (per concurrent worker) |
| Disk | 10 GB | 50 GB+ (depends on retention policy) |
| OS | Ubuntu 22.04 / 24.04 | 24.04 LTS |
| Open ports | 22 (SSH), 443 (HTTPS) | same |
| Domain | optional | strongly recommended for TLS |

#### 1. Install Docker (if not already)

```bash
# Remove any old packages
sudo apt remove -y docker docker-engine docker.io containerd runc 2>/dev/null

# Install prerequisites
sudo apt update
sudo apt install -y ca-certificates curl gnupg

# Add Docker's official GPG key and repo
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Run docker without sudo (log out/in to apply)
sudo usermod -aG docker $USER
```

#### 2. Get the code

```bash
sudo mkdir -p /opt/pyvid && sudo chown $USER:$USER /opt/pyvid
cd /opt/pyvid

# Option A: clone from your git repo
git clone <your-git-url> .

# Option B: scp from your laptop
# scp -r /path/to/pyvid user@server:/opt/pyvid/
```

#### 3. Create a production `.env`

**Do not commit this file.** It is in `.gitignore`.

```bash
cd /opt/pyvid
cat > .env <<'EOF'
# Strong token — generate with: openssl rand -hex 32
PYVID_API_TOKENS=CHANGEME-USE-openssl-rand-hex-32

# Bind only to loopback; let the reverse proxy terminate TLS
PYVID_HOST_PORT=127.0.0.1:8000

# Keep low unless you have the RAM — each worker is a full Chromium
PYVID_CONCURRENCY=1

# Per-token sliding window
PYVID_RATE_LIMIT=30
EOF
chmod 600 .env
```

Regenerate the token:
```bash
sed -i "s/^PYVID_API_TOKENS=.*/PYVID_API_TOKENS=$(openssl rand -hex 32)/" .env
```

#### 4. Build + start

```bash
cd /opt/pyvid
docker compose up -d --build
docker compose ps
docker compose logs -f   # Ctrl-C to exit; container keeps running
```

Healthcheck:
```bash
curl -s http://127.0.0.1:8000/health
# → {"ok":true,"auth_enabled":true,...}
```

Quick end-to-end test (replace TOKEN):
```bash
TOKEN=$(grep PYVID_API_TOKENS .env | cut -d= -f2)
curl -X POST http://127.0.0.1:8000/download \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.tiktok.com/@bellapoarch/video/6862153058223197445"}'
```

#### 5. Reverse proxy with TLS (Caddy — simplest)

Caddy auto-provisions Let's Encrypt certificates. No manual certbot needed.

```bash
sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile <<'EOF'
pyvid.your-domain.com {
    reverse_proxy 127.0.0.1:8000 {
        header_up X-Real-IP {remote}
        transport http {
            # Video download streams can be slow and big — don't cut them off
            response_header_timeout 10m
            read_timeout 30m
        }
    }
    # Allow very large file downloads
    request_body {
        max_size 10MB
    }
    encode gzip
    log {
        output file /var/log/caddy/pyvid.log
        format console
    }
}
EOF
sudo systemctl reload caddy
```

If you're using **Traefik** or **Nginx** instead, similar idea — proxy
`127.0.0.1:8000` and make sure idle/read timeouts are at least 10 minutes
(large video downloads are slow).

DNS: point `pyvid.your-domain.com` A record at the server's public IP.
Caddy will fetch a TLS cert on first request (takes ~5 seconds).

#### 6. Firewall

```bash
# Only allow SSH + HTTPS from the public internet
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

pyvid listens on `127.0.0.1:8000` only — never exposed to the public internet
directly. All external traffic goes through Caddy on :443.

#### 7. Portainer (optional UI)

Portainer is just a Docker UI — install it alongside:

```bash
docker volume create portainer_data
docker run -d \
  --name portainer \
  --restart unless-stopped \
  -p 127.0.0.1:9443:9443 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v portainer_data:/data \
  portainer/portainer-ce:latest
```

Add a Caddy block for Portainer too:
```
portainer.your-domain.com {
    reverse_proxy 127.0.0.1:9443 {
        transport http { tls_insecure_skip_verify }
    }
}
```

In Portainer:
- **Stacks → Add stack → Web editor**
- Paste the contents of `docker-compose.yml` (edit `build: .` to
  `image: your-registry/pyvid:0.1.0` if you're pulling from a registry)
- Set env vars under **Environment variables** (mirrors `.env` above)
- Click **Deploy the stack**

### Operations

#### Logs

```bash
cd /opt/pyvid
docker compose logs -f              # live tail
docker compose logs --tail 200      # last 200 lines
docker compose logs --since 1h      # last hour
```

Caddy logs: `/var/log/caddy/pyvid.log`.

#### Updates

```bash
cd /opt/pyvid
git pull                            # or scp the new code
docker compose up -d --build        # rebuild + restart
```

Downtime is ~15 seconds while Chromium starts. In-flight jobs are lost
(no queue persistence yet — that's on the roadmap).

#### Backups

The only thing worth backing up is `.env` (your tokens). The downloaded
files in `./downloads/` can be re-fetched by resubmitting the URL.

```bash
# Backup token file to a secure location
scp /opt/pyvid/.env admin@backup.example.com:/secure/backups/pyvid-$(date +%F).env
```

#### Cleanup (disk hygiene)

No automatic cleanup yet. If downloads pile up, add a cron job:

```bash
# Delete files older than 24h from the downloads volume
sudo crontab -e
# Add:
0 * * * * find /opt/pyvid/downloads -type f -mtime +1 -delete
0 * * * * find /opt/pyvid/downloads -type d -empty -delete
```

Or trigger via the API: `DELETE /jobs/{id}` removes both the job record
and its files.

#### Monitoring

Basic liveness monitoring — Caddy already has retries; add UptimeRobot /
Healthchecks.io / cron ping:

```bash
*/5 * * * * curl -fsS https://pyvid.your-domain.com/health > /dev/null \
  || echo "pyvid down at $(date)" | mail -s "pyvid alert" ops@your-domain.com
```

### Resource sizing cheat sheet

| Scenario | CPU | RAM | Disk | Concurrency |
|---|---|---|---|---|
| Personal use (<100 downloads/day) | 2 vCPU | 2 GB | 20 GB | 1 |
| Small team (~1k/day) | 4 vCPU | 4 GB | 50 GB | 2 |
| Heavy / SaaS-scale | 8+ vCPU | 8+ GB | 200+ GB | 4+ (add Redis) |

For >1 concurrent worker, you'll also want to swap the in-memory rate
limiter for Redis (noted in the Operational caveats below).

### Docker test results (local Mac, Docker Desktop)

Tested all supported sites via the HTTP API running inside the container:

| Site | Result | File | Notes |
|---|---|---|---|
| TikTok | ✅ | 1.27 MB | 576×1024 H.264, 10.7s, unwatermarked |
| Vimeo | ✅ | 7.02 MB | 480×848 H.264, 37.6s |
| Instagram (public reel) | ✅ | 2.28 MB | 720×1280 H.264, 37.7s |
| YouTube (UMP reassembly) | ✅ | 468 KB | AV1 320×240, 19.0s — full duration |
| archive.org (progressive) | ✅ | 59 MB | 640×360 H.264, 9:56 |
| Facebook (public video) | ✅ | 1.44 MB | 720×1280 H.264, 9.9s |
| Twitch (live, retry) | ✅ | 21.75 MB | 1920×1080 H.264 + AAC, 28s live snapshot. First attempt failed with transient DNS, retry succeeded. |

### Status codes reference

| Code | Meaning |
|---|---|
| 200 | OK |
| 202 | job submitted |
| 401 | missing / bad bearer token |
| 404 | job not found |
| 409 | job still running (file not yet available) |
| 410 | job failed, or output file deleted |
| 429 | rate limit hit — respect `Retry-After` header |
