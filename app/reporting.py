"""历史投递判定核对报表：带时区时间窗（左闭右开）、分组计数与游标分页的查询参数处理。

- 时间窗：start / end 必须是带时区的 ISO 8601 时刻，规范化为 UTC 后与库中 created_at
  直接做字符串比较（created_at 一律为 UTC ISO 格式）；窗口左闭右开 [start, end)。
- 快照边界：首次查询在单只读事务内固定“截至当时已提交判定”的内部序号上界，
  连同筛选条件一起编码进不透明游标；后续翻页只凭游标，沿用同一筛选与边界——
  并发新投递的序号超出边界，不会进入旧报表；键集分页保证页间不漏不重。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

CURSOR_VERSION = 1


class ReportQueryError(Exception):
    """报表查询参数无效：invalid_time_window / invalid_page_size / invalid_cursor。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_instant(raw: object, *, field: str, code: str = "invalid_time_window") -> datetime:
    if not isinstance(raw, str):
        raise ReportQueryError(code, f"{field} must be an ISO 8601 timestamp string")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise ReportQueryError(code, f"{field} is not a valid ISO 8601 timestamp: {raw!r}")
    if moment.tzinfo is None:
        raise ReportQueryError(code, f"{field} must carry a timezone offset: {raw!r}")
    return moment.astimezone(timezone.utc)


def parse_time_window(start: str | None, end: str | None) -> tuple[str, str]:
    """校验并规范化起止时刻为 UTC ISO 字符串（与 created_at 的存储格式可比较）。

    窗口左闭右开 [start, end)：两个时刻都必须带时区，且 start 严格早于 end。
    """
    if start is None or end is None:
        raise ReportQueryError(
            "invalid_time_window", "start and end are required for the first page of a report"
        )
    begin = _parse_instant(start, field="start")
    finish = _parse_instant(end, field="end")
    if begin >= finish:
        raise ReportQueryError(
            "invalid_time_window", f"start must be earlier than end: {start!r} >= {end!r}"
        )
    return begin.isoformat(), finish.isoformat()


def validate_page_size(page_size: int | None) -> int:
    """页大小缺省为 DEFAULT_PAGE_SIZE；超出 [1, MAX_PAGE_SIZE] 明确拒绝。"""
    if page_size is None:
        return DEFAULT_PAGE_SIZE
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ReportQueryError(
            "invalid_page_size", f"page_size must be between 1 and {MAX_PAGE_SIZE}: {page_size}"
        )
    return page_size


@dataclass(frozen=True)
class ReportCursor:
    """翻页游标内容：首次查询固定的筛选条件 + 快照边界 + 键集分页位置。"""

    start: str                 # 规范化后的窗口起点（UTC ISO，含）
    end: str                   # 规范化后的窗口终点（UTC ISO，不含）
    policy_revision: int | None
    snapshot_seq: int          # 首次查询时“已提交判定”的内部序号上界
    after_seq: int             # 上一页最后一条的内部序号（本页从其之后继续）


def encode_cursor(cursor: ReportCursor) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "start": cursor.start,
        "end": cursor.end,
        "policy_revision": cursor.policy_revision,
        "snapshot": cursor.snapshot_seq,
        "after": cursor.after_seq,
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _cursor_int(payload: dict, field: str, *, minimum: int) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ReportQueryError("invalid_cursor", f"cursor field {field!r} is invalid")
    return value


def decode_cursor(raw: str) -> ReportCursor:
    """解码并完整校验游标；任何不一致都明确拒绝（invalid_cursor）。"""
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except Exception:
        raise ReportQueryError("invalid_cursor", "cursor is not decodable")
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ReportQueryError("invalid_cursor", "cursor has an unsupported shape or version")

    start = _parse_instant(payload.get("start"), field="start", code="invalid_cursor").isoformat()
    end = _parse_instant(payload.get("end"), field="end", code="invalid_cursor").isoformat()
    if start >= end:
        raise ReportQueryError("invalid_cursor", "cursor time window is empty")

    policy_revision = payload.get("policy_revision")
    if policy_revision is not None and (
        not isinstance(policy_revision, int) or isinstance(policy_revision, bool) or policy_revision < 0
    ):
        raise ReportQueryError("invalid_cursor", "cursor field 'policy_revision' is invalid")

    return ReportCursor(
        start=start,
        end=end,
        policy_revision=policy_revision,
        snapshot_seq=_cursor_int(payload, "snapshot", minimum=0),
        after_seq=_cursor_int(payload, "after", minimum=0),
    )
