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

## The watchlist (exclusive)

Exactly these 11 streamers are monitored (`autoclipper/watchlist.yaml`, verified 18 Nov 2026).
Nothing else is ever added automatically; streamers you add yourself in the dashboard are the
only exception.

| Streamer | Channels, tried in order (first live one is recorded) | How verified |
|---|---|---|
| Marlon | Twitch `marlon` | Twitch channel page |
| Lacy | Twitch `lacy` → Kick `lacy` | Kick: verified, 103k followers |
| Stable Ronaldo | Twitch `stableronaldo` → Kick `stableronaldo` | Kick: verified |
| IShowSpeed | YouTube `UCWsDFcIhY2DBi3GB5uykGXA` (@IShowSpeed) → Twitch `ishowspeed` | YouTube canonical URL |
| Kai Cenat | Twitch `kaicenat` | Twitch channel page |
| Jynxzi | Twitch `jynxzi` | Twitch channel page |
| JasonTheWeen | Twitch `jasontheween` | Twitch channel page |
| Adin Ross | Kick `adinross` | Kick: verified, 3.2M followers |
| Rakai | Twitch `2xrakai` (`twitch.tv/rakai` is a different person) | Twitch channel page |
| Tota | Twitch `totaamc` | Twitch channel page |
| Typical Gamer | YouTube `UC2wKfjlioOCLP4xQMOWNcgg` (@TypicalGamer) → Twitch `typicalgamer` | YouTube canonical URL |

Kick accounts that only share a name (kaicenat, jynxzi, marlon, tota, typicalgamer, ishowspeed,
jasontheween: unverified, few followers) are name squatters and are deliberately not monitored.

How the watchlist is monitored:
- **One recording per streamer:** a multistream is recorded once, from the first channel in
  the list that is live.
- **Platform switches:** when the recorded channel goes offline, the next check looks at the
  other channels.
- **Collaborations:** detected when another watchlist streamer is named in the title, in speech
  or repeatedly in chat. The same moment seen from two streams is clipped only once.

**Chat per platform**

| Platform | Chat source | Needs |
|---|---|---|
| Twitch | official IRC over WebSocket (anonymous, read-only) | nothing |
| Kick | official Kick Public API: `chat.message.sent` events delivered to `/webhooks/kick`, RSA-signature verified | free Kick developer app (`KICK_CLIENT_ID`/`SECRET`) + HTTPS |
| YouTube | YouTube Data API `liveChatMessages.list`, within a daily quota budget | `YT_API_KEY` |

Without a chat source, a stream is still recorded and clipped. Moments then need at least two of
three strong signals: a facial expression change, body movement and an audio spike. A visible
reaction is always required, and loudness alone never triggers a clip.

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

### 2b. Real-time face, expression and body language
On every 2-second segment, AutoClipper decodes only the keyframe, which is about 10× cheaper
than decoding every frame of a 1080p60 stream. YuNet finds the face, FER+ (ONNX Model Zoo, MIT)
reads the expression (happiness, surprise, anger, fear, sadness) and frame differencing around
the face measures body movement. These signals reinforce chat spikes and drive no-chat
detection. Measured CPU use here: about 0.25 core per live stream including audio. Four
simultaneous live streams ran at a load of 0.8 on 4 cores.

### 2c. Context understanding
Each candidate clip is classified from the transcript, chat, facial expressions, audio,
category and title. The moment types are: funny, rage, argument, reaction, collab, emotional,
controversial, donation, embarrassing, fail, impressive, discovery and meme. Every label lists
its evidence (for example `chat: 41% laugh`, `streamer laughing (72%)`, `said: "no way"`).
Ordinary conversation, meaning only weak cues and no clear chat explosion, is rejected. An
optional local LLM (OpenAI-compatible, for example Ollama) can add a second opinion. Celebrity
appearances are only recognised when they're named in speech, chat or the title; there is no
face recognition of people.

### 2d. GTA VI priority mode
- **Detection:** GTA VI play is recognised from the stream category as reported by the
  platform, for example "Grand Theft Auto VI" on Twitch and "Grand Theft Auto VI (GTA)" on
  Kick. No streamer is assumed to play it.
- **During the release period** (19 Nov 2026 plus 45 days, configurable): detection thresholds
  drop to ×0.85, GTA moments are processed first, trend relevance is 90+, and titles and
  hashtags are GTA-specific.
- **GTA tags** come from speech and chat: first reaction, NPC, police chase, hidden location,
  easter egg, realistic, glitch, secret mission, death, graphics, online, Vice City.

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

### Clip length (strict)
- Ideal 20–40 s. Trimmed toward 45 s where possible. **Never longer than 60 s**: the cut
  planner, the render check and the publish check each enforce this.
- Shorter than 20 s is kept only if the score is ≥ 85.
- Clips are never padded with unrelated footage. Context is extended only by a real earlier
  sentence.
- Silence and waiting/loading screens (static picture, no speech, no chat reaction) are
  removed. The ending never cuts a word.
- When the last second matches the opening frame, the clip ends there so it loops naturally.

### 6. Score (0–100, a prediction, not a view guarantee)
| Component | Weight | Measured from |
|---|---|---|
| Opening hook | 20 | speech, interest and loudness in the first 3 s |
| Entertainment value | 20 | moment type and evidence, chat reaction mix, emote walls, "clip it" |
| Retention potential | 20 | active seconds, 20–40 s length, where the payoff falls |
| Emotional intensity | 10 | audio spike, facial expression, emphatic speech, shock/laugh chat |
| Chat engagement | 10 | spike vs. the streamer's own baseline |
| Trend relevance | 10 | GTA VI release period, category performance of your published clips |
| Originality | 5 | difference from recent clips, rarity of the spike |
| Streamer popularity | 5 | live viewers / followers reported by the platform, your clip results |

Tiers: **95–100 immediate**, **90–94 very high** (both published as soon as limits allow),
**85–89 high**, **80–84 normal** (next posting time), **70–79 saved for review**, **below 70
auto-rejected**. All of these are configurable under Settings → scoring.

Speed target: 60–180 s from the moment to a finished clip. AutoClipper waits only 15 s after
the moment, Whisper uses greedy decoding with names and GTA terms as a spelling prompt, and two
clip workers run in parallel. The time is measured and shown for every clip.

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
Besides views, likes, comments and shares: the engagement rate, average % viewed (YouTube; a
completion proxy), subscribers gained, and performance by streamer, style, moment type, GTA vs
other, clip length (0–20, 20–30, 30–40, 40–45, 45–60 s), tier and posting hour.
**Resource allocation**: each streamer gets a weight from real results (1.0 = average, no data =
1.0). Higher weights get their moments processed first. When the CPU is busy, the weakest
performers record at 720p60, while GTA VI streams always stay at full quality.

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
- **YouTube live** (IShowSpeed, Typical Gamer): recording uses Streamlink's YouTube plugin. Both
  channels were offline during testing, so YouTube recording hasn't been seen live from this
  server. YouTube sometimes asks datacenter IPs to "confirm you're not a bot"; if that happens,
  the streamer is still caught when they stream on Twitch (both have Twitch channels in the
  watchlist).
- **Kick chat** needs your free Kick developer app. The webhook signature check is tested with
  real RSA signatures, but no real Kick chat event has been received yet, because that requires
  your app. Until then, Kick streams use face, body language and audio.
- **Context understanding** is evidence-based (speech, chat, face, audio), not a general video AI.
  Expression recognition (FER+) is coarse. Visual game events (for example a police chase) are
  recognised from what streamers and chat say, not from the picture itself.
