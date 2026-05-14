"""FastAPI 接收端：监听 NapCat OneBot 11 HTTP 上报."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from db import DB_PATH, get_conn
from parser import parse_message

app = FastAPI(title="caiji-mvp", description="QQ 群消息采集 MVP")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
JSONL_PATH = DATA_DIR / "messages.jsonl"


def log_to_jsonl(payload: dict[str, Any]) -> None:
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        record = {"received_at": datetime.now().isoformat(), "payload": payload}
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_to_db(payload: dict[str, Any]):
    if payload.get("post_type") != "message":
        return None
    if payload.get("message_type") != "group":
        return None

    conn = get_conn()
    try:
        sender = payload.get("sender") or {}
        cur = conn.execute(
            """
            INSERT INTO messages (
                self_id, user_id, group_id, group_name,
                sender_nickname, sender_card, sender_role,
                message_type, sub_type, raw_message, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload.get("self_id", "")),
                str(payload.get("user_id", "")),
                str(payload.get("group_id", "")),
                payload.get("group_name", ""),
                sender.get("nickname", ""),
                sender.get("card", ""),
                sender.get("role", ""),
                payload.get("message_type", ""),
                payload.get("sub_type", ""),
                payload.get("raw_message", ""),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        message_id = cur.lastrowid

        feed = parse_message(payload.get("raw_message") or "")
        if feed.price or feed.taobao_token or feed.coupon_code:
            conn.execute(
                """
                INSERT INTO feeds (
                    message_id, price, product_name, taobao_token,
                    coupon_code, quantity, raw_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    feed.price,
                    feed.product_name,
                    feed.taobao_token,
                    feed.coupon_code,
                    feed.quantity,
                    feed.raw_message,
                ),
            )
        conn.commit()
        return message_id
    finally:
        conn.close()


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "caiji-mvp", "db": str(DB_PATH)}


@app.post("/onebot/event")
async def onebot_event(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    log_to_jsonl(payload)
    message_id = save_to_db(payload)
    return JSONResponse(content={"code": 0, "msg": "ok", "message_id": message_id})


@app.get("/recent")
async def recent(limit: int = 20) -> dict[str, Any]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT f.id, m.group_name, m.sender_nickname,
                   f.price, f.product_name, f.taobao_token,
                   f.coupon_code, f.quantity, f.parsed_at,
                   m.raw_message
            FROM feeds f
            JOIN messages m ON m.id = f.message_id
            ORDER BY f.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return {"count": len(rows), "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/stats")
async def stats() -> dict[str, Any]:
    conn = get_conn()
    try:
        total_msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        total_feed = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        by_group = conn.execute(
            """
            SELECT group_name, group_id, COUNT(*) as cnt
            FROM messages
            WHERE group_name != ''
            GROUP BY group_id ORDER BY cnt DESC LIMIT 10
            """
        ).fetchall()
        return {
            "total_messages": total_msg,
            "total_feeds": total_feed,
            "parse_rate": round(total_feed / total_msg, 3) if total_msg else 0,
            "top_groups": [dict(r) for r in by_group],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8090, reload=True)
