# AutoClipper

An autonomous livestream clipping system. It watches several Twitch streamers at once and
records them as soon as they go live. It reads their chat in real time, detects moments where
chat explodes compared with that streamer's normal activity, and turns each moment into an
edited, subtitled 9:16 clip. That happens within minutes, while the stream is still running.

```
Twitch ──Streamlink──▶ FFmpeg segmenter ──▶ 2 s segments (rolling buffer, wall-clock indexed)
   │                                              │
   └──chat (WebSocket IRC)──▶ rolling 10 s windows ──▶ detector (vs. the streamer's baseline)
                                                      │  + audio loudness / motion per segment
                                                      ▼
                       moment ──▶ extract −30 s/+30 s ──▶ Whisper (word timestamps)
                                   ──▶ best opening/ending + dead-air removal
                                   ──▶ layout (facecam stack / face tracking / blurred fit)
                                   ──▶ word-by-word subtitles ──▶ FFmpeg render 1080×1920
                                   ──▶ score 0-100 + metadata ──▶ dashboard ──▶ (publish)
```

Everything is free and open source: Streamlink, FFmpeg, faster-whisper (Whisper on CPU),
OpenCV, SQLite, FastAPI and Docker. You don't need any paid API.

---

## How it works

### 1. Monitoring and recording
- Every `live_check_interval_sec` (60 s) each enabled streamer is checked. By default this uses
  Streamlink. With optional free Twitch app credentials it uses a single Helix call for up to 100
  streamers.
- When a streamer is live, Streamlink pipes the stream into FFmpeg's segment muxer, which writes
  2-second MPEG-TS segments. Each segment gets wall-clock start and end times so video lines up
  with chat.
- Recording restarts automatically if the pipeline dies, with backoff. Twitch ads are filtered.
  Several streamers are recorded in parallel.
- Only the last `buffer_minutes` (15) of segments are kept, plus any range a queued clip still needs.

### 2. Chat and moment detection
- One anonymous read-only WebSocket connection (`wss://irc-ws.chat.twitch.tv`) covers all
  channels. It needs no Twitch account, and reconnects automatically.
- Every 2 s the last 10 s of chat is analysed:
  - the message count compared with this streamer's **own** baseline, using the robust median
    and MAD of 10 s bins over the last 30 minutes;
  - the share of reaction messages: laugh (`LOL`, `LMAO`, `KEKW`, 💀…), shock (`WTF`, `OMG`,
    `NO WAY`…), hype (`W`, `POG`…), fail (`L`, `RIP`…), tension/arguments (`yikes`, `???`…) and
    `clip it` requests;
  - repeated messages (emote walls, copypasta);
  - the audio loudness spike from the recorded segments.
- A moment starts when chat is clearly above baseline, or moderately above baseline with mostly
  reaction messages. Audio can lower the bar a little but **never creates a moment on its own**.
- Chat reacts a few seconds late, so the moment is shifted back by `reaction_delay_sec`.
  Detections close together are merged. Echoes within the cooldown are ignored, and a moment
  already covered by a clip is skipped.

### 3. Clipping (runs during the stream)
As soon as the 30 s after a moment have been recorded:
1. The window from 30 s before to 30 s after is joined losslessly from the segments.
2. Whisper (faster-whisper, CPU int8) transcribes it with word timestamps and detects the language.
3. An interest curve (chat, audio, motion, speech) picks the **peak**. The **ending** is where the
   reaction has died down, never in the middle of a word or sentence. The **opening** is a
   sentence start or pause about 6–14 s before the peak that also starts lively.
4. Dead air is removed: stretches longer than 1.2 s with no speech, low audio and no chat or
   visual activity. The moment itself is never cut.
5. Length is usually 15–60 s (configurable). Quality comes first, within those limits.

### 4. Vertical editing
Frames are sampled to detect black borders and faces (OpenCV):
- **stack**: a streamer with a facecam overlay gets the facecam on top (38%) and the gameplay below.
- **track**: a large face (Just Chatting/IRL) gets a 9:16 crop that smoothly follows the face.
- **fit**: with no face, the full gameplay sits on a blurred background, so no gameplay is lost.

Audio is loudness-normalised to −14 LUFS. Video is H.264 1080×1920, up to 60 fps, AAC 192k,
with faststart.

### 5. Subtitles
Word-by-word ASS subtitles, burned in with libass:
- bold white text with a black outline, at most 2 lines;
- important words highlighted in yellow: strong words, numbers, words chat repeated, shouted words;
- words appear exactly when they're spoken, and the layout never jumps;
- placed inside the TikTok/Reels/Shorts safe zone and **never over the face**. In the fit layout
  they sit on the blurred area, so no gameplay is covered.

### 6. Score (0–100)
| Component | Default weight | Measured from |
|---|---|---|
| Chat engagement | 25 | spike vs. baseline (ratio, z-score) |
| Emotional intensity | 15 | audio spike, emphatic speech, shock/laugh chat |
| Entertainment value | 20 | reaction mix, emote walls, "clip it" requests, laughter |
| Opening hook | 15 | speech, interest and loudness in the first 3 s |
| Retention potential | 15 | share of active seconds, length, where the payoff falls |
| Uniqueness | 10 | difference from the streamer's recent clips, rarity of the spike |

Clips scoring ≥ `priority_threshold` (80) are marked **priority** and sorted first. Clips below
`min_score_to_keep` are auto-rejected. All weights and thresholds can be changed in the dashboard.

### 7. Metadata
Every clip gets a title, a hook (also burned in for the first 2.5 s), a description with credit,
hashtags, a thumbnail (a frame at the peak) with a suggestion, the score with **the evidence
behind it**, and the source timestamps (UTC, plus the stream offset when Helix is configured).
Titles use a **literal quote** from the transcript or the measured chat reaction, so nothing is
made up. Optionally an OpenAI-compatible endpoint (for example a local Ollama model) can rewrite
them, using the same facts.

### 8. Editing styles (per account)
Every clip keeps a **master**: the lossless source window plus its edit decisions (cut points,
peak, layout, words, highlights, hook). Any style can then be rendered later without
re-analysing, so each social account can have its own look:

| Style | Look |
|---|---|
| `clean` (preview default) | hook text, pop-in word captions, facecam/gameplay split |
| `energetic` | larger UPPERCASE captions, punch-in zoom on the peak, 6% zoom on every jump cut, a generated "whoosh" on strong moments only (score ≥ 60 and laugh/shock/hype/fail chat) |
| `minimal` | no hook, smaller captions, no effects |
| `auto` (account setting) | learns which style performs best per account from real metrics |

Other editing steps: scene-change detection (cuts land on hard scene changes near the opening or
ending), 15 ms audio fades at every cut, YuNet face detection (with Haar as fallback), facecam
border detection, −14 LUFS loudness, H.264 1080×1920 with no watermark. Styles are edited under
Settings → styles; the "Preview style" button on a clip renders one.

### 9. Publishing (official/authorized APIs only)

```
clip ─▶ quality check ─▶ rights + account rules ─▶ score (+ learned streamer bonus) ─▶ duplicate check
     ─▶ platform metadata ─▶ next free slot ─▶ [TikTok approval] ─▶ worker uploads ─▶ verify ─▶ URL
```

| Route | How | Unattended? | Cost |
|---|---|---|---|
| **YouTube Shorts** | YouTube Data API v3, OAuth "Connect YouTube", resumable upload, processing check | yes | free (100 uploads/day per API project by default) |
| **TikTok via Buffer** | Buffer public API; Buffer is an authorized TikTok partner | yes | **free plan** (3 channels, API included) |
| **TikTok official** | Content Posting API Direct Post, OAuth "Connect TikTok" | **one tap per post** (TikTok rule) | free; public posts need TikTok's app audit |
| TikTok/YouTube/IG via Upload-Post | Upload-Post API | yes | TikTok needs a paid plan (from $16/mo yearly, $24 monthly) |
| Instagram Reels | Instagram Graph API (token added in the dashboard) or via Buffer | yes | free |

**Publishing rules** (global under Settings → publishing, and per account in Accounts):
minimum score (80), maximum uploads per day, minimum minutes between uploads, posting times
(`["12:30", ...]`, `immediate`, or `auto` = learned best hours), editing style, which streamers,
YouTube visibility, AI-content disclosure, auto-publish on/off, dry run on/off.

**Safety**: global **dry run is ON by default**: the worker renders, checks quality and checks
each login, but uploads nothing. The red **■ STOP** button in the header halts all uploads
instantly. Auto-publishing only uses streamers marked "publishing rights granted". Manual
sending requires you to confirm you have permission.

**Reliability**: the queue is stored in SQLite. Transient errors (network, 5xx, 429, quota) retry
with exponential backoff (2, 4, 8 … minutes, max 6 h, `max_attempts`). Auth errors pause the
account and notify you. Permanent rejections fail at once. Jobs interrupted by a crash resume
from their saved state: the YouTube resumable session, TikTok `publish_id`, Buffer post id or
Upload-Post request id. They are never uploaded twice.

**TikTok Direct Post compliance**: TikTok's Content Sharing Guidelines require, for every post:
fresh creator info with the nickname shown, privacy chosen manually with no default,
comment/duet/stitch off by default, the commercial-content disclosure, the consent declaration,
a preview, and explicit consent before upload. AutoClipper implements exactly that screen
(Queue → Review) and validates it again on the server. Unaudited apps can post only `SELF_ONLY`,
for at most 5 users per 24 h. TikTok also caps posting at about 15 posts per creator per day.
For hands-off TikTok posting use the Buffer route.

### 10. Analytics and learning
The worker fetches, every 3 h: YouTube views, likes and comments (Data API), plus watch time,
average % viewed and subscribers gained (YouTube Analytics API); TikTok views, likes, comments
and shares (`video.list` scope); Buffer post metrics; Instagram likes and comments. **Missing
metrics stay empty ("n/a") and are never estimated.** Each post is compared with its own
account's median views. Once a group has `min_samples` posts, the results adjust the score of
the streamer's clips (±10), pick the best style for `auto` accounts (with 20% exploration) and
pick the best posting hours for `auto` posting times.

### 11. Notifications (Telegram, optional)
Successful uploads, failures, viral clips (score ≥ 85), accounts needing reconnection or
expiring, TikTok posts waiting for approval, and a daily summary. Each type can be toggled
under Settings → notifications.

### 12. Architecture

```
docker compose
├── app     (python -m autoclipper)         monitoring · recording · chat · detection · editing · dashboard
└── worker  (python -m autoclipper.worker)  publish queue · uploads · token refresh · analytics · Telegram
        └── shared ./data volume: SQLite (WAL) · config.yaml · clips/masters · secrets/master.key
```
Both services restart automatically (`restart: unless-stopped`, Docker healthchecks; the worker
writes a heartbeat). They run entirely on the server: no phone, PC or Claude session is
involved at runtime. Redis/Celery were considered: at this scale (a few uploads per hour) a
SQLite-backed queue with leases gives the same persistence, retry and recovery guarantees
without another service to operate.

### 13. CapCut and other tools (researched)
- **CapCut is not used.** [VectCutAPI](https://github.com/sun-guannan/VectCutAPI) (Apache-2.0,
  active) generates CapCut/JianYing **draft projects**; its cloud rendering is not open source,
  and export needs the CapCut desktop app (Windows/macOS). [capcut-claude-skill](https://github.com/Altergom/capcut-claude-skill)
  (MIT, 2 commits) edits CapCut **cloud drafts** through CapCut's MCP. Export only happens in
  CapCut's web editor, it needs the Claude desktop app (Windows/macOS), and it downloads
  unpinned third-party code at runtime. Neither can export a finished video unattended from a
  Linux server, so AutoClipper's own FFmpeg editor does everything, and there is no "Connect
  CapCut" button.
- **MediaPipe**: version 1.x needs `opencv-contrib-python` (it conflicts with the headless
  OpenCV used here), plus matplotlib and sounddevice, and ships without a face model. OpenCV's
  built-in YuNet detector provides the same capability with a 230 KB model.
- **Remotion**: not needed. ASS subtitles and FFmpeg filters cover captions, zooms and effects
  without a headless Chrome render farm. Remotion also requires a paid company licence for
  larger teams.

---

## Installation (cloud server)

Requirements: a Linux VPS with Docker, at least 4 vCPU and 8 GB RAM for 3–5 streamers (Whisper and
rendering are CPU-heavy), and around 50 GB of disk.

```bash
git clone <this repo> && cd <repo>/autoclipper
cp .env.example .env && chmod 600 .env     # set DASHBOARD_PASSWORD (required)
mkdir -p data && cp config.example.yaml data/config.yaml   # optional: initial streamers
docker compose up -d --build
docker compose logs -f
```

Open `http://SERVER_IP:8080` and log in with `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.
For anything public, put it behind HTTPS (for example Caddy: `caddy reverse-proxy --from
clips.example.com --to localhost:8080`). Instagram publishing needs that HTTPS address in
`PUBLIC_BASE_URL`.

The first clip downloads the Whisper model (about 480 MB for `small`) into `data/models`.

The container runs as a non-root user with `restart: unless-stopped`, so it comes back after
crashes and reboots. Half-finished clip jobs are queued again on start, and all state lives in
`./data`:

```
data/
  config.yaml        persistent settings (also edited from the dashboard)
  autoclipper.db     SQLite: streamers, chat, moments, clips, accounts (encrypted tokens),
                     publish queue, metrics, encrypted wizard credentials
  segments/          rolling recording buffer
  clips/YYYY-MM-DD/  finished clips + style variants (.mp4 + .jpg)
  clips/masters/     per-clip source + edit decisions (kept while needed, max 3 days)
  models/            Whisper models
  logs/              autoclipper.log (app) + worker.log
  secrets/master.key encryption key (0600) unless AUTOCLIPPER_SECRET_KEY is set
```

### Run without Docker
```bash
sudo apt install ffmpeg fonts-dejavu-core fonts-montserrat
pip install -r requirements.txt
DATA_DIR=./data DASHBOARD_PASSWORD=... python -m autoclipper
```

## Configuration
All settings are in `autoclipper/config.py` (`DEFAULTS`) and can be overridden in
`data/config.yaml` or on the dashboard's Settings tab. The important ones:

| Setting | Default | Meaning |
|---|---|---|
| `detection.z_threshold` / `ratio_threshold` | 3.0 / 2.2 | how far above normal chat must go |
| `detection.hype_share_threshold` | 0.35 | share of reaction messages for the lower bar |
| `chat.reaction_delay_sec` | 5 | how late chat reacts to the stream |
| `clip.pre_sec` / `post_sec` | 30 / 30 | context captured around a moment |
| `clip.min_sec` / `max_sec` | 15 / 60 | final clip length range |
| `scoring.weights.*` | see table | score weights (any positive numbers) |
| `scoring.priority_threshold` | 80 | priority marker |
| `whisper.model` | small | `base` is faster, `medium` more accurate |
| `render.fps` / `crf` / `preset` | 60 / 20 / veryfast | output quality vs. CPU |
| `recording.buffer_minutes` | 15 | rolling buffer on disk |
| `storage.max_storage_gb` | 50 | weakest undecided clips are removed above this |
| `publishing.dry_run` | **true** | nothing is uploaded until you switch it off |
| `publishing.min_score` | 80 | default minimum score for auto-publishing |
| `publishing.daily_limit` / `min_interval_min` | 3 / 120 | defaults for new accounts |
| `publishing.posting_times` / `timezone` | 12:30 16:30 19:30 22:00 / Europe/Amsterdam | default slots |
| `styles.*` | clean, energetic, minimal | editing presets |
| `notifications.*` | all on | Telegram event toggles, viral score, daily summary hour |
| `analytics.*` | every 3 h, learning on | metrics collection and learning |

## Credentials and setup
Use the dashboard's **Setup** tab (works on iPhone/iPad). It shows each step with its status and
the exact redirect URIs to register. Credentials entered there are stored **encrypted**
(Fernet, key in `data/secrets/master.key` or `AUTOCLIPPER_SECRET_KEY`); values in `.env`
always take precedence. Tokens are never shown in the dashboard and never written in plain text.

What only you can do (accounts, approvals, payments):
1. Point a domain at the server, enable HTTPS (for example Caddy) and set `PUBLIC_BASE_URL`.
2. **YouTube**: a Google Cloud project with YouTube Data API v3 (+ YouTube Analytics API) and an
   OAuth client of type *Web application*. **Publish the OAuth consent screen**: in "Testing"
   mode Google expires the login after 7 days. Then press *Connect YouTube* (once per channel).
3. **TikTok, unattended**: a free Buffer account with your TikTok Business account connected,
   plus a Buffer API key, then *Import Buffer channels*.
4. **TikTok, official API** (optional): a TikTok developer app with Login Kit and the Content
   Posting API. Public posting requires passing TikTok's audit.
5. Optional: Upload-Post (paid), Instagram token, Telegram bot, Twitch app credentials.
6. Run the **dry-run test**, check the Queue, then switch off dry run.

## Testing
```bash
pip install pytest graphql-core
python -m pytest tests                             # 35 tests: detection, cutting, subtitles, scoring,
                                                   # encryption, queue/retries/recovery, TikTok rules, analytics
python tests/live_api_checks.py                    # real YouTube/TikTok/Buffer/Upload-Post/Telegram endpoints with
                                                   # invalid credentials + Buffer queries validated against its live schema
python tests/e2e_live.py <live_twitch_channel> 600 --sensitive   # full run against a live stream
```

## Limits (honest)
- Chat-based detection works for **Twitch**. Kick and YouTube channels can be recorded, but have
  no automatic chat detection.
- Whisper runs **per clip**, not continuously on every stream: that keeps CPU usage manageable.
  A transcript therefore improves the cut and the score, but doesn't trigger detection.
- Publishing to real accounts can only be verified once you connect them. Request formats are
  built from the current official docs. Buffer's are validated against its live GraphQL schema,
  and the auth and error handling is checked against the real endpoints, but no real post has
  been made from this test environment.
- YouTube: 100 uploads/day per API project by default. Shorts must be ≤ 3 min and vertical.
- TikTok official API: one approval tap per post. Private only until the app passes TikTok's audit.
- Buffer + TikTok: Buffer's help center says a TikTok Business account is needed for automatic
  publishing. Trending sounds can't be added through APIs.
- "Viral" is a prediction from measured signals, not a guarantee. Use the approval queue.
- Respect creators: credit them (done automatically), honour takedown requests and only publish
  with permission.
