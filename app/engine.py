"""核定核心算法。

业务规则
--------
* 一首录音下每单申报声明一个 [start_date, end_date) 区间（含起日、不含止日）
  以及各权利人的整数万分比，单内各权利人比例之和必须为 10000。
* 申报只能整单保留 / 整单退回。
* 保留的单据中，同一权利人在同一天的同值只计一次。
* 同一天若同一权利人出现异值，或当天去重后的份额之和不等于 10000，
  则当天无法供数，对应保留方案非法。
* 结算期内任一天没有任何保留单据覆盖，同样无法供数。
* 按区间核算：用所有相关单据的起止日把结算期切成若干基本段，
  逐段（而非逐日展开）检查覆盖与份额即可。
* 目标：退回最少的单据，使整段结算期均可供数；
  保留数最大的方案即退回最少，平手时按单号字典序取最小保留集。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def split_segments(start: str, end: str, declarations: list[dict]) -> list[tuple[str, str]]:
    """用相关申报的起止日切分 [start, end) 基本段。"""
    bounds = {start, end}
    for decl in declarations:
        if decl["end_date"] > start and decl["start_date"] < end:
            bounds.add(max(start, decl["start_date"]))
            bounds.add(min(end, decl["end_date"]))
    ordered = sorted(bounds)
    return [(a, b) for a, b in zip(ordered, ordered[1:]) if a < b]


def covering(declarations: Iterable[dict], seg_start: str, seg_end: str) -> list[dict]:
    """覆盖整个基本段的申报（区间端点相接即可覆盖）。"""
    return [
        d
        for d in declarations
        if d["start_date"] <= seg_start and d["end_date"] >= seg_end
    ]


def evaluate_segment(declarations: list[dict], seg_start: str, seg_end: str) -> dict:
    """评估一个基本段。

    返回 {"ok": bool, "reason": str|None, "shares": {权利人: 万分比}}。
    """
    covers = covering(declarations, seg_start, seg_end)
    if not covers:
        return {"ok": False, "reason": "无申报覆盖", "shares": {}}

    merged: dict[str, int] = {}
    for decl in covers:
        for name, value in decl["shares"].items():
            if name in merged and merged[name] != value:
                return {
                    "ok": False,
                    "reason": f"权利人 {name} 同段异值（{merged[name]} 与 {value}）",
                    "shares": {},
                }
            merged[name] = value

    total = sum(merged.values())
    if total != 10000:
        return {
            "ok": False,
            "reason": f"去重后份额合计 {total}，不等于 10000",
            "shares": merged,
        }
    return {"ok": True, "reason": None, "shares": merged}


@dataclass
class Solution:
    feasible: bool
    retain: list[str]          # 保留单号
    returned: list[str]        # 退单单号
    segments: list[dict]       # 分段剩余份额
    checked_order_count: int   # 参与核定的相关申报数
    reason: str | None = None  # 无解时的诊断


def solve(declarations: list[dict], start: str, end: str) -> Solution:
    """求最少退单方案。

    declarations: [{"order_no","start_date","end_date","shares":{...}}]
    区间均为 ISO 字符串，半开区间 [start_date, end_date)。
    """
    relevant = sorted(
        (d for d in declarations if d["end_date"] > start and d["start_date"] < end),
        key=lambda d: d["order_no"],
    )
    segments = split_segments(start, end, relevant)

    if not segments:
        return Solution(False, [], [], [], 0, "结算区间为空或无相关申报")

    n = len(relevant)
    # 保留数从大到小枚举；同规模下索引组合按字典序，
    # 而索引按单号升序，故第一个可行解即“退单最少 + 单号字典序最小”。
    for size in range(n, 0, -1):
        for combo in combinations(n, size):
            kept = [relevant[i] for i in combo]
            segment_results: list[dict] = []
            ok = True
            for seg_start, seg_end in segments:
                result = evaluate_segment(kept, seg_start, seg_end)
                segment_results.append(
                    {
                        "start_date": seg_start,
                        "end_date": seg_end,
                        "ok": result["ok"],
                        "reason": result["reason"],
                        "shares": result["shares"],
                    }
                )
                if not result["ok"]:
                    ok = False
            if ok:
                kept_ids = {d["id"] for d in kept}
                returned = [d["order_no"] for d in relevant if d["id"] not in kept_ids]
                return Solution(
                    feasible=True,
                    retain=[d["order_no"] for d in kept],
                    returned=returned,
                    segments=segment_results,
                    checked_order_count=n,
                )

    # 无解：给出每个基本段在“全部申报保留”下的诊断，方便定位
    diagnostics = []
    for seg_start, seg_end in segments:
        result = evaluate_segment(relevant, seg_start, seg_end)
        diagnostics.append(
            {
                "start_date": seg_start,
                "end_date": seg_end,
                "ok": result["ok"],
                "reason": result["reason"],
                "shares": result["shares"],
            }
        )
    return Solution(
        feasible=False,
        retain=[],
        returned=[],
        segments=diagnostics,
        checked_order_count=n,
        reason="不存在可覆盖整个结算期且全部通过校验的保留组合",
    )


def combinations(n: int, k: int):
    """按字典序产出 range(n) 中取 k 个的全部组合，避免额外依赖。"""
    if k <= 0 or k > n:
        return
    indices = list(range(k))
    yield tuple(indices)
    while True:
        for i in range(k - 1, -1, -1):
            if indices[i] != i + n - k:
                break
        else:
            return
        indices[i] += 1
        for j in range(i + 1, k):
            indices[j] = indices[j - 1] + 1
        yield tuple(indices)
