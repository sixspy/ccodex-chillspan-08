"""超温游标开闭。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import ExcursionCloseIn, ExcursionOpenIn
from validators import detect_excursion

router = APIRouter(tags=["excursions"])


def acquire_cursor_lock(conn, lock_key: str, holder: str) -> bool:
    """原子抢游标锁：INSERT OR IGNORE 一步落位，后到只记争用痕。

    先查再插会在双窗并发下双双看到空行，触发 UNIQUE 例外而非争用登记，
    因此抢占动作必须交给单条语句完成。
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO chamber_locks(lock_key,holder,acquired_at)
           VALUES (?,?,?)""",
        (lock_key, holder, now_iso()),
    )
    if cur.rowcount == 1:
        return True
    row = conn.execute(
        "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row and row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (
            lock_key,
            holder,
            "争用",
            f"持有者={row['holder'] if row else '未知'}",
            now_iso(),
        ),
    )
    return False


def _point_value(point: dict) -> float:
    return float(point.get("c", point.get("temp", 0)))


def build_fallback_proof(points: list, threshold: float) -> dict | None:
    """回落证明：峰值后须有回落采样，且最新采样已回到阈值以内。

    只认温谱原始采样，不认备注/口头确认；证明不足返回 None。
    """
    samples = [(i, p) for i, p in enumerate(points) if p]
    if not samples:
        return None
    exceed = [(i, p) for i, p in samples if _point_value(p) > threshold]
    if not exceed:
        return None
    last_i = exceed[-1][0]
    fallback = [
        p
        for i, p in samples
        if i > last_i and _point_value(p) <= threshold
    ]
    latest = samples[-1][1]
    if _point_value(latest) > threshold or not fallback:
        return None
    peak_i, peak_p = max(exceed, key=lambda x: _point_value(x[1]))
    return {
        "threshold": threshold,
        "proven": True,
        "peak_temp": _point_value(peak_p),
        "peak_at": peak_p.get("t"),
        "last_excursion_at": exceed[-1][1].get("t"),
        "fallback_samples": fallback,
        "latest_sample": latest,
    }


def _proof_state(points: list, threshold: float) -> dict:
    proof = build_fallback_proof(points, threshold)
    latest = points[-1] if points else None
    return {
        "fallback_proven": proof is not None,
        "latest_sample": latest,
        "fallback_reason": None
        if proof
        else "缺回落证明：峰值后无回落至阈值的采样或最新采样仍超阈",
    }


@router.get("/api/excursions")
def list_excursions(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT e.*, b.batch_code, b.temp_points FROM excursion_cursors e
               JOIN temp_batches b ON b.id=e.batch_id"""
        if status:
            rows = conn.execute(
                q + " WHERE e.status=? ORDER BY e.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY e.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            points = json.loads(d.pop("temp_points") or "[]")
            d.update(_proof_state(points, float(d["threshold"])))
            items.append(d)
        return {"items": items}


@router.post("/api/excursions/open")
def open_excursion(body: ExcursionOpenIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"batch:{body.batch_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (body.batch_id,)
            ).fetchone()
            if not batch:
                raise HTTPException(404, "批次不存在")
            if batch["status"] == "已封批":
                raise HTTPException(409, "已封批禁止开游标")
            open_n = conn.execute(
                """SELECT COUNT(*) c FROM excursion_cursors
                   WHERE batch_id=? AND status='开放'""",
                (body.batch_id,),
            ).fetchone()["c"]
            if open_n:
                raise HTTPException(409, "已有开放游标")
            ch = conn.execute(
                "SELECT target_max FROM cold_chambers WHERE id=?",
                (batch["chamber_id"],),
            ).fetchone()
            threshold = float(body.threshold if body.threshold is not None else ch["target_max"] + 2)
            points = json.loads(batch["temp_points"] or "[]")
            peak = detect_excursion(points, threshold)
            if peak is None:
                raise HTTPException(422, "温度点未超阈，无需开游标")
            key = f"EX-{batch['batch_code']}-{open_n + 1}"
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO excursion_cursors(batch_id,cursor_key,peak_temp,threshold,
                   status,opened_at,note) VALUES (?,?,?,?,?,?,?)""",
                (body.batch_id, key, peak, threshold, "开放", now, body.note),
            )
            conn.execute(
                "UPDATE temp_batches SET status='超温处理中' WHERE id=?",
                (body.batch_id,),
            )
            write_audit(
                conn,
                user,
                "excursion_open",
                key,
                {"batch_id": body.batch_id, "peak": peak},
            )
            conn.commit()
            return {"id": cur.lastrowid, "cursor_key": key, "peak_temp": peak, "status": "开放"}
        finally:
            release(conn, lock_key, user)
            # 异常路径上 with 块会回滚，须在此显式提交释放，避免游标/批次锁泄漏
            conn.commit()


@router.post("/api/excursions/close")
def close_excursion(body: ExcursionCloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"cursor:{body.cursor_id}"
    with connect() as conn:
        if not acquire_cursor_lock(conn, lock_key, user):
            # 后到：只留争用痕，不写任何游标业务，也不得抹成已收口
            write_audit(
                conn,
                user,
                "excursion_close_contended",
                f"cursor:{body.cursor_id}",
                {"lock_key": lock_key},
            )
            conn.commit()
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            row = conn.execute(
                "SELECT * FROM excursion_cursors WHERE id=?", (body.cursor_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "游标不存在")
            if row["status"] != "开放":
                raise HTTPException(409, "游标已关闭")
            batch = conn.execute(
                "SELECT temp_points FROM temp_batches WHERE id=?",
                (row["batch_id"],),
            ).fetchone()
            points = json.loads((batch["temp_points"] if batch else None) or "[]")
            proof = build_fallback_proof(points, float(row["threshold"]))
            if proof is None:
                # 甲路径：无回落证明 → 拒关，游标正式零改，只记回落偏航
                write_audit(
                    conn,
                    user,
                    "excursion_close_deviation",
                    row["cursor_key"],
                    {
                        "deviation": "回落偏航",
                        "reason": "缺回落证明",
                        "threshold": float(row["threshold"]),
                        "note_attempted": body.note,
                        "cursor_status": row["status"],
                    },
                )
                conn.commit()
                raise HTTPException(
                    409, "缺回落证明：最新采样未回落至阈值以内，拒绝关闭"
                )
            now = now_iso()
            conn.execute(
                """UPDATE excursion_cursors SET status='已闭', closed_at=?, note=?
                   WHERE id=?""",
                (now, body.note or row["note"], body.cursor_id),
            )
            open_left = conn.execute(
                """SELECT COUNT(*) c FROM excursion_cursors
                   WHERE batch_id=? AND status='开放'""",
                (row["batch_id"],),
            ).fetchone()["c"]
            if open_left == 0:
                conn.execute(
                    "UPDATE temp_batches SET status='已入库' WHERE id=? AND status='超温处理中'",
                    (row["batch_id"],),
                )
            write_audit(
                conn,
                user,
                "excursion_close",
                row["cursor_key"],
                {
                    "fallback_proof": proof,
                    "closed_at": now,
                },
            )
            conn.commit()
            return {
                "id": body.cursor_id,
                "status": "已闭",
                "closed_at": now,
                "fallback_proof": proof,
            }
        finally:
            release(conn, lock_key, user)
            # 异常路径上 with 块会回滚，须在此显式提交释放，避免游标锁泄漏
            conn.commit()