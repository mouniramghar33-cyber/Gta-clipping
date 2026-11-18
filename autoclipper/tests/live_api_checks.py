"""Calls the REAL platform endpoints with deliberately invalid credentials and checks that each
integration reaches the API and classifies the error correctly (auth problem vs. retryable).
Nothing is posted. Run: python tests/live_api_checks.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoclipper.config import Config  # noqa: E402
from autoclipper.db import DB  # noqa: E402
from autoclipper.notify import Notifier  # noqa: E402
from autoclipper.publishing import Engine  # noqa: E402
from autoclipper.secure import Vault  # noqa: E402
from autoclipper.social import AuthError, ProviderError  # noqa: E402

d = Path(tempfile.mkdtemp())
cfg = Config(d)
db = DB(cfg.db_path)
vault = Vault(d, db)
for k, v in {"YT_CLIENT_ID": "invalid.apps.googleusercontent.com", "YT_CLIENT_SECRET": "invalid",
             "TIKTOK_CLIENT_KEY": "invalidkey", "TIKTOK_CLIENT_SECRET": "invalid",
             "BUFFER_API_KEY": "invalid-key", "UPLOADPOST_API_KEY": "invalid-key",
             "TELEGRAM_BOT_TOKEN": "123:invalid", "TELEGRAM_CHAT_ID": "1"}.items():
    vault.set(k, v)
eng = Engine(cfg, db, vault)
results = []


def check(name, fn, expect=AuthError):
    t = time.time()
    try:
        out = fn()
        results.append((name, "UNEXPECTED SUCCESS", str(out)[:120]))
    except ProviderError as e:
        ok = isinstance(e, expect)
        results.append((name, "ok" if ok else f"WRONG CLASS {type(e).__name__}", str(e)[:150]))
    except Exception as e:  # noqa: BLE001
        results.append((name, f"CRASH {type(e).__name__}", str(e)[:150]))
    results[-1] += (f"{time.time() - t:.1f}s",)


yt = {"id": 1, "provider": "youtube", "tokens_enc": vault.encrypt({"refresh_token": "invalid", "expires_at": 0})}
tt = {"id": 2, "provider": "tiktok", "tokens_enc": vault.encrypt({"refresh_token": "invalid", "expires_at": 0})}
db.execute("INSERT INTO accounts (id, provider, platform, external_id, created_at, updated_at) VALUES "
           "(1,'youtube','youtube','x',0,0),(2,'tiktok','tiktok','y',0,0)")
check("YouTube token refresh", lambda: eng.providers["youtube"].access_token(yt))
check("TikTok token refresh", lambda: eng.providers["tiktok"].access_token(tt))
check("Buffer API (organizations)", lambda: eng.providers["buffer"].connect())
check("Upload-Post API (profiles)", lambda: eng.providers["uploadpost"].connect())
ok, msg = Notifier(cfg, db, vault).test()
results.append(("Telegram sendMessage", "ok" if not ok else "UNEXPECTED SUCCESS", msg[:150], "-"))

w = max(len(r[0]) for r in results)
for r in results:
    print(f"{r[0]:<{w}}  {r[1]:<20} {r[3]:>6}  {r[2]}")

# ---- Buffer: validate our exact GraphQL documents against Buffer's live schema ----------
try:
    import httpx
    from graphql import build_client_schema, get_introspection_query, parse, validate

    from autoclipper.social.buffer import Buffer
    schema = build_client_schema(httpx.post("https://api.buffer.com", json={"query": get_introspection_query()},
                                            timeout=60).json()["data"])
    sent = []
    b = Buffer(eng.ctx)
    b.gql = lambda q: sent.append(q) or {"createPost": {"post": {"id": "p"}}, "post": {"status": "scheduled"},
                                         "account": {"organizations": [{"id": "o", "name": "n"}]}, "channels": []}
    for plat in ("youtube", "tiktok", "instagram"):
        b.publish({"id": 1, "title": 'quote "x"', "description": "d", "hashtags": ["a"]},
                  {"external_id": "c", "platform": plat, "ai_disclosure": 1}, Path("/x"), "https://h/v.mp4", lambda s: None)
    b.check({"upload_state": {"post_id": "p"}, "remote_id": "p"}, {})
    b.connect()
    bad = [(q[:60], [e.message for e in validate(schema, parse(q))]) for q in sent if validate(schema, parse(q))]
    print(f"Buffer GraphQL documents valid against live schema: {len(sent) - len(bad)}/{len(sent)}", bad or "")
except ImportError:
    print("pip install graphql-core to validate Buffer queries against the live schema")
