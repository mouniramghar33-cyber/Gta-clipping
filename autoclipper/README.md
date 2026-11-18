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

### 8. Publishing (optional, official APIs only)
- **YouTube Shorts**: YouTube Data API v3 `videos.insert`, resumable upload.
- **TikTok**: Content Posting API, Direct Post with `FILE_UPLOAD`. Until TikTok audits your app,
  posts are private only.
- **Instagram Reels**: Instagram Graph API, `media` (REELS) → status check → `media_publish`.
  Instagram downloads the video from a private, unguessable URL on this server, which only works
  for approved clips.

Default mode is **manual**: you approve a clip and press Publish, after confirming you have the
rights. In **auto** mode only clips above `auto_min_score` are published, and only for streamers
marked "publishing rights granted". Only publish content you have permission to use.

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
  autoclipper.db     SQLite: streamers, sessions, chat history, moments, clips, publications
  segments/          rolling recording buffer
  clips/YYYY-MM-DD/  finished clips (.mp4 + .jpg)
  models/            Whisper models
  logs/              rotating logs
  secrets/           rotating OAuth tokens (0600)
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
| `publishing.mode` | manual | `auto` publishes only for streamers with rights granted |

## Credentials
All secrets go in `.env` only. They are never written to config, the database or the dashboard.

- **Twitch (optional)**: create an app at dev.twitch.tv and set `TWITCH_CLIENT_ID` / `_SECRET`.
- **YouTube**: in Google Cloud, enable YouTube Data API v3 and create an OAuth client. Get a
  refresh token with scope `https://www.googleapis.com/auth/youtube.upload` (for example with the
  OAuth 2.0 Playground using your own client), then set `YT_CLIENT_ID`, `YT_CLIENT_SECRET` and
  `YT_REFRESH_TOKEN`. Uploads use API quota; check your project's quota in the console.
- **TikTok**: register an app with the Content Posting API (`video.publish`), authorize your
  account and set `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` and `TIKTOK_REFRESH_TOKEN`.
  Unaudited apps can only post privately.
- **Instagram**: a professional (Business/Creator) account with an app that has
  `instagram_business_content_publish`. Set `IG_USER_ID`, `IG_ACCESS_TOKEN` and `PUBLIC_BASE_URL`.

## Testing
```bash
pip install pytest && python -m pytest tests      # detector, cutting, subtitles, scoring, config
python tests/e2e_live.py <live_twitch_channel> 600 # real end-to-end run against a live stream
```

## Limits (honest)
- Chat-based detection works for **Twitch**. Kick and YouTube channels can be recorded, but have
  no automatic chat detection.
- Whisper runs **per clip**, not continuously on every stream: that keeps CPU usage manageable.
  A transcript therefore improves the cut and the score, but doesn't trigger detection.
- Face detection uses OpenCV's Haar cascade. If you put the YuNet model
  (`face_detection_yunet_2023mar.onnx`) at `/opt/models` or set `YUNET_MODEL`, the stronger
  detector is used instead.
- "Viral" is a prediction from measured signals, not a guarantee. Use the approval queue.
- Respect creators: credit them (done automatically), honour takedown requests and only publish
  with permission.
