"""AI Reply must authenticate ChatGPT Subscription exactly like chat does.

Chat routes every session through ``resolve_session_auth`` which discards the
stored session headers for ChatGPT Subscription (the bearer is never persisted)
and rebuilds them from ``resolve_endpoint_runtime`` + ``build_headers`` — the
path that refreshes an expiring OAuth access token. AI Reply used to read the
raw ``sessions.headers`` column instead, sending an empty or stale bearer to
chatgpt.com/backend-api/codex and getting 401 while chat kept working.
"""

import base64
import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database
import routes.chat_helpers as chat_helpers
import routes.email.email_routes as email_routes
import src.chatgpt_subscription as chatgpt_subscription
import src.endpoint_resolver as endpoint_resolver
import src.llm_core as llm_core
from core.database import Base, ModelEndpoint, ProviderAuthSession, Session as DbSession

_CODEX_BASE = "https://chatgpt.com/backend-api/codex"
_CODEX_URL = endpoint_resolver.build_chat_url(_CODEX_BASE)
_MODEL = "gpt-5.6-luna"


def _jwt(exp: int) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(chat_helpers, "SessionLocal", TestSessionLocal)

    db = TestSessionLocal()
    db.add(ProviderAuthSession(
        id="auth1", provider=chatgpt_subscription.CHATGPT_SUBSCRIPTION_PROVIDER,
        owner="alice", base_url=_CODEX_BASE, auth_mode="chatgpt",
        access_token=_jwt(int(time.time()) + 3600), refresh_token="refresh-1",
    ))
    db.add(ModelEndpoint(
        id="ep1", name="ChatGPT Subscription", base_url=_CODEX_BASE,
        provider_auth_id="auth1", owner="alice", is_enabled=True, api_key=None,
    ))
    # Chat already scrubbed the bearer (or left a stale one) in the row.
    db.add(DbSession(
        id="sess1", name="chat", endpoint_url=_CODEX_URL, endpoint_id="ep1",
        model=_MODEL, owner="alice",
        headers={"Authorization": "Bearer stale-session-token"},
    ))
    db.commit()
    db.close()

    # Isolate the handler from settings, mail config and other endpoints.
    monkeypatch.setattr(email_routes, "_load_settings", lambda: {})
    monkeypatch.setattr(email_routes, "_get_email_config", lambda *a, **k: {"from_address": "me@example.com"})
    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(endpoint_resolver, "resolve_utility_fallback_candidates", lambda **k: [])
    monkeypatch.setattr(llm_core, "list_model_ids", lambda *a, **k: [_MODEL])

    calls = []

    async def fake_llm(candidates, **kwargs):
        calls.append(list(candidates))
        return "<<<REPLY>>>Thanks, I will review this and get back to you.<<<END>>>"

    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake_llm)
    return TestSessionLocal, calls


async def _ai_reply(data):
    router = email_routes.setup_email_routes()
    endpoint = next(r.endpoint for r in router.routes if r.path.endswith("/ai-reply"))
    return await endpoint(data, owner="alice")


def _chat_headers(TestSessionLocal):
    """Headers the chat path would send for the same session."""
    db = TestSessionLocal()
    row = db.query(DbSession).filter(DbSession.id == "sess1").first()
    import types
    sess = types.SimpleNamespace(
        id=row.id, owner=row.owner, endpoint_url=row.endpoint_url,
        endpoint_id=row.endpoint_id, model=row.model, headers=row.headers,
    )
    db.close()
    chat_helpers.resolve_session_auth(sess, "sess1", owner="alice")
    return sess.headers


@pytest.mark.asyncio
async def test_ai_reply_uses_chat_auth_builder_for_chatgpt_subscription(env, monkeypatch):
    TestSessionLocal, calls = env
    used = {}
    real_resolve_session_auth = chat_helpers.resolve_session_auth

    def spy(sess, session_id, owner=None):
        used["session_auth"] = (session_id, owner)
        return real_resolve_session_auth(sess, session_id, owner=owner)

    monkeypatch.setattr(chat_helpers, "resolve_session_auth", spy)
    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint_runtime",
        lambda ep, owner=None: (_CODEX_BASE, "live-access-token"),
    )

    result = await _ai_reply({"session_id": "sess1", "original_body": "Can you review the draft?", "fast": True})

    assert result["success"] is True, result
    assert used["session_auth"] == ("sess1", "alice")
    url, model, headers = calls[0][0]
    assert (url, model) == (_CODEX_URL, _MODEL)
    assert headers["Authorization"] == "Bearer live-access-token"
    # Byte-for-byte the same header set chat builds for this session.
    monkeypatch.setattr(chat_helpers, "resolve_session_auth", real_resolve_session_auth)
    assert headers == _chat_headers(TestSessionLocal)
    assert headers == chatgpt_subscription.chatgpt_headers("live-access-token")


@pytest.mark.asyncio
async def test_ai_reply_refreshes_expiring_chatgpt_token_like_chat(env, monkeypatch):
    TestSessionLocal, calls = env
    db = TestSessionLocal()
    row = db.query(ProviderAuthSession).filter(ProviderAuthSession.id == "auth1").first()
    row.access_token = _jwt(int(time.time()) - 10)
    db.commit()
    db.close()

    fresh = _jwt(int(time.time()) + 3600)
    refreshes = []

    def fake_refresh(access_token, refresh_token, timeout=20.0):
        refreshes.append(refresh_token)
        return {"access_token": fresh, "refresh_token": "refresh-2"}

    monkeypatch.setattr(chatgpt_subscription, "refresh_oauth_tokens", fake_refresh)

    result = await _ai_reply({"session_id": "sess1", "original_body": "Can you review the draft?", "fast": True})

    assert result["success"] is True, result
    assert refreshes == ["refresh-1"]
    _url, _model, headers = calls[0][0]
    assert headers["Authorization"] == f"Bearer {fresh}"
    assert "stale-session-token" not in json.dumps(headers)
    # Chat sees the same refreshed bearer (no second refresh needed).
    assert _chat_headers(TestSessionLocal) == headers
    assert refreshes == ["refresh-1"]
    # The bearer is never persisted onto the sessions row.
    db = TestSessionLocal()
    stored = db.query(DbSession).filter(DbSession.id == "sess1").first().headers or {}
    db.close()
    assert not any(k.lower() == "authorization" for k in stored)
