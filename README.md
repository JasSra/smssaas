# SmsSaaS — phone backend for Inboxr

Real Australian (and any-carrier-supported) SMS + voice powered by physical
Android phones, USB / WiFi / network-passthrough'd to a Docker host, controlled
by a tiny Android APK that dispatches via `SmsManager.sendTextMessage()` and
the system `TelecomManager` for calls.

This is the backend that powers SMS + voice for [getinboxr.app](https://getinboxr.app).
Self-hostable. License required for commercial use — free for personal,
evaluation, and internal-only company use.

## What's in the box

| Component | Purpose | Image |
|---|---|---|
| `api` (FastAPI) | REST surface for inboxr-cloud + the APK to talk to. SQLite, Redis-backed telemetry, IVR engine, Whisper transcription. | `jassra/smssaas-api:latest` |
| `adb-bridge` (bash + adb) | Watches USB / WiFi for Android devices. Sets up reverse tunnels. Optionally runs a Python ADB worker as fallback when no APK is installed. | `jassra/smssaas-adb-bridge:latest` |
| `android-app` (Kotlin) | The APK that actually sends/receives SMS and bridges call audio. Source in this repo; built APK is sideloaded via ADB at install time. | local build |

## Quick start

```bash
# 1. Pull the public compose template
mkdir smssaas && cd smssaas
wget https://raw.githubusercontent.com/JasSra/smssaas/main/docker-compose.public.yml -O docker-compose.yml
wget https://raw.githubusercontent.com/JasSra/smssaas/main/.env.example -O .env

# 2. Edit .env — minimum:
#   ADMIN_KEY=$(openssl rand -hex 32)
#   DEVICE_SECRET=$(openssl rand -hex 16)
#   # optional but recommended for voice:
#   OPENAI_API_KEY=sk-...
#   ANTHROPIC_API_KEY=sk-ant-...

# 3. Bring it up
docker compose up -d

# 4. Plug a Pixel into the host with USB debugging enabled, accept the
#    "Allow USB debugging?" prompt on the phone (tap Always allow).
docker compose exec adb-bridge adb devices
# Should list your phone as `device` (not `unauthorized`).

# 5. Build + install the APK on the phone (one-time per device).
#    See android-app/INSTALL.md for the build pipeline.
```

After step 5, the API is live at `http://localhost:8300` and the phone
auto-registers. Point your inboxr-cloud at it via:

```
SMS_API_URL=http://<this-host>:8300
SMS_ADMIN_KEY=<same as ADMIN_KEY above>
```

## API surface

All endpoints require `X-Admin-Key: $ADMIN_KEY` (or `Authorization: Bearer ...`
for tenant-scoped `/v1/*` paths).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/admin/phones` | Fleet view: phone state, signal, battery, queue depth |
| GET | `/admin/phones/online` | Redis-backed presence (sorted set) |
| GET | `/admin/diag/{device_id}` | 7-day diagnostic stream (battery / signal / queue) |
| GET | `/admin/recent/sms` | Live fleet-wide SMS feed (1000-entry capped Stream) |
| GET | `/admin/recent/calls` | Live fleet-wide call state feed |
| POST | `/admin/sms/test` | Send a test SMS (used by the cloud's tenant proxy) |
| POST | `/v1/voice/calls` | Place an outbound call |
| GET | `/v1/voice/ivr-flows` | List IVR flows |
| POST | `/v1/voice/ivr-flows/from-description` | AI-generate a flow from English (Anthropic) |
| GET | `/v1/voice/tts/voices` | List OpenAI TTS voices |

Full spec: see `api/main.py` for the route registrations.

## License

| Use | License |
|---|---|
| Personal / OSS / evaluation | Free, no license needed |
| Internal company use (no external customers) | Free, fair-use |
| External commercial use | Per-instance license — request at [getinboxr.app/license/request](https://getinboxr.app/license/request) |

License plumbing is built but **not yet enforced** on this image. We'll flip
enforcement on with at least 30 days notice. Set `INBOXR_LICENSE_ENFORCE=warn`
in the cloud's env to see warnings without rejection during the transition.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  inboxr-cloud (or any HTTP client)                                  │
│      │  REST + SSE + WebSocket                                      │
│      ▼                                                              │
│  smssaas-api (FastAPI)  ──────────  Redis (optional)                │
│      │                                                              │
│      ▼                                                              │
│  smssaas-adb-bridge  ──USB/WiFi──▶  Android phone(s)                │
│                                       ├─ SmsSaaS APK                │
│                                       │   ├─ SmsWorkerService       │
│                                       │   ├─ VoiceWorkerService     │
│                                       │   └─ SensorWorkerService    │
│                                       └─ SIM ───── Carrier          │
└─────────────────────────────────────────────────────────────────────┘
```

## Source

This repo contains:

- `api/` — FastAPI service. Python 3.12.
- `adb-bridge/` — bash watcher + Python ADB shim. Ubuntu 24.04.
- `android-app/` — Kotlin Android app. AGP 8.5.2, Gradle 8.7, target SDK 34.
- `docker-compose.public.yml` — the one-shot compose for self-hosters.
- `docker-compose.yml` — the dev compose with local builds.
- `docs/FLEET-ONBOARDING.md` — adding more phones to a deployed fleet.

## Support

Issues + discussions: github.com/JasSra/smssaas. For a managed SaaS — same code,
zero ops — use [getinboxr.app](https://getinboxr.app).
