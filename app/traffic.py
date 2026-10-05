"""默认投递的稳定分桶：按规范化收件地址把请求确定性地分到基线或候选版本。

仅对规范化地址本身取哈希（SHA-256，取前 8 字节），不混入策略内容，
因此同一地址在同一策略下永远落在同一桶；调整候选流量比例只会让分桶
边界移动（地址随比例单调地从基线侧进入候选侧），已分桶地址不会来回抖动。
"""
from __future__ import annotations

import hashlib

MODULUS = 100


def bucket_for(address: str) -> int:
    """返回地址的稳定桶号（0–99）。address 应为已规范化地址（小写、去空白）。"""
    digest = hashlib.sha256(address.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % MODULUS


def select_version(address: str, candidate_percent: int) -> str:
    """按候选流量比例选择版本：桶号 < 比例 → 候选，否则基线。

    0% 全选基线，100% 全选候选；同一地址同一比例结果恒定。
    返回 "baseline" 或 "candidate"。
    """
    return "candidate" if bucket_for(address) < candidate_percent else "baseline"
