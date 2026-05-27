# HTTP API

Reference for the REST surface served by the **control-api** container on
port 5556 (`bilbo.http.app`). All routes are versioned under `/api/v1/`.
The dashboard container reverse-proxies `/api/*` to `/api/v1/*`, so the
same routes work from the browser as `/api/<...>`.

The control-api is a thin dispatcher: each route parses the request and
hands off to the matching `bilbo.api.<module>.<func>`. The HTTP shape
mirrors that Python contract one-to-one — if you need response details
beyond what's tabulated here, the source is the truth.

## Conventions

- All times are ISO-8601 UTC with `Z` suffix (`2026-05-26T23:47:49Z`) unless noted.
- All endpoints return JSON unless they `send_file` (frame + recap video).
- Errors: `400` for bad query/body, `404` for missing data/file, `403` for
  forbidden paths (frame resolver). Internal errors surface as Flask 500.
- Responses pass through `_respond()`; a `_status` key inside the payload
  (currently unused) would override the HTTP status code.
- `_status` is stripped before serializing.

## Routes

### Health

| Method | Path        | Description |
|--------|-------------|-------------|
| GET    | `/healthz`  | Liveness probe. `{"ok": true}`. Not under `/api/v1/`. |

### Status & timeline

| Method | Path                    | Query                                    | Description |
|--------|-------------------------|------------------------------------------|-------------|
| GET    | `/api/v1/status`        | —                                        | Headline for the top of the dashboard: current state (`Asleep` / `Awake` / `Out of bassinet`), duration in current state, last frame path + age, active alerts. |
| GET    | `/api/v1/timeline`      | `date=YYYY-MM-DD`, `hours=N` (default 24) | Per-frame entries for a day or a recent window. One element per `entries` row, smoothed `state`, raw labels, shadow blob. |
| GET    | `/api/v1/sleep-stats`   | `days=N` (default 7)                     | Daily totals + transition counts (sleep/wake/feed/diaper). |
| GET    | `/api/v1/bassinet-daily`| `days=N` (default 7)                     | Per-day in-bassinet duration breakdown by state. |
| GET    | `/api/v1/sleep-trend`   | `days=N` (default 14)                    | Rolling sleep totals + sliding-window averages. |
| GET    | `/api/v1/feeds`         | `days=N` (default 1)                     | Feed events parsed from corrections / annotations. |
| GET    | `/api/v1/diapers`       | `days=N` (default 1)                     | Diaper events. |
| GET    | `/api/v1/events`        | `hours=N` (default 72), `count=N` (default 20), `type=all\|wake\|asleep\|...` | Recent state transitions for the Events panel. |

### Entry edits & review

| Method | Path                          | Body                                                  | Description |
|--------|-------------------------------|-------------------------------------------------------|-------------|
| POST   | `/api/v1/update-entry`        | `{timestamp, state?, position?, eyeState?, faceBbox?}` | Edit one entry's primary fields. Sets `eye_state_edited=1` so post-retrain backfills skip it. `faceBbox` may be an object, `null`, or absent (omit to leave unchanged). |
| POST   | `/api/v1/mark-reviewed`       | `{timestamps: [string, ...]}`                          | Bulk mark a block of entries as ground-truth-reviewed. |
| POST   | `/api/v1/run-inference`       | `{timestamp}`                                          | Re-runs BIRDEYE on the frame for that entry. Forwards to capture's `/infer` over `BILBO_CAPTURE_URL`. Returns `{ok, shadow, faceBbox?, faceConfidence?, retrainAgreed?}` or `{ok:false, error}` (with 502 if capture is unreachable). |

### Training

| Method | Path                        | Body                                       | Description |
|--------|-----------------------------|--------------------------------------------|-------------|
| GET    | `/api/v1/training-status`   | —                                          | Whether a training run is in progress (queries Docker for a container named `bilbo-training`), plus the latest run's metrics. |
| POST   | `/api/v1/retrain`           | `{trigger?, skipFaceDetect?}` (both optional) | Spawn the `bilbo-training` container via the Docker socket. `trigger` is a free-text tag stored on the run. |
| POST   | `/api/v1/retrain/abort`     | —                                          | Kill the in-flight `bilbo-training` container if one is running. |

### Corrections inbox

| Method | Path                              | Body                  | Description |
|--------|-----------------------------------|-----------------------|-------------|
| GET    | `/api/v1/pending-corrections`     | —                     | Open correction rows awaiting label, with timestamps, current eye state, and the BIRDEYE prediction. |
| POST   | `/api/v1/correction/resolve`      | `{id, eyeState}`      | Apply a label to a pending correction. |
| POST   | `/api/v1/correction/discard`      | `{id}`                | Drop the label-correction row. Any bbox correction for the same frame is preserved. |

### System health (the "System" tab)

| Method | Path                          | Query                                | Description |
|--------|-------------------------------|--------------------------------------|-------------|
| GET    | `/api/v1/system-usage`        | —                                    | Load avg, memory (Linux `/proc/meminfo` in the container; macOS `vm_stat` on host dev), disk, baby-monitor data-dir sizes, and a per-container view of the bilbo stack (CPU%/mem%/RSS/uptime) via the Docker SDK. ~1-2 s response time because Docker stats samples for ~1 s per container; dashboard polls every 10 s. |
| GET    | `/api/v1/pipeline-health`     | —                                    | Capture freshness, 24 h capture coverage vs nominal, 24 h gap list, ongoing gap, detection-method mix, cloud-call attempts/failures (with quota-exhausted subset), watchdog outage state. |
| GET    | `/api/v1/classification-rate` | `hours=N` (default 24), `bucketMin=N` (default 60) | Stacked outcomes per time bucket: cloud, birdeye-confident, birdeye-fallback. |

### Model performance & safety

| Method | Path                                | Query                  | Description |
|--------|-------------------------------------|------------------------|-------------|
| GET    | `/api/v1/safety-stats`              | `hours=N` (default 168) | Aggregate alert/safety counts over the window. |
| GET    | `/api/v1/monitor-stats`             | `hours=N` (default 24) | Live BIRDEYE-vs-cloud agreement, per-class precision/recall, bbox-impact deltas. |
| GET    | `/api/v1/eye-state-daily-metrics`   | `days=N` (default 14)  | Eye-state classifier daily F1 / precision / recall. |
| GET    | `/api/v1/pipeline-history`          | `days=N` (default 14)  | Daily rollup of detection methods + cloud fallback rate. |

### Air quality

| Method | Path                  | Query                  | Description |
|--------|-----------------------|------------------------|-------------|
| GET    | `/api/v1/air-quality` | `hours=N` (default 24) | Polls `AIRGRADIENT_DB_PATH` (default `/app/airgradient-logger/data/airgradient.db`) read-only and aggregates CO₂, PM2.5, TVOC index, temp, humidity into time buckets. Returns `{points, latest, statuses, alerts, transitions, badZones, health, insights, recommendations, score, note}`. If the DB is missing, `note` describes why and `points` is empty. |

### Frames & recap

| Method | Path                       | Query / Body            | Description |
|--------|----------------------------|-------------------------|-------------|
| GET    | `/api/v1/frame`            | `path=<abs-or-legacy>`  | Streams a JPEG. Path is resolved under `FRAMES_DIR`; pre-refactor host paths (`/Users/.../baby-monitor/data/frames/...`) fall back to `FRAMES_DIR/basename(path)`. `400` if `path` empty, `404` if the file isn't under `FRAMES_DIR` and the basename isn't there either. |
| POST   | `/api/v1/recap/generate`   | `{date?, fps?, force?}` | Stitches a day's frames into an MP4 via `ffmpeg`. Idempotent unless `force=true`. Returns metadata + on-disk name. |
| GET    | `/api/v1/recap/video`      | `name=<filename>`       | Streams the recap MP4. `send_file` with `conditional=True` so the `<video>` element's Range requests work. |

## Response schemas

Field type syntax: `string`, `int`, `float`, `bool`, `array<X>`, `object`. A
trailing `?` marks an optional or nullable field. Nested objects are shown
indented. Where a route has a non-200 error branch, that branch is shown
below the 200 shape.

### `GET /healthz`
```
ok: bool
```

### `GET /api/v1/status`
```
display:              string             # human label, e.g. "Asleep"
icon:                 string             # absent | asleep | awake | unknown
duration:             string             # "Nh MMm" or "MMm"
durationSeconds:      int
timestamp:            string             # ISO 8601 UTC of last entry
frame:                string             # frame path
position:             string?
alerts:               array<object>
captureMode:          string?
secondsSinceCapture:  int?
```
404 when the DB is empty: `{ error: string }`.

### `GET /api/v1/timeline`
```
entries: array<{
  timestamp:                string
  babyPresent:              bool
  state:                    string           # smoothed: Asleep | Awake | FallingAsleep | Unknown | not_present
  eyeState:                 string?          # eyes_open | eyes_closed | not_scoreable | not_present
  eyeStateEdited:           bool             # true if user-corrected
  eyeStateCorrectedAt:      string?
  detectionMethod:          string           # birdeye | pixel-diff | vision-api | openai-vision
  shadowModelVersion:       string?
  shadowBirdeyeState:       string?
  shadowEyeState:           string?
  shadowPresenceConfidence: float?
  shadowEyeConfidence:      float?
  shadowFallback:           string?          # reason BIRDEYE deferred to cloud
  headPosition:             { x: float, y: float }?
  faceBbox:                 { x: float, y: float, w: float, h: float }?
  faceConfidence:           float?
  faceBboxCorrected:        bool
  retrainAgreed:            bool?
  reviewed:                 bool
  frame:                    string
  alerts:                   array<object>
  experiments:              object?
}>
feeds: array<{ timestamp, type, condition, location, notes }>
```

### `GET /api/v1/sleep-stats`
```
days: array<{
  date:                  string             # YYYY-MM-DD (ET)
  totalHours:            float
  longestSleepHours:     float
  longestBassinetHours:  float
  stretches:             array<{ start, end, hours }>
}>
```

### `GET /api/v1/bassinet-daily`
```
days: array<{
  date:                 string
  asleepHours:          float
  awakeHours:           float
  fallingAsleepHours:   float
  unknownInHours:       float
  inHours:              float
  outHours:             float
  inPct:                float
}>
```

### `GET /api/v1/sleep-trend`
```
slotMinutes:   int                          # 15
slotsPerNight: int                          # 76
startHour:     int                          # 16 (4 PM)
endHour:       int                          # 11 (11 AM next day)
nights: array<{
  date:  string
  label: string
  cells: array<string>                      # asleep | awake | out | unknown | none
}>
p50: {                                      # 50th percentile night
  label: string
  cells: array<{
    cat:   string
    share: { asleep: float, awake: float, out: float, unknown: float }
  }>
}
p90: { ... }                                # same shape as p50
```

### `GET /api/v1/feeds`
```
feeds: array<{ start, end, duration, condition, location, endCondition, notes }>
count: int
```

### `GET /api/v1/diapers`
```
diapers: array<{ start, color, consistency, contents }>
count: int
```

### `GET /api/v1/events`
```
events: array<{
  timestamp: string
  type:      string                         # wake | asleep | feed | diaper | ...
  duration?: string
}>
```

### `POST /api/v1/update-entry`
```
ok: bool
```
On failure: `{ ok: false, error: string }` with status 400 (bad input) or 404 (timestamp not found).

### `POST /api/v1/mark-reviewed`
```
ok:      bool
updated: int
```
On failure: `{ error: string }` with status 400.

### `POST /api/v1/run-inference`
Success (200, body passed through from capture's `/infer`):
```
ok:                  bool
shadow: {
  birdeyeState:        string
  eyeState:            string?
  presenceConfidence:  float
  eyeConfidence:       float?
  fallback:            string?
  birdeyeTimings:      { load, presence, total, ... }
}
faceBbox:            object?
faceConfidence:      float?
retrainAgreed:       bool?
```
Errors: 400 (missing timestamp), 502 (capture unreachable), 504 (timeout). All return `{ ok: false, error: string }`.

### `GET /api/v1/training-status`
```
running:                  bool
runStatus:                string             # running | idle | success | failed | aborted
pid:                      int?
containerId:              string?
trigger:                  string?
startedAt:                string?
finishedAt:               string?
exitCode:                 int?
lastTrained:              string?
version:                  string?            # deployed model version
lastMetrics:              object?            # per-classifier metric snapshot
lastLabelSources:         object?
lastEntriesTotal:         int?
lastDurationSeconds:      int?
prevVersion:              string?
prevMetrics:              object?
pendingCorrections:       int
totalCorrections:         int
trainingDurationStats:    { count, avg_seconds, p99_seconds }?
lastTrainedPerClassifier: object?
```

### `POST /api/v1/retrain`
```
ok:          bool
containerId: string                         # short Docker ID
```
On failure: `{ ok: false, error: string }` with status 409 (already running).

### `POST /api/v1/retrain/abort`
```
ok:     bool
status: string                              # killed | finishing | ...
```
On failure: `{ error: string }` with status 404 (nothing running).

### `GET /api/v1/pending-corrections`
```
corrections: array<{
  id:                  int?
  correctedAt:         string
  originalTimestamp:   string
  frame:               string?
  originalState:       string?
  correctedState:      string?
  originalEyeState:    string?
  correctedEyeState:   string?
  originalPosition:    string?
  correctedPosition:   string?
  detectionMethod:     string?
  source:              string                # ui | bbox-tool | ...
}>
count:            int
lastTrained:      string?
eyeStateChanges:  object                    # { "eyes_open → eyes_closed": int, ... }
```

### `POST /api/v1/correction/resolve`
```
ok:        bool
id:        int
eyeState:  string
```
On failure: `{ ok: false, error: string }` with status 400 / 404.

### `POST /api/v1/correction/discard`
```
ok: bool
id: int
```
On failure: `{ ok: false, error: string }` with status 400 / 404.

### `GET /api/v1/system-usage`
```
asOf: string
load: {
  oneMin:      float
  fiveMin:     float
  fifteenMin:  float
  cores:       int
  ratio:       float                        # oneMin / cores
  trend:       float                        # oneMin - fiveMin
}
memory: {
  totalBytes:       int
  freeBytes:        int
  availableBytes:   int?
  usedPct:          float?
  cachedPct:        float?
  freePct:          float?
  cachedBytes:      int?                    # Linux only
  activeBytes:      int?                    # macOS only
  inactiveBytes:    int?                    # macOS only
  wiredBytes:       int?                    # macOS only
  compressedBytes:  int?                    # macOS only
}
disk: array<{ label, path, totalBytes, freeBytes, usedPct }>
babyMonitor: {
  sizes: { dataDirBytes, framesDirBytes, modelsDirBytes, monitorDbBytes }
  processes: array<ProcessRow>
}
topProcesses: array<ProcessRow>             # top 8 by CPU
topByMemory:  array<ProcessRow>             # top 8 by RSS

ProcessRow = {
  pid:      string                          # container short id post-cutover
  cpuPct:   float
  memPct:   float
  rssKb:    int
  etime:    string                          # ps-style, e.g. "01:23" or "1-04:05:06"
  command:  string                          # container image
  script:   string                          # container name
}
```

### `GET /api/v1/pipeline-health`
```
asOf: string
lastEntry: {
  timestamp:   string
  ageSeconds:  int
  freshness:   string                       # fresh | stale | down
}?
captures24h: {
  actual:       int
  nominal:      int
  intervalSec:  int
}
gaps24h: {
  thresholdMin:    int
  count:           int
  totalMissedMin:  float
  items:           array<{ start, end, minutes }>   # up to 20, newest first
  ongoing:         { start, minutes }?
}
detectionMethods24h: array<{ method, count, pct }>
cloudCalls24h: {
  attempted:        int
  succeeded:        int
  failed:           int
  quotaExhausted:   int
  lastFailure:      { timestamp, reason }?
}
watchdog: {
  outageStartedAt:  string?
  outageActive:     bool
  lastAlertAt:      string?
  lastAlertKind:    string?
}?
```

### `GET /api/v1/classification-rate`
```
asOf:               string
hours:              int
bucketMin:          int
nominalPerBucket:   int
buckets: array<{
  start:           string
  endExclusive:    string
  birdeye:         int
  "pixel-diff":    int
  "cloud-success": int
  "cloud-failed":  int
  other:           int
  total:           int
}>
```

### `GET /api/v1/safety-stats`
```
hours:                  float
shadowTotal:            int
deployedVersion:        string?
latestTrainedVersion:   string?
rolledBack:             bool
groundTruth: { total, reviewed, corrected }
presence:  { birdeyeVsGT: Panel?, cloudVsGT: Panel? }
eyeState:  { birdeyeVsGT: Panel?, cloudVsGT: Panel? }
faceDetection: object
experiments:   object                       # keyed by experiment name

Panel = {
  confusion: object                         # { trueClass: { predClass: count } }
  perClass:  object                         # { class: { precision, recall, f1, support } }
  macroF1:   float
  accuracy:  float
  total:     int
}
```

### `GET /api/v1/monitor-stats`
```
hours:        float
total:        int
methods: { birdeye: int, cloud_api: int, pixel_diff: int }
birdeyeRate:  float
confidence: {
  presence: { avg, min, max, p50, p99 }?
  eye:      { avg, min, max, p50, p99 }?
}
timing:        { avg, min, max, p50, p99 }?
cloudModels:   object                       # { modelName: count }
cost:          { apiCalls, apiAvoided, estCost, estSaved }
gaps:          int                          # gaps > 10 min in window
birdeyeVersions: object                     # { version: count }
shadow: {
  total:          int
  agreed:         int
  disagreed:      int
  agreementRate:  float?
}
```

### `GET /api/v1/eye-state-daily-metrics`
```
days: int
rows: array<{
  date:        string                       # YYYY-MM-DD (ET)
  total:       int
  eyes_open:   { precision, recall, f1, support } | { precision: null, recall: null, f1: null, support: 0 }
  eyes_closed: { precision, recall, f1, support } | { precision: null, recall: null, f1: null, support: 0 }
}>
```

### `GET /api/v1/pipeline-history`
```
days: int
rows: array<{
  date:       string                        # YYYY-MM-DD (ET)
  pixelDiff:  { count, pct }
  birdeye:    { count, pct }
  cloudApi:   { count, pct }
  captures:   int
  cost:       float                         # estimated USD
  versions:   array<{ version, count, pct }>  # BIRDEYE versions, desc by count
}>
```

### `GET /api/v1/air-quality`
```
hours:    int
points:   array<{ t, co2, pm25, temp, rh, tvoc_index }>   # ≤ 360 buckets
latest:   { t, co2?, pm25?, temp?, rh?, tvoc_index? }?
statuses: {
  co2:      { value, level, headline, detail, unit }?
  pm25:     ...?
  temp:     ...?
  humidity: ...?
  tvoc:     ...?
}?
score: {
  score:    float                           # 0-100
  label:    string
  drivers:  array<{ metric, label, sub, weight }>
}?
alerts:           array<{ severity, metric, value, unit, t, action }>
insights:         array<{ id, title, body }>
recommendations:  array<{ id, icon, title, body }>
badZones: {
  co2:        array<{ tStart, tEnd }>
  pm25:       array<{ tStart, tEnd }>
  temp:       array<{ tStart, tEnd }>
  rh:         array<{ tStart, tEnd }>
  tvoc_index: array<{ tStart, tEnd }>
}
health: {
  lastReading:       string?
  secondsSinceLast:  int?
  samples:           int
  expected:          int?
  missingPct:        float?
  verdict:           string                 # ok | no_data | stale | gappy
}
transitions: array<object>?
note:        string?                        # set when DB is missing or window empty
```

### `GET /api/v1/frame`
JPEG binary. `400` on empty `path`, `404` on missing file (after `FRAMES_DIR/basename` fallback), `403` reserved (currently unreachable post-fallback fix).

### `POST /api/v1/recap/generate`
```
status:        string                       # "ready" | "empty"
cached:        bool?
date:          string
fps:           int
frame_count:   int
duration_sec:  float?
size_bytes:    int?
video_url:     string?
```
On failure: `{ error: string }` with status 400 (bad fps/date), 404 (no frames), 500/504 (ffmpeg).

### `GET /api/v1/recap/video`
MP4 binary. `send_file(..., conditional=True)` so HTTP Range requests work for `<video>` seeking. `404` if `name` doesn't resolve to a generated recap.

## Auth

The control-api itself trusts whatever reaches it on `:5556`. In
production it's not exposed publicly — Cloudflare Access fronts the
dashboard (port 5555), and the dashboard's reverse proxy is the only
client. The PWA service worker injects `CF-Access-Client-Id` /
`CF-Access-Client-Secret` headers from `.env` so the installed app's
background fetches survive SSO cookie expiry.

## How it reaches the other containers

- **SQLite (`data/monitor.db`)** — bind-mounted into control-api at
  `/app/data`; all read paths go through `bilbo.storage.db.get_connection()`,
  a thread-local sqlite connection in WAL mode.
- **AirGradient DB** — bind-mounted from `airgradient-logger/data/` at
  `/app/airgradient-logger/data:ro`. Override the lookup with the
  `AIRGRADIENT_DB_PATH` env var.
- **Capture's `/infer`** — control-api dials `BILBO_CAPTURE_URL`
  (default `http://capture:5557`, overridden via `env_file` to the
  host-gateway IP that reaches capture's host-network listener; see
  `.env.example` for per-OS values). Used by `/api/v1/run-inference`.
- **Training container** — control-api has `/var/run/docker.sock`
  mounted and uses the `docker` SDK to spawn `bilbo:latest` with
  `bilbo-train` as the command, bind-mounting `${BILBO_HOST_DATA}` and
  `${BILBO_HOST_MODELS}` (so the in-container daemon resolves the same
  host paths).
- **Frame archive** — bind-mounted at `/app/data/frames/`. The recap
  generator and `/api/v1/frame` both serve out of here.

## Adding a route

1. Implement the function under `bilbo/api/<module>.py`, with no Flask
   dependency. Return a dict; raise normal Python exceptions for
   programmer errors and let `pipeline_health`-style cases return the
   error data as a structured field (`note`, `error`, etc.).
2. Wire it in `bilbo/http/app.py` with the matching `@app.get` /
   `@app.post` decorator under `/api/v1/...`. Use `_respond(...)` for
   JSON responses; `send_file(...)` for binary.
3. Update the relevant table above.
4. The dashboard's reverse proxy auto-forwards `/api/<rest>` to
   `/api/v1/<rest>` — no proxy changes required for new routes.
