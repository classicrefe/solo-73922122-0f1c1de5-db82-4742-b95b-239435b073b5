"""仓数据验收：回执 CSV 解析、按区间入账、核定版核对。

设计要点
--------
* 回执以「回执号 + 内容哈希」去重：同号同内容只入账一次，同号异内容报冲突。
* 入仓 / 冲销按「权利人 + 万分比 + 日期区间」记入一张**净额区间表**；
  同一权利人同日同值的入仓与冲销自然相抵，不同值互不影响。
* 新回执只拆分 / 更新其涉及的权利人、值及日期区间，不重放历史回执，
  也不逐日展开；核对时对「净额区间 + 核定分段边界」做扫描。
* 核定版中应有的「权利人 + 值」净条数必须为 1，其余值的净条数必须为 0。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date
from itertools import pairwise


class ReceiptError(ValueError):
    """回执 CSV 内容不合法（可在修正文件后重传）。"""


class ReceiptConflict(ValueError):
    """同一回执号已入账过不同内容。"""


# ---------- CSV 解析 ----------

_HEADER_ALIASES = {
    "receipt_no": {
        "回执号", "回执编号", "回执", "receipt", "receiptno", "receiptid",
    },
    "action": {
        "类型", "动作", "方向", "入仓/冲销", "入仓或冲销", "入仓冲销",
        "action", "type", "direction",
    },
    "holder": {
        "权利人", "权利人名", "权利人名称", "权利人姓名",
        "holder", "rightsholder", "name", "rights",
    },
    "value": {
        "万分比", "整数万分比", "份额", "比例", "value", "share",
        "sharevalue", "points", "basispoints",
    },
    "start": {"起日", "起始日", "开始日期", "开始日", "start", "startdate", "from"},
    "end": {"止日", "终止日", "结束日期", "结束日", "end", "enddate", "to"},
}

_IN_ACTIONS = {"入仓", "入库", "入账", "in", "load", "inbound", "stockin", "stock_in"}
_REVERSE_ACTIONS = {
    "冲销", "冲帐", "冲账", "reverse", "reversal", "offset", "writeoff",
    "write_off", "stockout", "stock_out",
}

_FIELD_ORDER = ("receipt_no", "action", "holder", "value", "start", "end")


def _norm_cell(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip().lower())


def _norm_header(value: str) -> str:
    text = value or ""
    # 兼容 (含)/(不含)、全角括号等写法
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\(.*?\)", "", text)
    return _norm_cell(text)


def _detect_delimiter(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            if "\t" in line and "," not in line:
                return "\t"
            if ";" in line and "," not in line:
                return ";"
            return ","
    return ","


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ReceiptError("文件编码无法识别，请使用 UTF-8 或 GB18030 编码的 CSV")


def _map_header(cells: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(cells):
        normalized = _norm_header(cell)
        for field, aliases in _HEADER_ALIASES.items():
            if normalized in aliases:
                if field in mapping:
                    raise ReceiptError(f"表头字段重复：{cell.strip()}")
                mapping[field] = idx
                break
    return mapping


def _looks_like_header(cells: list[str]) -> bool:
    mapping = _map_header(cells)
    return len(mapping) >= 2 and any(f in mapping for f in ("action", "holder", "value"))


def _parse_action(raw: str, line_no: int) -> str:
    value = _norm_cell(raw)
    if value in _IN_ACTIONS:
        return "in"
    if value in _REVERSE_ACTIONS:
        return "reverse"
    raise ReceiptError(f"第 {line_no} 行类型须为「入仓」或「冲销」，当前为 {raw.strip()!r}")


def parse_receipt_csv(raw: bytes, form_receipt_no: str | None = None) -> tuple[str, list[dict]]:
    """解析回执 CSV，返回 (回执号, 规范行列表)。

    每行必备：类型、权利人、整数万分比、起日、止日；
    回执号可在 CSV 每行填写，也可由表单提供；两者同时存在时须一致。
    """
    text = _decode(raw)
    if not text.strip():
        raise ReceiptError("CSV 文件为空")

    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    rows: list[tuple[int, list[str]]] = []
    header_mapping: dict[str, int] | None = None
    positional: tuple[str, ...] | None = None
    first = True

    for line_no, cells in enumerate(reader, start=1):
        if not any((c or "").strip() for c in cells):
            continue
        if first:
            first = False
            if _looks_like_header(cells):
                header_mapping = _map_header(cells)
                missing = [f for f in ("action", "holder", "value", "start", "end")
                           if f not in header_mapping]
                if missing:
                    raise ReceiptError(
                        "CSV 表头缺少字段：" + "、".join(missing) + "（需要类型/权利人/万分比/起日/止日）"
                    )
                continue
            # 无表头：6 列首列为回执号，5 列由表单/文件名提供回执号
            if len(cells) == 6:
                positional = _FIELD_ORDER
            elif len(cells) == 5:
                positional = ("action", "holder", "value", "start", "end")
            else:
                raise ReceiptError(
                    f"第 {line_no} 行列数为 {len(cells)}，无表头时须为 5 列"
                    "（类型,权利人,万分比,起日,止日）或 6 列（回执号在前）"
                )
        rows.append((line_no, cells))

    if not rows:
        raise ReceiptError("CSV 没有数据行")

    expected_cols = max((header_mapping or {}).values(), default=-1) + 1 if header_mapping else len(positional or ())
    if header_mapping:
        expected_cols = max(header_mapping.values()) + 1

    form_no = (form_receipt_no or "").strip()
    csv_receipts: set[str] = set()
    lines: list[dict] = []

    for line_no, cells in rows:
        if len(cells) < expected_cols:
            raise ReceiptError(
                f"第 {line_no} 行列数不足：需要 {expected_cols} 列，实际 {len(cells)} 列"
            )

        def field(name: str, cells: list[str] = cells) -> str:
            if header_mapping is not None:
                idx = header_mapping.get(name)
            else:
                idx = positional.index(name) if positional and name in positional else None
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].strip()

        receipt_cell = field("receipt_no")
        if receipt_cell:
            csv_receipts.add(receipt_cell)

        action = _parse_action(field("action"), line_no)
        holder = field("holder")
        if not holder:
            raise ReceiptError(f"第 {line_no} 行权利人为空")
        value_raw = field("value")
        try:
            value = int(value_raw)
        except (TypeError, ValueError):
            raise ReceiptError(
                f"第 {line_no} 行万分比须为整数，当前为 {value_raw!r}"
            ) from None
        if not 0 <= value <= 10000:
            raise ReceiptError(f"第 {line_no} 行万分比须在 0~10000 之间，当前为 {value}")
        start_raw = field("start")
        end_raw = field("end")
        try:
            start_date = date.fromisoformat(start_raw)
            end_date = date.fromisoformat(end_raw)
        except ValueError:
            raise ReceiptError(
                f"第 {line_no} 行日期须为 YYYY-MM-DD（当前起日 {start_raw!r}、止日 {end_raw!r}）"
            ) from None
        if start_date >= end_date:
            raise ReceiptError(f"第 {line_no} 行须满足起日早于止日（含起日、不含止日）")
        lines.append({
            "action": action,
            "holder": holder,
            "value": value,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        })

    if not lines:
        raise ReceiptError("CSV 没有有效数据行")
    if len(lines) > 5000:
        raise ReceiptError("单份回执行数过多（上限 5000 行）")

    if len(csv_receipts) > 1:
        raise ReceiptError("CSV 中不同行的回执号不一致：" + "、".join(sorted(csv_receipts)))
    csv_no = next(iter(csv_receipts)) if csv_receipts else ""
    if form_no and csv_no and form_no != csv_no:
        raise ReceiptError(f"表单回执号 {form_no} 与 CSV 回执号 {csv_no} 不一致")
    receipt_no = form_no or csv_no
    if not receipt_no:
        raise ReceiptError("缺少回执号：请在表单填写，或在 CSV 每行填写回执号")
    if len(receipt_no) > 128:
        raise ReceiptError("回执号过长（上限 128 字符）")
    return receipt_no, lines


def content_hash(lines: list[dict]) -> str:
    """同号同内容的判定哈希；行顺序、空白与大小写变化不影响结果。"""
    payload = [
        [line["action"], line["holder"], line["value"],
         line["start_date"], line["end_date"]]
        for line in lines
    ]
    payload.sort()
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------- 净额区间 ----------

def _merge_pieces(pieces: list[tuple[str, str, int]]) -> list[tuple[str, str, int]]:
    merged: list[tuple[str, str, int]] = []
    for start, end, count in pieces:
        if merged and merged[-1][1] == start and merged[-1][2] == count:
            merged[-1] = (merged[-1][0], end, count)
        else:
            merged.append((start, end, count))
    return merged


def apply_line_delta(
    conn,
    scope: tuple[int, str, str],
    holder: str,
    value: int,
    start: str,
    end: str,
    delta: int,
) -> None:
    """把一条回执行（入仓 +1 / 冲销 -1）叠加到该权利人的净额区间。

    scope = (recording_id, period_start, period_end)：仓数据按结算期留存，
    跨核定版本继续有效。只读取并重写与本次 [start, end) 相交的区间；
    其他权利人、其他日期的区间完全不动。结果保持
    「同一权利人同一值各区间互不重叠」。
    """
    rows = conn.execute(
        """SELECT id, start_date, end_date, net_count
           FROM warehouse_interval
           WHERE recording_id = ? AND period_start = ? AND period_end = ?
             AND holder = ? AND share_value = ?
             AND end_date > ? AND start_date < ?
           ORDER BY start_date""",
        (*scope, holder, value, start, end),
    ).fetchall()

    lo = min([start] + [r["start_date"] for r in rows])
    hi = max([end] + [r["end_date"] for r in rows])
    points = {lo, hi, start, end}
    for row in rows:
        points.add(max(lo, row["start_date"]))
        points.add(min(hi, row["end_date"]))
    ordered = sorted(points)

    pieces: list[tuple[str, str, int]] = []
    for a, b in pairwise(ordered):
        if a >= b:
            continue
        base = sum(
            row["net_count"]
            for row in rows
            if row["start_date"] <= a and row["end_date"] >= b
        )
        count = base + (delta if a >= start and b <= end else 0)
        if count:
            pieces.append((a, b, count))

    for row in rows:
        conn.execute("DELETE FROM warehouse_interval WHERE id = ?", (row["id"],))
    for a, b, count in _merge_pieces(pieces):
        conn.execute(
            """INSERT INTO warehouse_interval
               (recording_id, period_start, period_end,
                holder, share_value, start_date, end_date, net_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (*scope, holder, value, a, b, count),
        )


# ---------- 核对 ----------

def _segment_shares(segments: list[dict], start: str, end: str) -> dict[str, int] | None:
    for seg in segments:
        if seg["start_date"] <= start and seg["end_date"] >= end:
            return seg.get("shares") or {}
    return None


def compute_check(
    period_start: str,
    period_end: str,
    segments: list[dict],
    warehouse_intervals: list[dict],
    receipt_count: int,
) -> dict:
    """按「核定分段 + 净额区间」的合并边界逐段核对，不逐日展开。

    每段要求：
    * 核定值中的每个 (权利人, 万分比) 净条数为 1；
    * 其他 (权利人, 万分比) 净条数为 0。
    """
    boundaries = {period_start, period_end}
    active_segments: list[dict] = []
    for seg in segments:
        seg_start = max(seg["start_date"], period_start)
        seg_end = min(seg["end_date"], period_end)
        if seg_start < seg_end:
            active_segments.append({**seg, "start_date": seg_start, "end_date": seg_end})
            boundaries.add(seg_start)
            boundaries.add(seg_end)

    for interval in warehouse_intervals:
        interval_start = max(interval["start_date"], period_start)
        interval_end = min(interval["end_date"], period_end)
        if interval_start < interval_end:
            boundaries.add(interval_start)
            boundaries.add(interval_end)

    ordered = sorted(boundaries)
    result_intervals: list[dict] = []
    mismatch_count = 0

    for a, b in pairwise(ordered):
        if a >= b:
            continue
        approved = _segment_shares(active_segments, a, b)
        expected_pairs = {
            (holder, value) for holder, value in (approved or {}).items()
        }
        actual: dict[tuple[str, int], int] = {}
        for interval in warehouse_intervals:
            if (interval["end_date"] > a and interval["start_date"] < b
                    and interval["net_count"]):
                key = (interval["holder"], interval["share_value"])
                actual[key] = actual.get(key, 0) + interval["net_count"]

        diffs: list[dict] = []
        expected = approved or {}
        for holder in sorted(expected):
            value = expected[holder]
            count = actual.get((holder, value), 0)
            if count != 1:
                kind = "missing" if count == 0 else "count"
                diffs.append({
                    "kind": kind,
                    "holder": holder,
                    "value": value,
                    "expected": 1,
                    "actual": count,
                    "message": f"核定值 {holder}={value} 应有净条数 1，实际 {count}",
                })
        for (holder, value), count in sorted(actual.items()):
            if (holder, value) not in expected_pairs:
                diffs.append({
                    "kind": "unexpected",
                    "holder": holder,
                    "value": value,
                    "expected": 0,
                    "actual": count,
                    "message": f"核定无此项，实际多出 {holder}={value} 净条数 {count}",
                })

        mismatch_count += len(diffs)
        result_intervals.append({
            "start_date": a,
            "end_date": b,
            "approved": dict(sorted(expected.items())),
            "actual": [
                {"holder": h, "value": v, "net_count": n}
                for (h, v), n in sorted(actual.items())
            ],
            "diffs": diffs,
            "ok": not diffs,
        })

    return {
        "passed": mismatch_count == 0,
        "mismatch_count": mismatch_count,
        "receipt_count": receipt_count,
        "interval_count": len(result_intervals),
        "period_start": period_start,
        "period_end": period_end,
        "intervals": result_intervals,
    }


# ---------- 演示样例 CSV ----------

def build_sample_csv(version, role: str) -> tuple[str, str]:
    """按核定分段生成入仓/冲销/补仓样例 CSV，便于在页面直接演示。"""
    segments = json.loads(version["segments_json"])
    if role == "in":
        rows = [
            ("入仓", holder, value, seg["start_date"], seg["end_date"])
            for seg in segments
            for holder, value in (seg.get("shares") or {}).items()
        ]
    elif role in ("reverse", "restore"):
        first = segments[0]
        action = "冲销" if role == "reverse" else "入仓"
        rows = [
            (action, holder, value, first["start_date"], first["end_date"])
            for holder, value in (first.get("shares") or {}).items()
        ]
    else:
        raise ValueError("未知样例类型")

    receipt_no = f"SAMPLE-{role.upper()}-V{version['id']}"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["回执号", "类型", "权利人", "万分比", "起日", "止日"])
    for action, holder, value, start, end in rows:
        writer.writerow([receipt_no, action, holder, value, start, end])
    filename = f"sample_{role}_v{version['version_no']}.csv"
    return buffer.getvalue(), filename
