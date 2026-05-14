"""Parser 测试（基于廖宇杰 2026-04-28 真实样本）."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser import parse_message


def test_25_5_yuan_361_t_shirt():
    raw = "25.5亓‼\r\n1、下拉详情，加1件\r\n361运动短袖T恤\r\n(qqQq5kXmJJ6)/ HU9901 "
    feed = parse_message(raw)
    assert feed.price == 25.5
    assert feed.taobao_token == "qqQq5kXmJJ6"
    assert feed.coupon_code == "HU9901"
    assert feed.quantity == 1
    assert feed.product_name and "361" in feed.product_name


def test_28_3_yuan_veet():
    raw = "进淘淦币拍，28.3元‼\r\nVeet薇婷 净纯脱毛膏50ml\r\n/ CZ356 IUjb5k2HllH/"
    feed = parse_message(raw)
    assert feed.price == 28.3
    assert feed.coupon_code == "CZ356"
    assert feed.product_name and "Veet" in feed.product_name


def test_25_5_with_hu7709():
    raw = "25.5亓‼\r\n1、下拉详情，加1件\r\n361运动短袖T恤\r\n(JKDq5kXmdqP)// HU7709"
    feed = parse_message(raw)
    assert feed.price == 25.5
    assert feed.taobao_token == "JKDq5kXmdqP"
    assert feed.coupon_code == "HU7709"


def test_no_price_returns_none():
    feed = parse_message("hi this is just a chat message")
    assert feed.price is None
    assert feed.taobao_token is None
    assert feed.coupon_code is None


def test_empty_message():
    feed = parse_message("")
    assert feed.price is None
    assert feed.product_name is None
    assert feed.raw_message == ""
