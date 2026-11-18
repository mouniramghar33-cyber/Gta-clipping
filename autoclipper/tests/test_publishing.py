"""Tests for the publishing engine, encryption, providers' request building and analytics.

Provider HTTP calls are exercised against the real platform endpoints in
tests/live_api_checks.py. Here a small in-test provider stands in for a platform so the
queue's state machine (retries, recovery, limits, duplicates) can be tested deterministically.
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoclipper import analytics  # noqa: E402
from autoclipper.config import Config  # noqa: E402
from autoclipper.db import DB  # noqa: E402
from autoclipper.publishing import Engine, platform_metadata, quality_check  # noqa: E402
from autoclipper.secure import Vault  # noqa: E402
from autoclipper.social import AuthError, Permanent, Provider, Result, Transient  # noqa: E402
from autoclipper.social.buffer import lit  # noqa: E402
from autoclipper.social.tiktok import chunk_plan, validate_approval  # noqa: E402
from autoclipper.social.youtube import build_body  # noqa: E402


class StubProvider(Provider):
    """Behaves like a platform: scripted outcomes per call."""
    name = "stub"

    def __init__(self, ctx, outcomes=None):
        super().__init__(ctx)
        self.outcomes = list(outcomes or [])
        self.published = []
        self.checks = 0

    def configured(self):
        return True

    def verify(self, account):
        return {}

    def publish(self, job, account, video, public_url, progress):
        if job.get("upload_state", {}) and job["upload_state"].get("sent"):
            return Result("processing", "remote-1", None, job["upload_state"])
        progress({"sent": False})
        out = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(out, Exception):
            raise out
        self.published.append(job["id"])
        return Result("processing", f"remote-{job['id']}", None, {"sent": True})

    def check(self, job, account):
        self.checks += 1
        return Result("published", job["remote_id"], f"https://example.invalid/{job['remote_id']}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOCLIPPER_SECRET_KEY", raising=False)
    cfg = Config(tmp_path)
    cfg.update({"publishing": {"dry_run": False, "posting_times": ["10:00", "14:00", "18:00", "21:00"],
                               "timezone": "Europe/Amsterdam", "min_interval_min": 60, "daily_limit": 2}})
    db = DB(cfg.db_path)
    vault = Vault(tmp_path, db)
    eng = Engine(cfg, db, vault)
    stub = StubProvider(eng.ctx)
    eng.providers["stub"] = stub
    db.add_streamer("xqc", rights_granted=True)
    return cfg, db, eng, stub, tmp_path


def make_clip(db, tmp_path, score=90.0, streamer="xqc", t0=1000.0, fp=("a", "b", "c")):
    video = tmp_path / f"clip{score}{t0}.mp4"
    if not video.exists():
        os.system(f"ffmpeg -loglevel error -y -f lavfi -i color=c=black:s=1080x1920:d=4 -f lavfi "
                  f"-i sine=d=4 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest {video}")
    return db.insert("clips", {"streamer": streamer, "path": str(video), "duration": 4.0, "score": score,
                               "scores": {"fingerprint": list(fp)}, "title": "xQc: \"NO WAY\" 😳", "hook": "NO WAY",
                               "hashtags": ["xqc", "gta6", "twitch"], "status": "pending",
                               "src_start": t0, "src_end": t0 + 30, "created_at": time.time()})


def add_account(eng, provider="stub", platform="youtube", **over):
    ids = eng.save_accounts(provider, [{"external_id": f"ext-{provider}-{platform}-{len(over)}-{time.time()}",
                                        "platform": platform, "display_name": "Test", "tokens": {"k": "v"}}])
    eng.db.update("accounts", ids[0], {"auto_publish": 1, "dry_run": 0, **over})
    return eng.account(ids[0])


# ---- encryption --------------------------------------------------------------
def test_tokens_encrypted_at_rest(env):
    cfg, db, eng, stub, tmp = env
    acc = add_account(eng)
    raw = db.one("SELECT tokens_enc FROM accounts WHERE id=?", (acc["id"],))["tokens_enc"]
    assert '"k"' not in raw and raw.startswith("gAAAA")  # Fernet token, not plaintext JSON
    assert eng.vault.decrypt(raw) == {"k": "v"}
    assert (tmp / "secrets" / "master.key").stat().st_mode & 0o777 == 0o600


def test_vault_named_secrets_env_wins(env, monkeypatch):
    cfg, db, eng, stub, tmp = env
    eng.vault.set("BUFFER_API_KEY", "stored-value")
    assert eng.vault.get("BUFFER_API_KEY") == "stored-value"
    assert "stored-value" not in str(db.query("SELECT value_enc FROM secrets"))
    monkeypatch.setenv("BUFFER_API_KEY", "env-value")
    assert eng.vault.get("BUFFER_API_KEY") == "env-value"
    assert eng.vault.status()["BUFFER_API_KEY"] == "env"
    with pytest.raises(ValueError):
        eng.vault.set("DASHBOARD_PASSWORD", "x")


# ---- planning rules ------------------------------------------------------------
def test_plan_respects_score_rights_and_streamers(env):
    cfg, db, eng, stub, tmp = env
    acc = add_account(eng)
    low = make_clip(db, tmp, score=70)
    assert eng.plan_clip(low) == []                              # below 80
    db.add_streamer("other", rights_granted=False)
    other = make_clip(db, tmp, score=95, streamer="other", t0=5000, fp=("x",))
    assert eng.plan_clip(other) == []                            # no rights
    db.update("accounts", acc["id"], {"streamers": ["somebody"]})
    good = make_clip(db, tmp, score=92, t0=9000, fp=("q",))
    assert eng.plan_clip(good) == []                             # streamer not selected
    db.update("accounts", acc["id"], {"streamers": None})
    res = eng.plan_clip(good)
    assert len(res) == 1 and res[0]["job_id"]


def test_duplicates_prevented(env):
    cfg, db, eng, stub, tmp = env
    add_account(eng)
    c1 = make_clip(db, tmp, score=90, t0=1000, fp=("a", "b", "c", "d"))
    c2 = make_clip(db, tmp, score=91, t0=1010, fp=("z",))              # same moment (overlap)
    c3 = make_clip(db, tmp, score=91, t0=9000, fp=("a", "b", "c", "d"))  # same content
    assert len(eng.plan_clip(c1)) == 1
    assert eng.plan_clip(c1) == []
    r2 = eng.plan_clip(c2, manual=True)
    r3 = eng.plan_clip(c3, manual=True)
    assert "same moment" in r2[0]["skipped"]
    assert "near-duplicate" in r3[0]["skipped"]


def test_slots_daily_limit_interval_and_times(env):
    cfg, db, eng, stub, tmp = env
    acc = add_account(eng, daily_limit=2, min_interval_min=60, posting_times=["10:00", "10:30", "14:00", "18:00"])
    tz = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 11, 20, 9, 0, tzinfo=tz).timestamp()
    s1 = eng.next_slot(acc, now)
    assert datetime.fromtimestamp(s1, tz).strftime("%H:%M") == "10:00"
    for i, s in enumerate([s1]):
        db.insert("publish_jobs", {"clip_id": 900 + i, "account_id": acc["id"], "status": "scheduled",
                                   "scheduled_at": s, "created_at": now, "updated_at": now})
    s2 = eng.next_slot(acc, now)
    assert datetime.fromtimestamp(s2, tz).strftime("%H:%M") == "14:00"   # 10:30 is within 60 min
    db.insert("publish_jobs", {"clip_id": 999, "account_id": acc["id"], "status": "scheduled",
                               "scheduled_at": s2, "created_at": now, "updated_at": now})
    s3 = eng.next_slot(acc, now)
    d3 = datetime.fromtimestamp(s3, tz)
    assert d3.day == 21 and d3.strftime("%H:%M") == "10:00"               # daily limit 2 -> next day


def test_immediate_mode(env):
    cfg, db, eng, stub, tmp = env
    acc = add_account(eng, posting_times="immediate")
    now = time.time()
    assert abs(eng.next_slot(acc, now) - now) < 1


# ---- worker state machine ------------------------------------------------------
def due(db, job_id):
    db.update("publish_jobs", job_id, {"scheduled_at": time.time() - 1})


def test_upload_verify_and_url(env, monkeypatch):
    cfg, db, eng, stub, tmp = env
    monkeypatch.setattr("autoclipper.editor.ensure_variant", lambda cfg, db, clip, style: Path(clip["path"]))
    add_account(eng, provider="stub")
    jid = eng.plan_clip(make_clip(db, tmp))[0]["job_id"]
    due(db, jid)
    eng.tick()
    j = db.one("SELECT * FROM publish_jobs WHERE id=?", (jid,))
    assert j["status"] == "processing" and j["remote_id"] == f"remote-{jid}"
    db.update("publish_jobs", jid, {"next_attempt_at": 0})
    eng.tick()
    j = db.one("SELECT * FROM publish_jobs WHERE id=?", (jid,))
    assert j["status"] == "published" and j["url"].endswith(f"remote-{jid}")


def test_transient_errors_retry_with_backoff_then_fail(env, monkeypatch):
    cfg, db, eng, stub, tmp = env
    monkeypatch.setattr("autoclipper.editor.ensure_variant", lambda cfg, db, clip, style: Path(clip["path"]))
    cfg.update({"publishing": {"max_attempts": 3}})
    stub.outcomes = [Transient("HTTP 503"), Transient("HTTP 503"), Transient("HTTP 503")]
    add_account(eng, provider="stub")
    jid = eng.plan_clip(make_clip(db, tmp))[0]["job_id"]
    delays = []
    for _ in range(3):
        db.update("publish_jobs", jid, {"scheduled_at": time.time() - 1, "next_attempt_at": None})
        eng.tick()
        j = db.one("SELECT * FROM publish_jobs WHERE id=?", (jid,))
        if j["next_attempt_at"]:
            delays.append(j["next_attempt_at"] - time.time())
    assert j["status"] == "failed" and "gave up" in j["error"]
    assert delays[1] > delays[0] > 60  # exponential backoff


def test_permanent_error_fails_immediately(env, monkeypatch):
    cfg, db, eng, stub, tmp = env
    monkeypatch.setattr("autoclipper.editor.ensure_variant", lambda cfg, db, clip, style: Path(clip["path"]))
    stub.outcomes = [Permanent("rejected: copyright")]
    add_account(eng, provider="stub")
    jid = eng.plan_clip(make_clip(db, tmp))[0]["job_id"]
    due(db, jid)
    eng.tick()
    assert db.one("SELECT status FROM publish_jobs WHERE id=?", (jid,))["status"] == "failed"


def test_auth_error_marks_account_and_keeps_job(env, monkeypatch):
    cfg, db, eng, stub, tmp = env
    monkeypatch.setattr("autoclipper.editor.ensure_variant", lambda cfg, db, clip, style: Path(clip["path"]))
    stub.outcomes = [AuthError("token revoked")]
    acc = add_account(eng, provider="stub")
    jid = eng.plan_clip(make_clip(db, tmp))[0]["job_id"]
    due(db, jid)
    eng.tick()
    assert eng.account(acc["id"])["status"] == "expired"
    j = db.one("SELECT * FROM publish_jobs WHERE id=?", (jid,))
    assert j["status"] == "scheduled" and j["attempts"] == 0


def test_emergency_stop_and_dry_run(env, monkeypatch):
    cfg, db, eng, stub, tmp = env
    monkeypatch.setattr("autoclipper.editor.ensure_variant", lambda cfg, db, clip, style: Path(clip["path"]))
    add_account(eng, provider="stub")
    jid = eng.plan_clip(make_clip(db, tmp))[0]["job_id"]
    due(db, jid)
    cfg.update({"publishing": {"emergency_stop": True}})
    eng.tick()
    assert db.one("SELECT status FROM publish_jobs WHERE id=?", (jid,))["status"] == "scheduled"
    cfg.update({"publishing": {"emergency_stop": False, "dry_run": True}})
    eng.tick()
    j = db.one("SELECT * FROM publish_jobs WHERE id=?", (jid,))
    assert j["status"] == "dry_run" and "dry run OK" in j["error"] and stub.published == []


def test_crash_recovery_does_not_duplicate(env, monkeypatch):
    cfg, db, eng, stub, tmp = env
    monkeypatch.setattr("autoclipper.editor.ensure_variant", lambda cfg, db, clip, style: Path(clip["path"]))
    add_account(eng, provider="stub")
    jid = eng.plan_clip(make_clip(db, tmp))[0]["job_id"]
    # simulate a crash after the platform accepted the upload but before we saved "processing"
    db.update("publish_jobs", jid, {"status": "uploading", "upload_state": {"sent": True}, "lease_until": time.time() + 999})
    eng.recover()
    j = db.one("SELECT * FROM publish_jobs WHERE id=?", (jid,))
    assert j["status"] == "scheduled"
    due(db, jid)
    eng.tick()
    assert stub.published == []  # resumed from saved state, not uploaded twice
    assert db.one("SELECT status FROM publish_jobs WHERE id=?", (jid,))["status"] == "processing"


# ---- TikTok compliance -----------------------------------------------------------
CREATOR = {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "comment_disabled": False,
           "duet_disabled": True, "stitch_disabled": False, "max_video_post_duration_sec": 600}


def test_tiktok_approval_rules():
    base = {"title": "caption", "privacy_level": "PUBLIC_TO_EVERYONE", "consent": True}
    assert validate_approval(base, CREATOR, 30) == []
    assert validate_approval({**base, "privacy_level": None}, CREATOR, 30)        # no default privacy
    assert validate_approval({**base, "consent": False}, CREATOR, 30)             # explicit consent
    assert validate_approval({**base, "allow_duet": True}, CREATOR, 30)           # disabled by creator
    assert validate_approval({**base, "commercial": True}, CREATOR, 30)           # must pick brand option
    assert validate_approval({**base, "commercial": True, "brand_content": True,
                              "privacy_level": "SELF_ONLY"}, CREATOR, 30)         # branded can't be private
    assert validate_approval(base, CREATOR, 900)                                  # too long


def test_tiktok_jobs_wait_for_approval(env):
    cfg, db, eng, stub, tmp = env
    acc = add_account(eng, provider="tiktok", platform="tiktok")
    res = eng.plan_clip(make_clip(db, tmp))
    assert res[0]["approval"] is True
    assert db.one("SELECT status FROM publish_jobs WHERE id=?", (res[0]["job_id"],))["status"] == "awaiting_approval"
    assert acc


def test_tiktok_chunks():
    MB = 1024 * 1024
    assert chunk_plan(2 * MB) == (2 * MB, 1)
    assert chunk_plan(35 * MB) == (10 * MB, 3)


# ---- request building --------------------------------------------------------------
def test_youtube_body(env):
    cfg, db, eng, stub, tmp = env
    job = {"title": "x" * 150, "description": "desc", "hashtags": ["gta6", "xqc"], "options": {}}
    body = build_body(job, {"ai_disclosure": 1, "privacy": "unlisted"}, cfg)
    assert len(body["snippet"]["title"]) == 100
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["privacyStatus"] == "unlisted"
    assert "#Shorts" in body["snippet"]["description"] and "#gta6" in body["snippet"]["description"]


def test_platform_metadata_limits(env):
    cfg, db, eng, stub, tmp = env
    clip = {"title": "t", "streamer": "xqc", "hashtags": [f"h{i}" for i in range(12)]}
    assert len(platform_metadata(clip, "instagram", cfg)["hashtags"]) == 5
    assert len(platform_metadata(clip, "youtube", cfg)["hashtags"]) == 8
    assert "twitch.tv/xqc" in platform_metadata(clip, "tiktok", cfg)["description"]


def test_graphql_literal_escaping():
    assert lit('He said "hi"\n') == '"He said \\"hi\\"\\n"'


def test_quality_check(tmp_path):
    good = tmp_path / "g.mp4"
    os.system(f"ffmpeg -loglevel error -y -f lavfi -i color=c=black:s=1080x1920:d=4 -f lavfi -i sine=d=4 "
              f"-c:v libx264 -pix_fmt yuv420p -c:a aac -shortest {good}")
    bad = tmp_path / "b.mp4"
    os.system(f"ffmpeg -loglevel error -y -f lavfi -i color=c=black:s=640x360:d=2 -c:v libx264 -pix_fmt yuv420p {bad}")
    assert quality_check(good, "youtube") == []
    probs = quality_check(bad, "youtube")
    assert any("1080x1920" in p for p in probs) and "no audio" in probs and "shorter than 3 s" in probs


# ---- analytics: no fabrication -------------------------------------------------------
def test_analytics_never_invents_numbers(env):
    cfg, db, eng, stub, tmp = env
    acc = add_account(eng, provider="stub")
    now = time.time()
    for i, views in enumerate([100, 200, 300, None]):
        cid = make_clip(db, tmp, score=90, t0=20000 + i * 1000, fp=(str(i),))
        jid = db.insert("publish_jobs", {"clip_id": cid, "account_id": acc["id"], "status": "published",
                                         "style": "clean", "published_at": now - 3 * 86400, "created_at": now,
                                         "updated_at": now})
        db.insert("metrics", {"job_id": jid, "fetched_at": now, "age_hours": 72, "views": views})
    ins = analytics.insights(db, cfg)
    assert ins["totals"]["views"] == 600
    assert ins["totals"]["shares"] is None                      # nobody reported shares -> n/a, not 0
    assert ins["by_streamer"]["xqc"]["n"] == 3                  # the post without views is excluded
    assert analytics.streamer_bonus(db, cfg, "xqc") == pytest.approx(0.0, abs=1.0)
