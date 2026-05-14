"""FastAPI server 测试: 验签 + 路由."""
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

os.environ["CAIJI_SECRET"] = "test_secret_for_unit_test_only_do_not_use_in_prod"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402

client = TestClient(app)
TEST_SECRET = os.environ["CAIJI_SECRET"]


def _sign(body: bytes) -> str:
    return "sha1=" + hmac.new(
        TEST_SECRET.encode("utf-8"), body, hashlib.sha1
    ).hexdigest()


def _sample_payload() -> dict:
    return {
        "self_id": 100,
        "user_id": 1,
        "group_id": 999,
        "group_name": "unit-test-group",
        "message_type": "group",
        "sub_type": "normal",
        "post_type": "message",
        "sender": {"nickname": "unit-tester"},
        "raw_message": "测试 9.9元 (testToken) HU1234",
    }


def test_root_health():
    resp = client.get("/")
    assert resp.status_code == 200
    j = resp.json()
    assert j["status"] == "ok"
    assert j["service"] == "caiji-mvp"


def test_valid_signature_accepts_post():
    body = json.dumps(_sample_payload()).encode("utf-8")
    resp = client.post(
        "/onebot/event",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["code"] == 0


def test_missing_signature_rejects_post():
    body = json.dumps(_sample_payload()).encode("utf-8")
    resp = client.post(
        "/onebot/event",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert "X-Signature" in resp.json()["detail"]


def test_wrong_signature_rejects_post():
    body = json.dumps(_sample_payload()).encode("utf-8")
    resp = client.post(
        "/onebot/event",
        content=body,
        headers={
            "X-Signature": "sha1=" + "0" * 40,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert "mismatch" in resp.json()["detail"]


def test_invalid_signature_scheme_rejects_post():
    body = json.dumps(_sample_payload()).encode("utf-8")
    resp = client.post(
        "/onebot/event",
        content=body,
        headers={
            "X-Signature": "Bearer somebadtoken",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_signature_is_body_dependent():
    """改了 1 byte body, 旧签名必须失效 (HMAC 完整性)."""
    original = json.dumps(_sample_payload()).encode("utf-8")
    sig_for_original = _sign(original)
    tampered = original.replace(b"9.9", b"9.8")
    assert tampered != original
    resp = client.post(
        "/onebot/event",
        content=tampered,
        headers={"X-Signature": sig_for_original, "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_invalid_json_returns_400_after_signature_pass():
    body = b"not-valid-json"
    resp = client.post(
        "/onebot/event",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "Invalid JSON" in resp.json()["detail"]


def test_non_object_payload_returns_400():
    body = b"[1, 2, 3]"
    resp = client.post(
        "/onebot/event",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "JSON object" in resp.json()["detail"]
