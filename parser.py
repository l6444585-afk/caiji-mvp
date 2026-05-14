"""消息解析器：从原始群消息提取价格、淘口令、商品、优惠券码、数量.

测试样本来自廖宇杰示范（2026-04-28 微信聊天截图）：
- "25.5亓‼\r\n1、下拉详情，加1件\r\n361运动短袖T恤\r\n(qqQq5kXmJJ6)/ HU9901 "
- "进淘淦币拍，28.3元‼\r\nVeet薇婷 净纯脱毛膏50ml\r\n/ CZ356 IUjb5k2HllH/"
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

PRICE_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*亓"),
    re.compile(r"(\d+(?:\.\d+)?)\s*元"),
    re.compile(r"¥\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(\d+(?:\.\d+)?)\s*¥"),
)

TAOBAO_TOKEN_PATTERNS = (
    re.compile(r"\(([A-Za-z0-9]{8,15})\)"),
    re.compile(r"（([A-Za-z0-9]{8,15})）"),
)

COUPON_PATTERN = re.compile(r"\b([A-Z]{2,3}\d{3,6})\b")

QUANTITY_PATTERN = re.compile(r"加?\s*(\d+)\s*件")

NOISE_LINE_PREFIXES = re.compile(
    r"^(?:\d+[、.]\s*)?"
    r"(下拉详情|下单详情|进淘|淘宝|拼多多|京东|抖音|直接拍|去抢|抢购|加件|下单|【|备注|提示|说明)",
)


@dataclass
class ParsedFeed:
    price: Optional[float] = None
    product_name: Optional[str] = None
    taobao_token: Optional[str] = None
    coupon_code: Optional[str] = None
    quantity: Optional[int] = None
    raw_message: str = ""


def _extract_price(text: str) -> Optional[float]:
    for pat in PRICE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _extract_token(text: str) -> Optional[str]:
    for pat in TAOBAO_TOKEN_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _extract_coupon(text: str) -> Optional[str]:
    m = COUPON_PATTERN.search(text)
    return m.group(1) if m else None


def _extract_quantity(text: str) -> Optional[int]:
    m = QUANTITY_PATTERN.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_product_name(text: str) -> Optional[str]:
    """找包含 ≥2 个中文字符、且不是噪音前缀、不是纯价格/口令的行."""
    for line in text.splitlines():
        line = line.strip(" /\\r\\n\t.")
        if not line:
            continue
        if NOISE_LINE_PREFIXES.search(line):
            continue
        if len(re.findall(r"[一-龥]", line)) < 2:
            continue
        if any(p.search(line) for p in PRICE_PATTERNS):
            continue
        # 去掉口令/券码后还有中文 → 当作商品名
        stripped = re.sub(r"\([A-Za-z0-9]+\)", "", line)
        stripped = re.sub(r"（[A-Za-z0-9]+）", "", stripped)
        stripped = COUPON_PATTERN.sub("", stripped).strip(" /\\\\")
        if len(re.findall(r"[一-龥]", stripped)) >= 2:
            return stripped[:80]
    return None


def parse_message(raw: str) -> ParsedFeed:
    """从原始群消息提取结构化字段."""
    return ParsedFeed(
        price=_extract_price(raw),
        product_name=_extract_product_name(raw),
        taobao_token=_extract_token(raw),
        coupon_code=_extract_coupon(raw),
        quantity=_extract_quantity(raw),
        raw_message=raw,
    )
