"""关游标回落证明三红：未证明拒关、意图不得算收口、同游标争用留痕。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _seed_cursor(c: TestClient, h: dict) -> dict:
    items = c.get("/api/excursions", headers=h).json()["items"]
    return next(x for x in items if x["cursor_key"] == "EX-TB-CK-HD-01-003-1")


def _row(cursor_id: int):
    with connect() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM excursion_cursors WHERE id=?", (cursor_id,)
            ).fetchone()
        )


def test_close_without_fallback_refused_zero_change():
    """甲：未回落仍可关 → 必须拒关且游标正式零改。"""
    c = TestClient(app)
    h = _auth(c)
    ex = _seed_cursor(c, h)
    before = _row(ex["id"])

    r = c.post(
        "/api/excursions/close",
        headers=h,
        json={"cursor_id": ex["id"], "note": "装车完成，班组口头确认"},
    )
    assert r.status_code in (409, 422), r.text

    after = _row(ex["id"])
    assert after["status"] == "开放"
    assert after["closed_at"] is None
    assert after["note"] == before["note"]

    batch = c.get(f"/api/batches/{ex['batch_id']}", headers=h).json()["batch"]
    assert batch["status"] == "超温处理中"

    audit = c.get("/api/audit?limit=100", headers=h).json()["items"]
    assert any(
        a["action"] == "excursion_close_deviation"
        and a["target"] == ex["cursor_key"]
        and a["payload"].get("deviation") == "回落偏航"
        for a in audit
    ), "拒关须记回落偏航"
    assert not any(
        a["action"] == "excursion_close" and a["target"] == ex["cursor_key"]
        for a in audit
    ), "零改须无收口审计"


def test_close_intent_not_promoted_never_counted_closed():
    """乙：关闭意图未晋升不得在任何页算已收口。"""
    c = TestClient(app)
    h = _auth(c)
    ex = _seed_cursor(c, h)

    c.post(
        "/api/excursions/close",
        headers=h,
        json={"cursor_id": ex["id"], "note": "先记意图，待采样"},
    )

    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["open_excursions"] >= 1

    proof = c.get("/api/proof/batch/TB-CK-HD-01-003", headers=h).json()
    assert proof["open_excursions"] >= 1
    assert proof["excursion_closed"] is False

    batch = c.get(f"/api/batches/{ex['batch_id']}", headers=h).json()
    assert any(
        e["id"] == ex["id"] and e["status"] == "开放" for e in batch["excursions"]
    )

    closed = c.get("/api/excursions?status=已闭", headers=h).json()["items"]
    assert all(x["id"] != ex["id"] for x in closed)

    row = _row(ex["id"])
    assert row["status"] == "开放" and row["closed_at"] is None

    assert (
        c.post(
            "/api/spectrum/export", headers=h, json={"batch_id": ex["batch_id"]}
        ).status_code
        == 409
    )


def test_two_sessions_close_same_cursor_contend():
    """争用：双人同游标抢关，后到留争用痕且不写业务。"""
    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_ops = _auth(c, "ops")
    ex = _seed_cursor(c, h_admin)
    lock_key = f"cursor:{ex['id']}"

    assert (
        c.post(
            "/api/locks/acquire", headers=h_admin, json={"lock_key": lock_key}
        ).status_code
        == 200
    )
    r = c.post(
        "/api/excursions/close",
        headers=h_ops,
        json={"cursor_id": ex["id"], "note": "替班关同一游标"},
    )
    assert r.status_code == 409, r.text

    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "ops" for x in cont
    ), "后到须留争用痕"

    row = _row(ex["id"])
    assert row["status"] == "开放" and row["closed_at"] is None

    locks = c.get("/api/locks", headers=h_admin).json()["items"]
    assert all(
        not (x["lock_key"] == lock_key and x["holder"] == "ops") for x in locks
    ), "后到争用者不得残留游标锁"

    assert (
        c.post(
            "/api/locks/release", headers=h_admin, json={"lock_key": lock_key}
        ).status_code
        == 200
    )
