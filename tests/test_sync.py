"""sync worker 测试: at-least-once / 防重 / 重试 / disable flag."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

# 测试需要独立 DB, 避免污染开发库
TEST_DB = Path(__file__).resolve().parent.parent / "data" / "test_sync.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
if TEST_DB.exists():
    TEST_DB.unlink()

import db  # noqa: E402

db.DB_PATH = TEST_DB

import sync  # noqa: E402


SECRET = "test-zhaoshang-secret-do-not-use-prod"


def _setup_env(enabled: bool = True, url: str = "https://mock.example/api"):
    os.environ["ENABLE_ZHAOSHANG_SYNC"] = "1" if enabled else "0"
    os.environ["ZHAOSHANG_URL"] = url
    os.environ["ZHAOSHANG_SECRET"] = SECRET
    os.environ["SYNC_MAX_ATTEMPTS"] = "3"


def _clear_db():
    if TEST_DB.exists():
        TEST_DB.unlink()


def _insert_message_and_feed(
    raw_message: str = "test 9.9元",
    group_id: str = "100",
    group_name: str = "test-group",
) -> int:
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO messages (self_id, group_id, group_name, raw_message, payload_json) "
        "VALUES ('1', ?, ?, ?, '{}')",
        (group_id, group_name, raw_message),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO feeds (message_id, price, product_name, raw_message) "
        "VALUES (?, 9.9, '测试品', ?)",
        (mid, raw_message),
    )
    conn.commit()
    conn.close()
    return mid


def _feed_status(feed_id: int = 1) -> dict:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT sync_status, sync_attempts, synced_at, last_sync_error "
        "FROM feeds WHERE id=?",
        (feed_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def test_disabled_skips_everything():
    _clear_db()
    _setup_env(enabled=False)
    _insert_message_and_feed()
    result = asyncio.run(sync.sync_one_batch())
    assert result == {"skipped": True, "reason": "ENABLE_ZHAOSHANG_SYNC != 1"}
    assert _feed_status()["sync_status"] == "PENDING"


def test_no_url_skips():
    _clear_db()
    _setup_env(url="")
    _insert_message_and_feed()
    result = asyncio.run(sync.sync_one_batch())
    assert result["skipped"] is True


def test_successful_post_marks_synced():
    _clear_db()
    _setup_env()
    _insert_message_and_feed()

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["sig"] = request.headers.get("x-signature")
        return httpx.Response(200, json={"id": 42})

    transport = httpx.MockTransport(handler)
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await sync.sync_one_batch(client=client)

    result = asyncio.run(run())
    assert result["sent"] == 1
    assert result["failed"] == 0
    status = _feed_status()
    assert status["sync_status"] == "SYNCED"
    assert status["synced_at"]
    payload = json.loads(captured["body"])
    assert payload["source"] == "caiji-qq"
    assert payload["source_message_id"] == 1
    assert payload["parsed_price"] == 9.9
    expected_sig = "sha1=" + hmac.new(
        SECRET.encode(), captured["body"], hashlib.sha1
    ).hexdigest()
    assert captured["sig"] == expected_sig


def test_409_treated_as_idempotent_success():
    _clear_db()
    _setup_env()
    _insert_message_and_feed()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already exists"})

    transport = httpx.MockTransport(handler)
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await sync.sync_one_batch(client=client)

    result = asyncio.run(run())
    assert result["sent"] == 1
    assert _feed_status()["sync_status"] == "SYNCED"


def test_500_retried_then_failed_after_max_attempts():
    _clear_db()
    _setup_env()
    _insert_message_and_feed()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    transport = httpx.MockTransport(handler)
    async def run_once():
        async with httpx.AsyncClient(transport=transport) as client:
            return await sync.sync_one_batch(client=client)

    asyncio.run(run_once())
    status = _feed_status()
    assert status["sync_status"] == "PENDING"
    assert status["sync_attempts"] == 1
    assert "500" in (status["last_sync_error"] or "")

    asyncio.run(run_once())
    asyncio.run(run_once())
    status = _feed_status()
    assert status["sync_status"] == "FAILED"
    assert status["sync_attempts"] == 3

    asyncio.run(run_once())
    status = _feed_status()
    assert status["sync_attempts"] == 3


def test_network_error_does_not_lose_data():
    _clear_db()
    _setup_env()
    _insert_message_and_feed()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    transport = httpx.MockTransport(handler)
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await sync.sync_one_batch(client=client)

    result = asyncio.run(run())
    assert result["failed"] == 1
    status = _feed_status()
    assert status["sync_status"] == "PENDING"
    assert "ConnectError" in (status["last_sync_error"] or "")


def test_synced_rows_not_resent():
    _clear_db()
    _setup_env()
    _insert_message_and_feed(raw_message="first 1.0元")
    _insert_message_and_feed(raw_message="second 2.0元", group_id="200")

    posted: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            r1 = await sync.sync_one_batch(client=client)
            r2 = await sync.sync_one_batch(client=client)
            return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1["sent"] == 2
    assert r2.get("total", 0) == 0
    assert len(posted) == 2


def test_signature_is_secret_dependent():
    body = b'{"hello":"world"}'
    s1 = sync.sign_body(body, "secret-a")
    s2 = sync.sign_body(body, "secret-b")
    assert s1 != s2
    assert s1.startswith("sha1=")


def test_payload_shape():
    row = {
        "feed_id": 1,
        "message_id": 7,
        "group_id": "12345",
        "group_name": "群名",
        "price": 19.9,
        "product_name": "名字",
        "taobao_token": "abc123",
        "coupon_code": "HU1234",
        "quantity": 2,
        "raw_message": "原文",
        "parsed_at": "2026-05-14 15:00:00",
    }
    p = sync.build_candidate_payload(row)
    assert p["source"] == "caiji-qq"
    assert p["source_message_id"] == 7
    assert p["parsed_price"] == 19.9
    assert p["product_name"] == "名字"
    assert "feed_id" not in p
