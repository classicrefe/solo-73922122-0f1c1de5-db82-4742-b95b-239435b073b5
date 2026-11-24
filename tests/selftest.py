"""自测：算法规则 + Flask 端到端主流程。

以隔离模式运行：python -I tests/selftest.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.engine import evaluate_segment, solve, split_segments

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def d(order_no, start, end, shares):
    return {"id": abs(hash(order_no)) % 100000, "order_no": order_no,
            "start_date": start, "end_date": end, "shares": shares,
            "version": 1}


# ---------------- 算法层 ----------------

def test_engine() -> None:
    print("== 算法层 ==")

    # 1. 同权利人同日同值只计一次，合计 10000 可通过
    r = evaluate_segment(
        [d("A", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 4000}),
         d("B", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 4000})],
        "2026-01-01", "2026-02-01",
    )
    check("同值去重后合计 10000", r["ok"] and r["shares"] == {"甲": 6000, "乙": 4000})

    # 2. 同一权利人异值 -> 无法供数
    r = evaluate_segment(
        [d("A", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 4000}),
         d("B", "2026-01-01", "2026-02-01", {"甲": 5000, "乙": 5000})],
        "2026-01-01", "2026-02-01",
    )
    check("同段异值判失败", (not r["ok"]) and "异值" in r["reason"])

    # 3. 去重后合计不等于 10000
    r = evaluate_segment(
        [d("A", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 3000})],
        "2026-01-01", "2026-02-01",
    )
    check("合计 9000 判失败", not r["ok"] and "10000" in r["reason"])

    # 4. 无覆盖
    r = evaluate_segment([], "2026-01-01", "2026-02-01")
    check("无覆盖判失败", not r["ok"] and "覆盖" in r["reason"])

    # 5. 最少退单：3 单中 1 单异值，应恰好退回该单
    decls = [
        d("D001", "2026-01-01", "2026-04-01", {"甲": 6000, "乙": 4000}),
        d("D002", "2026-04-01", "2026-07-01", {"甲": 7000, "乙": 3000}),
        d("D003", "2026-03-01", "2026-04-01", {"甲": 5000, "乙": 5000}),
        d("D004", "2026-02-01", "2026-03-01", {"甲": 6000, "乙": 4000}),
    ]
    sol = solve(decls, "2026-01-01", "2026-07-01")
    check("最少退单恰好 1 单 D003",
          sol.feasible and sol.returned == ["D003"], str(sol.returned))
    check("保留 3 单", sol.retain == ["D001", "D002", "D004"], str(sol.retain))
    check("分段按区间切分（无逐日展开）", len(sol.segments) == 4,
          f"segments={len(sol.segments)}")
    check("每段合计 10000",
          all(sum(s["shares"].values()) == 10000 for s in sol.segments))

    # 6. 覆盖空档 -> 无解
    gap = [
        d("G1", "2026-01-01", "2026-04-01", {"甲": 8000, "乙": 2000}),
        d("G2", "2026-05-01", "2026-07-01", {"甲": 5500, "乙": 4500}),
    ]
    sol = solve(gap, "2026-01-01", "2026-07-01")
    check("空档场景无解", not sol.feasible)
    check("无解诊断含无覆盖段", any("覆盖" in (s["reason"] or "") for s in sol.segments))

    # 7. 必须退单且多个同规模方案时，单号字典序最小的保留集
    tie = [
        d("X1", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 4000}),
        d("X2", "2026-01-01", "2026-02-01", {"甲": 7000, "乙": 3000}),
        d("X3", "2026-01-01", "2026-02-01", {"甲": 8000, "乙": 2000}),
    ]
    # 三单两两异值，最多保留一单，应保留字典序最小的 X1，退回 X2、X3
    sol = solve(tie, "2026-01-01", "2026-02-01")
    check("平手取字典序最小保留集", sol.feasible and sol.retain == ["X1"]
          and sol.returned == ["X2", "X3"], str((sol.retain, sol.returned)))

    # 8. 部分相交的申报也会产生边界分段
    segs = split_segments("2026-01-01", "2026-07-01",
                          [d("P", "2025-12-01", "2026-02-01", {"甲": 10000})])
    check("越界相交申报按结算期截断", segs[0] == ("2026-01-01", "2026-02-01"))


# ---------------- Web 端到端 ----------------

def make_app(tmpdir):
    app = create_app({"TESTING": True, "DATABASE": os.path.join(tmpdir, "test.db")})
    with app.app_context():
        db.init_db()
        from app.seed import seed_demo_data
        seed_demo_data()
    return app


def form_post(client, path, data):
    return client.post(path, data=data, follow_redirects=False)


def test_web() -> None:
    print("== Web 端到端 ==")
    tmpdir = tempfile.mkdtemp()
    app = make_app(tmpdir)
    client = app.test_client()

    # 首页
    resp = client.get("/")
    check("首页 200 且列出演示录音", resp.status_code == 200 and "深夜电台" in resp.text)

    with app.app_context():
        recordings = {r["title"]: r["id"] for r in db.list_recordings()}
        rid1 = recordings["深夜电台（可核定）"]
        rid2 = recordings["清晨钟声（无解·回退上一版）"]
        rid3 = recordings["午后回廊（版本递增演示）"]

    # 录音详情
    resp = client.get(f"/recordings/{rid1}")
    check("录音详情含 4 单演示申报", resp.status_code == 200 and "D003" in resp.text)

    # 预览 R1
    resp = form_post(client, f"/recordings/{rid1}/preview",
                     {"period_start": "2026-01-01", "period_end": "2026-07-01"})
    check("预览提交 302", resp.status_code == 302)
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    resp = client.get(f"/previews/{token}")
    check("预览显示最少退单 D003", "D003" in resp.text and "退回 1 单" in resp.text)
    check("预览显示分段份额", "2026-04-01" in resp.text)

    # 确认发布
    resp = form_post(client, f"/previews/{token}/confirm", {})
    check("确认发布 302 到版本页", resp.status_code == 302 and "/versions/" in resp.headers["Location"])
    version_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    resp = client.get(f"/versions/{version_id}")
    check("版本页含保留/退单与份额", "D003" in resp.text and "10000" in resp.text)
    resp = client.get(f"/api/versions/{version_id}")
    data = resp.get_json()
    check("JSON 取数结构完整",
          resp.status_code == 200 and data["returned_orders"] == ["D003"]
          and all(sum(s["shares"].values()) == 10000 for s in data["segments"]))

    # 原申报仍留档
    with app.app_context():
        check("发布后原申报保留", len(db.list_declarations(rid1)) == 4)

    # 预览期间更正申报 -> 旧预览签名失效，确认被拒
    with app.app_context():
        d001 = db.find_declaration(rid1, "D001")
        edit_id = d001["id"]
    form_post(client, f"/declarations/{edit_id}/edit", {
        "start_date": "2026-01-01", "end_date": "2026-04-01",
        "holder_name": ["甲", "乙"], "holder_value": ["6500", "3500"],
    })
    resp = form_post(client, f"/previews/{token}/confirm", {})
    check("资料不一致时拒绝确认并重算提示",
          resp.status_code == 302)
    with client.session_transaction() as sess:
        flashes = [f[1] for f in sess.get("_flashes", [])]
    check("重算提示文案正确", any("不一致" in m for m in flashes), str(flashes))
    with app.app_context():
        check("更正后版本号递增且旧版留档",
              db.get_declaration(edit_id)["version"] == 2
              and len(db.list_history(edit_id)) == 1)
    resp = client.get(f"/previews/{token}")
    check("预览页标记资料不一致", "资料不一致" in resp.text)

    # 重新预览并发布 v2
    resp = form_post(client, f"/recordings/{rid1}/preview",
                     {"period_start": "2026-01-01", "period_end": "2026-07-01"})
    token2 = resp.headers["Location"].rsplit("/", 1)[-1]
    resp = form_post(client, f"/previews/{token2}/confirm", {})
    vid2 = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        check("同结算期版本号递增为 v2",
              db.get_version(vid2)["version_no"] == 2)

    # 模拟写入失败：不产生新版，上一版仍可取
    with app.app_context():
        before = len(db.list_versions(rid1))
    resp = form_post(client, f"/previews/{token2}/confirm", {"simulate_fail": "1"})
    check("写入失败 302 回预览页", resp.status_code == 302 and "/previews/" in resp.headers["Location"])
    with client.session_transaction() as sess:
        flashes = [f[1] for f in sess.get("_flashes", [])]
    check("写入失败提示回退上一版", any("写入失败" in m for m in flashes))
    with app.app_context():
        check("失败后未新增版本", len(db.list_versions(rid1)) == before)
    resp = client.get(f"/versions/{version_id}")
    check("上一版仍可正常取数", resp.status_code == 200 and "D003" in resp.text)

    # 无解录音
    resp = form_post(client, f"/recordings/{rid2}/preview",
                     {"period_start": "2026-01-01", "period_end": "2026-07-01"})
    token3 = resp.headers["Location"].rsplit("/", 1)[-1]
    resp = client.get(f"/previews/{token3}")
    check("无解预览展示原因与分段诊断", "无解" in resp.text and "无申报覆盖" in resp.text)
    resp = form_post(client, f"/previews/{token3}/confirm", {})
    check("无解禁止发布", resp.status_code == 302)
    with app.app_context():
        versions_r2 = db.list_versions(rid2)
        check("预置历史版可取数", len(versions_r2) >= 1)
        old_vid = versions_r2[0]["id"]
    check("上一版 JSON 可取", client.get(f"/api/versions/{old_vid}").status_code == 200)

    # 录入校验（非法输入 200 重渲染，flash 已在响应页面中展示）
    resp = form_post(client, f"/recordings/{rid3}/declarations/new", {
        "order_no": "BAD1", "start_date": "2026-01-01", "end_date": "2026-01-01",
        "holder_name": ["丙"], "holder_value": ["10000"],
    })
    check("起日=止日被拒", resp.status_code == 200 and "起日早于止日" in resp.text)

    resp = form_post(client, f"/recordings/{rid3}/declarations/new", {
        "order_no": "BAD2", "start_date": "2026-01-01", "end_date": "2026-02-01",
        "holder_name": ["丙"], "holder_value": ["9999"],
    })
    check("单内合计非 10000 被拒", resp.status_code == 200 and "10000" in resp.text)

    # 每首最多 12 单：补齐到上限再试第 13 单
    with app.app_context():
        existing = len(db.list_declarations(rid3))
    for i in range(existing, 12):
        form_post(client, f"/recordings/{rid3}/declarations/new", {
            "order_no": f"L{i:02d}", "start_date": "2026-01-01", "end_date": "2026-02-01",
            "holder_name": ["丙"], "holder_value": ["10000"],
        })
    resp = form_post(client, f"/recordings/{rid3}/declarations/new", {
        "order_no": "L13", "start_date": "2026-01-01", "end_date": "2026-02-01",
        "holder_name": ["丙"], "holder_value": ["10000"],
    })
    check("第 13 单被拒（上限 12）", resp.status_code == 200 and "12" in resp.text)

    # 版本列表页
    resp = client.get("/versions")
    check("核定版列表页 200", resp.status_code == 200 and "清晨钟声" in resp.text)


# ---------------- 验收：解析 / 净额区间 / 核对 ----------------

def test_acceptance_units() -> None:
    print("== 验收算法层 ==")
    from app import acceptance

    csv_text = (
        "回执号,类型,权利人,万分比,起日,止日\n"
        "R1,入仓,甲,6000,2026-01-01,2026-04-01\n"
        "R1,入仓,乙,4000,2026-01-01,2026-04-01\n"
    ).encode()
    receipt_no, lines = acceptance.parse_receipt_csv(csv_text, None)
    check("CSV 表头解析含回执号", receipt_no == "R1" and len(lines) == 2)
    check("入仓解析为 in", lines[0]["action"] == "in" and lines[0]["value"] == 6000)

    receipt_no2, lines2 = acceptance.parse_receipt_csv(
        "类型,权利人,万分比,起日,止日\n冲销,甲,6000,2026-01-01,2026-04-01\n"
        .encode(), "R2",
    )
    check("无表头回执号列时用表单回执号", receipt_no2 == "R2"
          and lines2[0]["action"] == "reverse")

    check("同号同内容哈希稳定（行序/空白无关）",
          acceptance.content_hash(lines) == acceptance.content_hash(list(reversed(lines))))
    check("同号异内容哈希不同",
          acceptance.content_hash(lines) != acceptance.content_hash(lines2))

    bad_cases = [
        ("回执号,类型\nR3,入仓\n", "表头缺少"),
        ("类型,权利人,万分比,起日,止日\n入仓,甲,6000.5,2026-01-01,2026-04-01\n", "整数"),
        ("类型,权利人,万分比,起日,止日\n转仓,甲,6000,2026-01-01,2026-04-01\n", "入仓"),
        ("类型,权利人,万分比,起日,止日\n入仓,甲,6000,2026-04-01,2026-04-01\n", "起日"),
        ("类型,权利人,万分比,起日,止日\n入仓,甲,6000,2026-13-01,2026-04-01\n", "YYYY-MM-DD"),
        ("类型,权利人,万分比,起日,止日\n入仓,,6000,2026-01-01,2026-04-01\n", "权利人"),
    ]
    for raw, expected in bad_cases:
        try:
            acceptance.parse_receipt_csv(raw.encode("utf-8"), "R9")
            ok = False
        except acceptance.ReceiptError as exc:
            ok = expected in str(exc)
        check(f"非法回执被拒（{expected}）", ok)

    # 净额区间：同值入仓 + 冲销相抵，异值互不影响
    r = acceptance.compute_check(
        "2026-01-01", "2026-02-01",
        [{"start_date": "2026-01-01", "end_date": "2026-02-01",
          "shares": {"甲": 6000, "乙": 4000}}],
        [{"holder": "甲", "share_value": 6000, "start_date": "2026-01-01",
          "end_date": "2026-02-01", "net_count": 1},
         {"holder": "乙", "share_value": 4000, "start_date": "2026-01-01",
          "end_date": "2026-02-01", "net_count": 1}],
        1,
    )
    check("净额与核定完全一致时通过", r["passed"] and r["mismatch_count"] == 0)
    check("核对按区间列出且不逐日展开", r["interval_count"] == 1)

    r = acceptance.compute_check(
        "2026-01-01", "2026-02-01",
        [{"start_date": "2026-01-01", "end_date": "2026-02-01",
          "shares": {"甲": 6000, "乙": 4000}}],
        [{"holder": "甲", "share_value": 6000, "start_date": "2026-01-01",
          "end_date": "2026-02-01", "net_count": 1},
         {"holder": "甲", "share_value": 5000, "start_date": "2026-01-01",
          "end_date": "2026-02-01", "net_count": 1},
         {"holder": "乙", "share_value": 4000, "start_date": "2026-01-01",
          "end_date": "2026-02-01", "net_count": 1}],
        2,
    )
    check("核定无此值时多出净条数判差异",
          not r["passed"] and any(d["kind"] == "unexpected" for d in r["intervals"][0]["diffs"]))

    r = acceptance.compute_check(
        "2026-01-01", "2026-02-01",
        [{"start_date": "2026-01-01", "end_date": "2026-02-01",
          "shares": {"甲": 6000, "乙": 4000}}],
        [{"holder": "甲", "share_value": 6000, "start_date": "2026-01-01",
          "end_date": "2026-02-01", "net_count": 1}],
        1,
    )
    check("核定值缺仓判差异",
          not r["passed"] and any(d["kind"] == "missing" for d in r["intervals"][0]["diffs"]))

    # 早于核定分段的入仓不应影响该区间；区间边界按净额表切分
    r = acceptance.compute_check(
        "2026-01-01", "2026-03-01",
        [{"start_date": "2026-02-01", "end_date": "2026-03-01",
          "shares": {"甲": 10000}}],
        [{"holder": "甲", "share_value": 10000, "start_date": "2026-02-01",
          "end_date": "2026-03-01", "net_count": 1},
         {"holder": "甲", "share_value": 10000, "start_date": "2026-01-01",
          "end_date": "2026-02-01", "net_count": 1}],
        2,
    )
    check("净额区间边界与核定分段边界合并切段",
          not r["passed"] and len(r["intervals"]) == 2,
          str(r["intervals"]))


def test_acceptance_web() -> None:
    print("== 验收 Web 端到端 ==")
    from app.engine import solve

    tmpdir = tempfile.mkdtemp()
    app = make_app(tmpdir)
    client = app.test_client()
    with app.app_context():
        rid = next(r["id"] for r in db.list_recordings() if "深夜电台" in r["title"])
        decls = db.list_declarations(rid)
        sig = db.input_signature(decls, "2026-01-01", "2026-07-01")
        sol = solve(decls, "2026-01-01", "2026-07-01")
        payload = {
            "feasible": sol.feasible, "retain": sol.retain, "returned": sol.returned,
            "segments": sol.segments, "checked_order_count": sol.checked_order_count,
            "reason": sol.reason,
        }
        vid = db.publish_version(rid, "2026-01-01", "2026-07-01", payload, sig)
        version = db.get_version(vid)

    segs = json.loads(version["segments_json"])

    def upload(receipt_no, rows, fail=False, filename="r.csv", use_client=None):
        body = "回执号,类型,权利人,万分比,起日,止日\n" + "".join(
            f"{receipt_no},{a},{h},{v},{s},{e}\n" for a, h, v, s, e in rows
        )
        data = {
            "receipt_no": receipt_no,
            "csv_file": (io.BytesIO(body.encode("utf-8")), filename),
        }
        if fail:
            data["simulate_fail"] = "1"
        return (use_client or client).post(
            f"/versions/{vid}/acceptance", data=data,
            content_type="multipart/form-data", follow_redirects=True,
        )

    api = lambda: client.get(f"/api/versions/{vid}/acceptance").get_json()

    resp = client.get(f"/versions/{vid}/acceptance")
    check("验收入口页 200 且列出区间与差异", resp.status_code == 200
          and "验收未通过" in resp.text and "应有净条数 1，实际 0" in resp.text)
    check("无回执时核对结果为空仓差异",
          api()["passed"] is False and api()["receipt_count"] == 0)

    # 空仓时缺仓 -> 未通过；入仓全部核定值 -> 通过
    rows_in = [(s["start_date"], s["end_date"], h, v)
               for s in segs for h, v in s["shares"].items()]
    resp = upload("R-IN-1", [("入仓", h, v, s, e) for s, e, h, v in rows_in])
    check("入仓后验收通过", resp.status_code == 200 and "验收通过" in resp.text)
    check("验收结果 JSON 明确通过", api()["passed"] is True)

    # 同号同内容重复上传只计一次
    resp = upload("R-IN-1", [("入仓", h, v, s, e) for s, e, h, v in rows_in])
    check("同号同内容重复不计",
          "未重复计入" in resp.text and "验收通过" in resp.text)

    # 同号异内容报冲突
    resp = upload("R-IN-1", [("入仓", rows_in[0][2], rows_in[0][3],
                              rows_in[0][0], rows_in[0][1])], filename="x.csv")
    check("同号异内容报冲突", "回执冲突" in resp.text)

    # 冲销先于入仓到达：先冲后入，最终净额仍通过
    first = segs[0]
    first_rows = [(h, v, first["start_date"], first["end_date"])
                  for h, v in first["shares"].items()]
    rev_rows = [("冲销", h, v, s, e) for h, v, s, e in first_rows]
    resp = upload("R-REV-1", rev_rows)
    check("冲销先到造成差异并收回通过标记",
          "验收未通过" in resp.text and "验收通过" not in resp.text)
    resp = upload("R-IN-LATE", [("入仓", h, v, s, e) for h, v, s, e in first_rows])
    check("后到入仓与原冲销相抵后恢复通过", "验收通过" in resp.text)

    # 不同值不互抵：给甲另记一个 5000 的入仓
    resp = upload("R-DIFF", [("入仓", "甲", 5000,
                             first["start_date"], first["end_date"])])
    check("异值入仓不抵消原值并判差异",
          "验收未通过" in resp.text and "甲=5000 净条数 1" in resp.text)
    check("后到回执造成差异时 JSON 也收回通过", api()["passed"] is False)

    # 差异后同号同内容仍只计一次（不会因重复计而进一步偏离）
    resp = upload("R-REV-1", rev_rows)
    check("同号同内容在差异状态下仍不重复计",
          "未重复计入" in resp.text and "验收未通过" in resp.text)

    # 撤掉异值入仓后恢复通过
    resp = upload("R-DIFF-UNDO", [("冲销", "甲", 5000,
                                   first["start_date"], first["end_date"])])
    check("撤销异值后恢复通过", "验收通过" in resp.text)

    # 写入失败：回执与核对同事务回滚，原结果仍可查；同号重传成功
    with app.app_context():
        receipts_before = len(db.list_receipts(vid))
    before = api()
    resp = upload("R-FAIL", [("入仓", "甲", 5500,
                             first["start_date"], first["end_date"])], fail=True)
    check("模拟写入失败提示可重传", "写入失败" in resp.text)
    with app.app_context():
        check("写入失败未新增回执", len(db.list_receipts(vid)) == receipts_before)
    check("失败后原核对结果照常可查", api() == before)
    resp = upload("R-FAIL", [("入仓", "甲", 5500,
                             first["start_date"], first["end_date"])])
    check("同一回执号重传成功", "已入账并完成核对" in resp.text)
    with app.app_context():
        check("重传后回执入账", any(
            r["receipt_no"] == "R-FAIL" for r in db.list_receipts(vid)))

    # 空文件被拒
    resp = upload("R-EMPTY", [], filename="empty.csv")
    check("空文件被拒", "解析失败" in resp.text)

    # 并发入账：多线程上传不同回执，全部计入且核对一致
    import threading
    last = segs[-1]
    target_holder, target_value = next(iter(last["shares"].items()))
    results = []

    def worker(i):
        try:
            c2 = app.test_client()
            r = upload(
                f"RC-{i}",
                [("入仓", target_holder, target_value,
                  last["start_date"], last["end_date"])],
                use_client=c2,
            )
            results.append(r.status_code)
        except Exception as exc:  # noqa: BLE001
            results.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发上传全部成功", len(results) == 6 and all(s == 200 for s in results),
          str(results))
    with app.app_context():
        intervals = db.list_warehouse_intervals(vid)
    base_net = 1  # 最初 R-IN-1 入仓
    # 前面 R-FAIL 给第一段加过 5500，与本段无关
    net = sum(it["net_count"] for it in intervals
              if it["holder"] == target_holder
              and it["share_value"] == target_value
              and it["start_date"] <= last["start_date"]
              and it["end_date"] >= last["end_date"])
    check("并发入账不漏计", net == base_net + 6, f"net={net}")
    resp = client.get(f"/versions/{vid}/acceptance")
    check("并发后差异按区间列出", "验收未通过" in resp.text
          and f"核定值 {target_holder}={target_value} 应有净条数 1，实际 {base_net + 6}"
          in resp.text)

    # 发布新核定版：仓数据按结算期沿用，新版本页立即给出最新核对结果
    resp = form_post(client, f"/recordings/{rid}/preview",
                     {"period_start": "2026-01-01", "period_end": "2026-07-01"})
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    resp = form_post(client, f"/previews/{token}/confirm", {})
    version2_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    resp = client.get(f"/versions/{version2_id}")
    check("新版本页沿用仓数据并显示未通过",
          resp.status_code == 200 and "未通过" in resp.text
          and "已入账回执" in resp.text)
    data = client.get(f"/api/versions/{version2_id}/acceptance").get_json()
    # 回执：R-IN-1, R-REV-1, R-IN-LATE, R-DIFF, R-DIFF-UNDO, R-FAIL, RC-0..5
    check("新版本验收继承回执数", data["receipt_count"] == 12,
          str(data["receipt_count"]))
    check("新旧版本同结算期同步收回通过标记",
          client.get(f"/api/versions/{vid}/acceptance").get_json()["passed"] is False)
    check("新版本可继续上传回执", client.get(
        f"/versions/{version2_id}/acceptance").status_code == 200)

    # 解析期间可提交其他回执：一份回执解析被拖慢时，另一路仍能立刻入账
    import threading
    import time

    from app import acceptance as acceptance_mod

    real_parse = acceptance_mod.parse_receipt_csv
    slow_rows = [("入仓", "乙", 3000, last["start_date"], last["end_date"])]
    fast_rows = [("冲销", "乙", 3000, last["start_date"], last["end_date"])]

    def slow_parse(raw, form_no):
        time.sleep(0.4)
        return real_parse(raw, form_no)

    acceptance_mod.parse_receipt_csv = slow_parse
    try:
        results_slow = []
        t = threading.Thread(
            target=lambda: results_slow.append(
                upload("R-SLOW", slow_rows, use_client=app.test_client()).status_code
            )
        )
        t.start()
        fast_status = upload("R-FAST", fast_rows,
                             use_client=app.test_client()).status_code
        t.join()
    finally:
        acceptance_mod.parse_receipt_csv = real_parse
    check("解析期间其他回执可直接入账",
          fast_status == 200 and results_slow == [200],
          f"fast={fast_status} slow={results_slow}")

    # 同号同内容并发上传：只入账一次，净额不多计
    with app.app_context():
        net_before = sum(it["net_count"] for it in db.list_warehouse_intervals(vid)
                         if it["holder"] == target_holder
                         and it["share_value"] == target_value)
    same_rows = [("入仓", "乙", 2000, last["start_date"], last["end_date"])]
    statuses = []

    def same_worker():
        try:
            r = upload("R-SAME", same_rows, use_client=app.test_client())
            statuses.append(r.status_code)
        except Exception as exc:  # noqa: BLE001
            statuses.append(repr(exc))

    same_threads = [threading.Thread(target=same_worker) for _ in range(5)]
    for th in same_threads:
        th.start()
    for th in same_threads:
        th.join()
    with app.app_context():
        receipts = [r for r in db.list_receipts(vid) if r["receipt_no"] == "R-SAME"]
        net_after = sum(it["net_count"] for it in db.list_warehouse_intervals(vid)
                        if it["holder"] == "乙" and it["share_value"] == 2000
                        and it["start_date"] <= last["start_date"]
                        and it["end_date"] >= last["end_date"])
    check("同号同内容并发只入账一次",
          len(receipts) == 1 and net_after == 1 and all(s == 200 for s in statuses),
          f"receipts={len(receipts)} net={net_after} statuses={statuses}")
    with app.app_context():
        net_same = sum(it["net_count"] for it in db.list_warehouse_intervals(vid)
                       if it["holder"] == target_holder
                       and it["share_value"] == target_value
                       and it["start_date"] <= last["start_date"]
                       and it["end_date"] >= last["end_date"])
    check("并发重复不影响原净额", net_same == net_before,
          f"before={net_before} after={net_same}")


if __name__ == "__main__":
    test_engine()
    test_web()
    test_acceptance_units()
    test_acceptance_web()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)
