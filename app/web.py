"""Web 路由：申报录入/更正、预览核定、确认发布、版本取数。"""
from __future__ import annotations

import json
import secrets
import sqlite3

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for,
)

from . import db
from .engine import parse_iso_date, solve

bp = Blueprint("web", __name__)


# ---------- 校验 ----------

def _parse_shares_form(form) -> tuple[dict[str, int] | None, str | None]:
    names = form.getlist("holder_name")
    values = form.getlist("holder_value")
    shares: dict[str, int] = {}
    for raw_name, raw_value in zip(names, values):
        name = raw_name.strip()
        if not name:
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return None, f"权利人 {name} 的万分比必须是整数"
        if not 0 <= value <= 10000:
            return None, f"权利人 {name} 的万分比须在 0~10000 之间"
        if name in shares:
            return None, f"权利人 {name} 重复填写"
        shares[name] = value
    if not shares:
        return None, "至少填写一个权利人"
    if sum(shares.values()) != 10000:
        return None, f"单内各权利人万分比之和须为 10000，当前为 {sum(shares.values())}"
    return shares, None


def _validate_period(start_raw: str, end_raw: str) -> tuple[str, str] | tuple[None, str]:
    try:
        start = parse_iso_date(start_raw)
        end = parse_iso_date(end_raw)
    except (ValueError, TypeError):
        return None, "日期格式应为 YYYY-MM-DD"
    if start >= end:
        return None, "结算/申报区间须满足起日早于止日（含起日、不含止日）"
    return start.isoformat(), end.isoformat()


def _load_recording_or_404(recording_id: str):
    recording = db.get_recording(int(recording_id))
    if recording is None:
        abort(404)
    return recording


# ---------- 首页与申报 ----------

@bp.route("/")
def index():
    return render_template("index.html", recordings=db.list_recordings())


@bp.route("/recordings/create", methods=["POST"])
def create_recording_route():
    title = request.form.get("title", "").strip()
    if not title:
        flash("录音名称不能为空", "error")
        return redirect(url_for("web.index"))
    existing = next((r for r in db.list_recordings() if r["title"] == title), None)
    if existing:
        flash("该录音已存在", "error")
        return redirect(url_for("web.recording_detail", recording_id=existing["id"]))
    recording_id = db.get_or_create_recording(title)
    return redirect(url_for("web.recording_detail", recording_id=recording_id))


@bp.route("/recordings/<int:recording_id>")
def recording_detail(recording_id: int):
    recording = _load_recording_or_404(recording_id)
    declarations = db.list_declarations(recording_id)
    versions = db.list_versions(recording_id)
    return render_template(
        "recording_detail.html",
        recording=recording, declarations=declarations, versions=versions,
    )


@bp.route("/recordings/<int:recording_id>/declarations/new", methods=["GET", "POST"])
def declaration_new(recording_id: int):
    recording = _load_recording_or_404(recording_id)
    if request.method == "POST":
        order_no = request.form.get("order_no", "").strip()
        if not order_no:
            flash("单号不能为空", "error")
            return render_template("declaration_form.html", recording=recording, decl=None)
        period = _validate_period(
            request.form.get("start_date", ""), request.form.get("end_date", "")
        )
        if period[0] is None:
            flash(period[1], "error")
            return render_template("declaration_form.html", recording=recording, decl=None)
        shares, err = _parse_shares_form(request.form)
        if err:
            flash(err, "error")
            return render_template("declaration_form.html", recording=recording, decl=None)
        if db.find_declaration(recording_id, order_no):
            flash(f"单号 {order_no} 已存在，请使用更正功能", "error")
            return render_template("declaration_form.html", recording=recording, decl=None)
        try:
            decl, _ = db.upsert_declaration(
                recording_id, order_no, period[0], period[1], shares
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("declaration_form.html", recording=recording, decl=None)
        flash(f"申报 {order_no} 已录入", "ok")
        return redirect(url_for("web.recording_detail", recording_id=recording_id))
    return render_template("declaration_form.html", recording=recording, decl=None)


@bp.route("/declarations/<int:declaration_id>/edit", methods=["GET", "POST"])
def declaration_edit(declaration_id: int):
    decl = db.get_declaration(declaration_id)
    if decl is None:
        abort(404)
    recording = _load_recording_or_404(decl["recording_id"])
    if request.method == "POST":
        period = _validate_period(
            request.form.get("start_date", ""), request.form.get("end_date", "")
        )
        if period[0] is None:
            flash(period[1], "error")
            return render_template(
                "declaration_form.html", recording=recording, decl=decl
            )
        shares, err = _parse_shares_form(request.form)
        if err:
            flash(err, "error")
            return render_template(
                "declaration_form.html", recording=recording, decl=decl
            )
        db.upsert_declaration(
            recording["id"], decl["order_no"], period[0], period[1], shares
        )
        flash(f"申报 {decl['order_no']} 已更正，旧版留档", "ok")
        return redirect(url_for("web.recording_detail", recording_id=recording["id"]))
    history = db.list_history(declaration_id)
    return render_template(
        "declaration_form.html", recording=recording, decl=decl, history=history
    )


# ---------- 预览与确认 ----------

@bp.route("/recordings/<int:recording_id>/preview", methods=["GET", "POST"])
def preview_new(recording_id: int):
    recording = _load_recording_or_404(recording_id)
    declarations = db.list_declarations(recording_id)
    if request.method == "POST":
        period = _validate_period(
            request.form.get("period_start", ""), request.form.get("period_end", "")
        )
        if period[0] is None:
            flash(period[1], "error")
            return render_template("preview_form.html", recording=recording,
                                   declarations=declarations)
        start, end = period
        result = solve(declarations, start, end)
        payload = {
            "feasible": result.feasible,
            "retain": result.retain,
            "returned": result.returned,
            "segments": result.segments,
            "checked_order_count": result.checked_order_count,
            "reason": result.reason,
        }
        signature = db.input_signature(declarations, start, end)
        token = secrets.token_urlsafe(16)
        db.save_preview(token, recording_id, start, end, payload, signature)
        return redirect(url_for("web.preview_show", token=token))
    return render_template("preview_form.html", recording=recording,
                           declarations=declarations)


@bp.route("/previews/<token>")
def preview_show(token: str):
    preview = db.get_preview(token)
    if preview is None:
        abort(404)
    recording = _load_recording_or_404(preview["recording_id"])
    current_signature = db.input_signature(
        db.list_declarations(recording["id"]),
        preview["period_start"], preview["period_end"],
    )
    stale = current_signature != preview["input_signature"]
    return render_template(
        "preview.html", preview=preview, recording=recording, stale=stale
    )


@bp.route("/previews/<token>/confirm", methods=["POST"])
def preview_confirm(token: str):
    preview = db.get_preview(token)
    if preview is None:
        abort(404)
    recording = _load_recording_or_404(preview["recording_id"])
    declarations = db.list_declarations(recording["id"])
    current_signature = db.input_signature(
        declarations, preview["period_start"], preview["period_end"]
    )
    if current_signature != preview["input_signature"]:
        flash("预览后该录音在结算期内的申报发生过新增/更正，资料不一致，请重新预览计算",
              "error")
        return redirect(url_for("web.preview_show", token=token))

    result = preview["result"]
    if not result["feasible"]:
        flash("当前预览无解，不能发布核定版；可更正申报后重新预览", "error")
        return redirect(url_for("web.preview_show", token=token))

    fail = request.form.get("simulate_fail") == "1"
    try:
        version_id = db.publish_version(
            recording["id"], preview["period_start"], preview["period_end"],
            result, current_signature, fail=fail,
        )
    except sqlite3.Error as exc:
        flash(f"核定版写入失败（{exc}），本次未发布；可取用同一录音、同一结算期的上一版",
              "error")
        return redirect(url_for("web.preview_show", token=token))

    flash("核定版已发布，原申报保留留档", "ok")
    return redirect(url_for("web.version_show", version_id=version_id))


# ---------- 核定版取数 ----------

@bp.route("/versions")
def versions_index():
    return render_template("versions.html", recordings=db.list_recordings())


@bp.route("/versions/<int:version_id>")
def version_show(version_id: int):
    version = db.get_version(version_id)
    if version is None:
        abort(404)
    recording = _load_recording_or_404(version["recording_id"])
    return render_template(
        "version_detail.html",
        version=version,
        recording=recording,
        retained=json.loads(version["retained_orders"]),
        returned=json.loads(version["returned_orders"]),
        segments=json.loads(version["segments_json"]),
    )


@bp.route("/api/versions/<int:version_id>")
def version_api(version_id: int):
    version = db.get_version(version_id)
    if version is None:
        abort(404)
    return jsonify({
        "id": version["id"],
        "recording_id": version["recording_id"],
        "period": [version["period_start"], version["period_end"]],
        "version_no": version["version_no"],
        "status": version["status"],
        "retained_orders": json.loads(version["retained_orders"]),
        "returned_orders": json.loads(version["returned_orders"]),
        "segments": json.loads(version["segments_json"]),
        "input_signature": version["input_signature"],
        "created_at": version["created_at"],
    })
