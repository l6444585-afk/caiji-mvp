"""caiji → zhaoshang Phase 2 同步 worker.

行为：
1. 每 SYNC_INTERVAL_SEC 秒扫一批 feeds (sync_status='PENDING'), POST 到 zhaoshang 候选池接口
2. 200/201/204/409 (幂等成功) → 标记 SYNCED
3. 其他状态码/异常 → sync_attempts+1, 仍标 PENDING 待下次重试
4. sync_attempts >= SYNC_MAX_ATTEMPTS → 标 FAILED (需人工 review)
5. ENABLE_ZHAOSHANG_SYNC=0 或 ZHAOSHANG_URL 未配置 → 跳过整个循环 (开发态默认关)

at-least-once 投递: 仅在确认收端成功后标记 SYNCED, 网络/进程崩溃下次还会推.
zhaoshang 接口必须**幂等**: 同一 (source='caiji-qq', source_message_id) 重复 POST 应返回 409 而不是重复入库.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import traceback
from datetime import datetime
from typing import Any

import httpx

from db import get_conn

SOURCE_TAG = "caiji-qq"


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def is_sync_enabled() -> bool:
    return os.environ.get("ENABLE_ZHAOSHANG_SYNC", "0") == "1"


def get_zhaoshang_url() -> str:
    return os.environ.get("ZHAOSHANG_URL", "")


def get_zhaoshang_secret() -> str:
    return os.environ.get("ZHAOSHANG_SECRET", "")


def sign_body(body: bytes, secret: str) -> str:
    return "sha1=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()


def build_candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    """把 caiji feed 行映射成 zhaoshang 候选池接口预期的 JSON 协议."""
    return {
        "source": SOURCE_TAG,
        "source_group_id": row.get("group_id") or None,
        "source_group_name": row.get("group_name") or None,
        "source_message_id": row["message_id"],
        "parsed_price": row.get("price"),
        "product_name": row.get("product_name"),
        "taobao_token": row.get("taobao_token"),
        "coupon_code": row.get("coupon_code"),
        "quantity": row.get("quantity"),
        "raw_message": row.get("raw_message"),
        "parsed_at": row.get("parsed_at"),
    }


def _fetch_pending_batch(batch_size: int, max_attempts: int) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT f.id AS feed_id, f.message_id, f.price, f.product_name,
                   f.taobao_token, f.coupon_code, f.quantity, f.raw_message,
                   f.parsed_at, m.group_id, m.group_name
            FROM feeds f
            JOIN messages m ON m.id = f.message_id
            WHERE f.sync_status = 'PENDING' AND f.sync_attempts < ?
            ORDER BY f.id ASC LIMIT ?
            """,
            (max_attempts, batch_size),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _mark_synced(feed_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE feeds SET sync_status='SYNCED', "
            "synced_at=datetime('now','localtime'), last_sync_error=NULL "
            "WHERE id=?",
            (feed_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_attempt(feed_id: int, error: str, max_attempts: int) -> None:
    """记一次失败. 达到 max_attempts 时切到 FAILED."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT sync_attempts FROM feeds WHERE id=?", (feed_id,)
        ).fetchone()
        attempts = (cur["sync_attempts"] if cur else 0) + 1
        new_status = "FAILED" if attempts >= max_attempts else "PENDING"
        conn.execute(
            "UPDATE feeds SET sync_status=?, sync_attempts=?, last_sync_error=? "
            "WHERE id=?",
            (new_status, attempts, error[:500], feed_id),
        )
        conn.commit()
    finally:
        conn.close()


async def sync_one_batch(
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """扫一批 PENDING feeds → POST 到 zhaoshang. 返回 stats."""
    if not is_sync_enabled():
        return {"skipped": True, "reason": "ENABLE_ZHAOSHANG_SYNC != 1"}
    url = get_zhaoshang_url()
    secret = get_zhaoshang_secret()
    if not url:
        return {"skipped": True, "reason": "ZHAOSHANG_URL not set"}
    if not secret:
        return {"skipped": True, "reason": "ZHAOSHANG_SECRET not set"}

    batch_size = _get_int("SYNC_BATCH_SIZE", 50)
    max_attempts = _get_int("SYNC_MAX_ATTEMPTS", 5)
    rows = _fetch_pending_batch(batch_size, max_attempts)
    if not rows:
        return {"sent": 0, "failed": 0, "pending_in_batch": 0}

    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
    sent = 0
    failed = 0
    try:
        for row in rows:
            payload = build_candidate_payload(row)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                resp = await client.post(
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Signature": sign_body(body, secret),
                    },
                )
                if resp.status_code in (200, 201, 204, 409):
                    _mark_synced(row["feed_id"])
                    sent += 1
                else:
                    _mark_attempt(
                        row["feed_id"],
                        f"HTTP {resp.status_code}: {resp.text[:200]}",
                        max_attempts,
                    )
                    failed += 1
            except Exception as e:  # noqa: BLE001
                _mark_attempt(
                    row["feed_id"],
                    f"{type(e).__name__}: {e}",
                    max_attempts,
                )
                failed += 1
    finally:
        if owned_client:
            await client.aclose()
    return {"sent": sent, "failed": failed, "total": len(rows)}


async def sync_loop() -> None:
    """长循环: 每 SYNC_INTERVAL_SEC 调一次 sync_one_batch.

    异常被 try/except 兜底, 不让 task die 影响后续循环.
    """
    interval = _get_int("SYNC_INTERVAL_SEC", 60)
    while True:
        try:
            stats = await sync_one_batch()
            if not stats.get("skipped") and stats.get("total", 0) > 0:
                print(
                    f"[sync] {datetime.now().isoformat(timespec='seconds')} {stats}",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(interval)
