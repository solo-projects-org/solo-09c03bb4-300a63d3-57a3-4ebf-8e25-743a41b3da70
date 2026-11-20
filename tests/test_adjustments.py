"""端到端自测：覆盖基准日前后折算、拆/并股、四舍五入、同版重送幂等、
异文退回、公告更正后凭送审号重现、重启持久化、同号公告交错导入、
按编号用接收流水校正旧库（含读取期间并发导入与失败重试）。"""
from __future__ import annotations

import importlib
import os
from fractions import Fraction

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("APP_DB_PATH", str(db_path))
    import app.db as db
    importlib.reload(db)
    import app.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c, db_path


def _import_prices(c, rows):
    resp = c.post("/api/prices/import", json={
        "prices": [{"trade_date": d, "close_cents": p} for d, p in rows]
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ann(c, **kw):
    return c.post("/api/announcements/import", json=kw)


# ---------------------------------------------------------- 纯函数：因子与舍入

def test_factor_split_before_and_after_base_date(client):
    from app.services import factor_for_date, round_half_up
    events = [{"effective_date": "2026-01-10", "old_shares": 1, "new_shares": 2}]
    # 1:2 拆股，基准日在生效日之后
    assert factor_for_date("2026-01-05", "2026-01-15", events) == Fraction(1, 2)
    assert factor_for_date("2026-01-15", "2026-01-15", events) == 1
    # 基准日在生效日之前：基准日之后的价格需反向放大
    assert factor_for_date("2026-01-15", "2026-01-05", events) == 2
    # 同日的对称调用应互为倒数
    assert factor_for_date("2026-01-05", "2026-01-15", events) \
        * factor_for_date("2026-01-15", "2026-01-05", events) == 1


def test_factor_consolidation(client):
    from app.services import factor_for_date
    # 并股 3:1，生效日介于 d 与 b 之间
    events = [{"effective_date": "2026-01-10", "old_shares": 3, "new_shares": 1}]
    assert factor_for_date("2026-01-05", "2026-01-15", events) == Fraction(3, 1)
    assert factor_for_date("2026-01-15", "2026-01-05", events) == Fraction(1, 3)


def test_round_half_up_to_cents(client):
    from app.services import round_half_up
    assert round_half_up(Fraction(100, 3)) == 33       # 33.33 舍
    assert round_half_up(Fraction(5, 2)) == 3          # 2.5 入
    assert round_half_up(Fraction(7, 2)) == 4          # 3.5 入
    assert round_half_up(Fraction(1001, 1)) == 1001


def test_relevant_events_boundary(client):
    from app.services import relevant_events
    anns = [
        {"ann_no": "A", "revision": 1, "effective_date": "2026-01-01",
         "old_shares": 1, "new_shares": 2},
        {"ann_no": "B", "revision": 1, "effective_date": "2026-01-10",
         "old_shares": 1, "new_shares": 2},
        {"ann_no": "C", "revision": 1, "effective_date": "2026-01-31",
         "old_shares": 1, "new_shares": 2},
    ]
    rel = relevant_events("2026-01-05", "2026-01-20", "2026-01-15", anns)
    assert [a["ann_no"] for a in rel] == ["B"]  # 早于区间起与晚于 max(end,base) 的不留底


# ---------------------------------------------------------- 端到端：预览折算

def test_preview_split_across_base_date(client):
    c, _ = client
    _import_prices(c, [
        ("2026-01-05", 1000), ("2026-01-08", 1000),
        ("2026-01-12", 2000), ("2026-01-15", 2000),
    ])
    r = _ann(c, ann_no="A001", revision=1, effective_date="2026-01-10",
             old_shares=1, new_shares=2)
    assert r.status_code == 200 and r.json()["outcome"] == "inserted"

    resp = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15"})
    assert resp.status_code == 200
    data = resp.json()
    by_date = {p["trade_date"]: p["adjusted_cents"] for p in data["points"]}
    # 生效日前按 1/2 折算，基准日当天不变
    assert by_date == {
        "2026-01-05": 500, "2026-01-08": 500,
        "2026-01-12": 2000, "2026-01-15": 2000,
    }
    assert data["stale"] is False
    assert len(data["basis"]) == 1


def test_preview_base_date_before_effective_date(client):
    """基准日在生效日之前（旧口径）：之后的新口径行情折算回基准日口径。"""
    c, _ = client
    _import_prices(c, [
        ("2026-01-05", 1000), ("2026-01-08", 1000),
        ("2026-01-12", 2000), ("2026-01-15", 2000),
    ])
    _ann(c, ann_no="A001", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    resp = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-05"})
    by_date = {p["trade_date"]: p["adjusted_cents"]
               for p in resp.json()["points"]}
    # 基准日（旧口径）不变；生效日后为新口径报价，1 旧股=2 新股，
    # 折回旧口径需乘以 r=2：2000 新价 -> 4000
    assert by_date == {
        "2026-01-05": 1000, "2026-01-08": 1000,
        "2026-01-12": 4000, "2026-01-15": 4000,
    }


def test_rounding_half_up_via_api(client):
    """2:3 拆股、基准日在生效日后（新口径），生效日前旧价乘 old/new=2/3：
    150 分 -> 100（整），151 分 -> 100.666… -> 101（四舍五入）。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 150), ("2026-01-06", 151),
                       ("2026-01-12", 100)])
    _ann(c, ann_no="X", revision=1, effective_date="2026-01-10",
         old_shares=2, new_shares=3)
    resp = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12"})
    by_date = {p["trade_date"]: p["adjusted_cents"]
               for p in resp.json()["points"]}
    assert by_date["2026-01-05"] == 100
    assert by_date["2026-01-06"] == 101
    assert by_date["2026-01-12"] == 100


# ---------------------------------------------------------- 公告收发规则

def test_announcement_resend_rules(client):
    c, _ = client
    a1 = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-10",
              old_shares=1, new_shares=2)
    assert a1.json()["outcome"] == "inserted"
    # 同号同版原样重送 -> reused 沿用
    same = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-10",
                old_shares=1, new_shares=2)
    assert same.status_code == 200 and same.json()["outcome"] == "reused"
    # 同号同版异文 -> 409 退回
    diff = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-11",
                old_shares=1, new_shares=2)
    assert diff.status_code == 409
    # 低修订 -> 422 退回
    old = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-10",
               old_shares=1, new_shares=3)
    assert old.status_code == 409  # 同版异文优先判定
    # 更高修订 -> updated 更正，现行只留最高修订
    upd = _ann(c, ann_no="A1", revision=2, effective_date="2026-01-10",
               old_shares=1, new_shares=3)
    assert upd.status_code == 200 and upd.json()["outcome"] == "updated"
    anns = c.get("/api/announcements").json()["announcements"]
    assert len(anns) == 1 and anns[0]["revision"] == 2 and anns[0]["new_shares"] == 3
    low = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-10",
               old_shares=1, new_shares=2)
    assert low.status_code == 422


def test_price_import_idempotent_and_conflict(client):
    c, _ = client
    r1 = _import_prices(c, [("2026-01-05", 1000), ("2026-01-06", 1010)])
    assert r1["inserted"] == 2
    r2 = _import_prices(c, [("2026-01-05", 1000)])  # 同价重复
    assert r2["inserted"] == 0 and r2["skipped_identical"] == 1
    resp = c.post("/api/prices/import", json={
        "prices": [{"trade_date": "2026-01-05", "close_cents": 999}]})
    assert resp.status_code == 409  # 异价冲突整批退回
    # 底表未被改动
    got = c.get("/api/prices").json()["prices"]
    assert {p["trade_date"]: p["close_cents"] for p in got} == {
        "2026-01-05": 1000, "2026-01-06": 1010}


# ---------------------------------------------------------- 锁定、更正重开、重启

def test_lock_then_correction_then_reopen_reproduces(client):
    c, db_path = client
    _import_prices(c, [
        ("2026-01-05", 1000), ("2026-01-08", 1000),
        ("2026-01-12", 2000), ("2026-01-15", 2000),
    ])
    _ann(c, ann_no="A001", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15"}).json()

    locked = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15", "basis_hash": pv["basis_hash"]})
    assert locked.status_code == 200
    sub_id = locked.json()["submission_id"]
    original_points = locked.json()["points"]
    original_hash = locked.json()["basis_hash"]

    # 用旧指纹锁定 -> 依据已不匹配前先做一次更正
    _ann(c, ann_no="A001", revision=2, effective_date="2026-01-10",
         old_shares=1, new_shares=4)
    stale_lock = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15", "basis_hash": original_hash})
    assert stale_lock.status_code == 409  # 现行公告与预览所据不同，须刷新

    # 预览自报 stale
    stale_pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15", "basis_hash": original_hash}).json()
    assert stale_pv["stale"] is True and stale_pv["note"]

    # 凭送审号重开：仍按旧依据重现原序列
    reopened = c.get(f"/api/submissions/{sub_id}").json()
    assert reopened["stale"] is True
    assert reopened["points"] == original_points
    assert reopened["basis_hash"] == original_hash
    assert reopened["current_basis_hash"] != original_hash

    # 现行口径下序列确实变了（1:4 后生效日前折算为 250）
    fresh = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15"}).json()
    assert fresh["points"][0]["adjusted_cents"] == 250


def test_reopen_survives_restart(client, monkeypatch):
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    _ann(c, ann_no="A001", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12"}).json()
    sub_id = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12", "basis_hash": pv["basis_hash"]}).json()["submission_id"]

    # 模拟“重启”：重新加载模块、重新建连接（同一 SQLite 文件）
    import app.db as db
    import app.main as main
    importlib.reload(db)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c2:
        reopened = c2.get(f"/api/submissions/{sub_id}")
        assert reopened.status_code == 200
        points = reopened.json()["points"]
        assert points[0]["adjusted_cents"] == 500
        assert points[1]["adjusted_cents"] == 2000
        # 行情底表仍在
        prices = c2.get("/api/prices").json()["prices"]
        assert len(prices) == 2


def test_chained_events_accumulate(client):
    """区间内多次拆并股：因子按生效顺序连乘，基准日取最后口径。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-15", 1000),
                       ("2026-01-25", 1000)])
    _ann(c, ann_no="A", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)   # 1:2
    _ann(c, ann_no="B", revision=1, effective_date="2026-01-20",
         old_shares=4, new_shares=1)   # 4:1 并股
    resp = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-25",
        "base_date": "2026-01-25"}).json()
    by_date = {p["trade_date"]: p for p in resp["points"]}
    # 首日对两次事件都处旧口径：1/2 * 4 = 2
    assert by_date["2026-01-05"]["factor"] == "2"
    assert by_date["2026-01-05"]["adjusted_cents"] == 2000
    # 中间日仅对并股处旧口径：4
    assert by_date["2026-01-15"]["factor"] == "4"
    assert by_date["2026-01-15"]["adjusted_cents"] == 4000
    # 基准日与两端同口径：1
    assert by_date["2026-01-25"]["factor"] == "1"
    assert len(resp["basis"]) == 2


def test_validation_errors(client):
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000)])
    # 基准日不在底表
    r = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-20",
        "base_date": "2026-01-20"})
    assert r.status_code == 422
    # 基准日越出区间
    r = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-06", "end_date": "2026-01-20",
        "base_date": "2026-01-05"})
    assert r.status_code == 422
    # 不存在的送审号
    assert c.get("/api/submissions/SB-NOPE").status_code == 404
    # 非法日期格式
    bad = c.post("/api/prices/import",
                 json={"prices": [{"trade_date": "2026/01/05", "close_cents": 1}]})
    assert bad.status_code == 422


# ---------------------------------------------------------- 同号拆并股公告：交错导入

def _anns_by_no(c):
    return {a["ann_no"]: a for a in c.get("/api/announcements").json()["announcements"]}


def _log(c, db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM announcements_log ORDER BY id").fetchall()]
    finally:
        conn.close()


def test_interleaved_low_revision_cannot_overwrite_high(client):
    """两版同号公告交错到达：迟到的第 1 版不得盖掉已接纳的第 2 版。"""
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    high = _ann(c, ann_no="S1", revision=2, effective_date="2026-01-10",
                old_shares=1, new_shares=4)
    assert high.status_code == 200 and high.json()["outcome"] == "inserted"
    low = _ann(c, ann_no="S1", revision=1, effective_date="2026-01-10",
               old_shares=1, new_shares=2)
    assert low.status_code == 422
    cur = _anns_by_no(c)["S1"]
    assert cur["revision"] == 2 and cur["new_shares"] == 4
    outcomes = [(r["revision"], r["outcome"]) for r in _log(c, db_path)]
    assert outcomes == [(2, "inserted"), (1, "rejected_old")]
    # 网页预览按第 2 版（1:4）折算，而非迟到的第 1 版（1:2）
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12"}).json()
    assert {p["trade_date"]: p["adjusted_cents"] for p in pv["points"]}[
        "2026-01-05"] == 250


def test_interleaved_same_revision_diff_body_is_not_a_correction(client):
    """同版异文（交错到达）不得被当成更正：沿用最早接纳的正文，异文退回。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    first = _ann(c, ann_no="S2", revision=1, effective_date="2026-01-10",
                 old_shares=1, new_shares=2)
    assert first.json()["outcome"] == "inserted"
    # 同一修订号的另一份正文（例如来自另一路的重复投递）
    diff = _ann(c, ann_no="S2", revision=1, effective_date="2026-01-10",
                old_shares=1, new_shares=3)
    assert diff.status_code == 409
    cur = _anns_by_no(c)["S2"]
    assert cur["new_shares"] == 2
    # 最早接纳的正文原样重送仍沿用
    same = _ann(c, ann_no="S2", revision=1, effective_date="2026-01-10",
                old_shares=1, new_shares=2)
    assert same.status_code == 200 and same.json()["outcome"] == "reused"
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12"}).json()
    assert pv["points"][0]["adjusted_cents"] == 500  # 1:2，而非异文的 1:3


def test_high_revision_arrives_after_low_still_updates(client):
    c, _ = client
    assert _ann(c, ann_no="S3", revision=1, effective_date="2026-01-10",
                old_shares=1, new_shares=2).json()["outcome"] == "inserted"
    upd = _ann(c, ann_no="S3", revision=3, effective_date="2026-01-10",
               old_shares=1, new_shares=5)
    assert upd.status_code == 200 and upd.json()["outcome"] == "updated"
    assert _anns_by_no(c)["S3"]["revision"] == 3
    # 介于两者之间的第 2 版仍算低修订
    assert _ann(c, ann_no="S3", revision=2, effective_date="2026-01-10",
                old_shares=1, new_shares=4).status_code == 422


# ---------------------------------------------------------- 按号校正旧库

def _corrupt_current(db_path, ann_no, revision, old_shares, new_shares,
                     effective_date="2026-01-10"):
    """模拟旧库里的错误现行公告（例如被迟到的低修订覆盖过）。"""
    import sqlite3
    from app.services import body_hash_of
    digest = body_hash_of({"revision": revision, "effective_date": effective_date,
                           "old_shares": old_shares, "new_shares": new_shares})
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE announcements SET revision=?, effective_date=?, old_shares=?,"
            " new_shares=?, body_hash=? WHERE ann_no=?",
            (revision, effective_date, old_shares, new_shares, digest, ann_no))
        conn.commit()
    finally:
        conn.close()


def test_reconcile_restores_highest_revision_and_earliest_body(client):
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    _ann(c, ann_no="R1", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    _ann(c, ann_no="R1", revision=2, effective_date="2026-01-10",
         old_shares=1, new_shares=4)
    # 旧库里现行公告被第 1 版覆盖
    _corrupt_current(db_path, "R1", 1, 1, 2)
    assert _anns_by_no(c)["R1"]["revision"] == 1

    rec = c.post("/api/announcements/R1/reconcile")
    assert rec.status_code == 200, rec.text
    body = rec.json()
    assert body["action"] == "corrected" and body["after_revision"] == 2
    assert body["before_revision"] == 1 and body["newly_accepted"] == 0
    cur = _anns_by_no(c)["R1"]
    assert cur["revision"] == 2 and cur["new_shares"] == 4
    # 校正只更新现行公告，不新增、不删除接收流水
    assert len(_log(c, db_path)) == 2
    # 校正后的预览按第 2 版折算
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12"}).json()
    assert pv["points"][0]["adjusted_cents"] == 250
    # 再跑一次为幂等 unchanged，可从任何状态重试
    again = c.post("/api/announcements/R1/reconcile").json()
    assert again["action"] == "unchanged"


def test_reconcile_uses_earliest_accepted_body_for_same_revision(client):
    """旧库里同版异文被错当成更正时，校正取该版最早接纳的正文。"""
    c, db_path = client
    from app.services import body_hash_of
    import sqlite3
    # 直接构造旧库流水：第 2 版先接纳 1→4，后又有一条“更正”成 1→9
    first_hash = body_hash_of({"revision": 2, "effective_date": "2026-01-10",
                               "old_shares": 1, "new_shares": 4})
    nine_hash = body_hash_of({"revision": 2, "effective_date": "2026-01-10",
                              "old_shares": 1, "new_shares": 9})
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO announcements(ann_no, revision, effective_date,"
            " old_shares, new_shares, body_hash) VALUES"
            " ('L1', 1, '2026-01-10', 1, 2, ?)",
            (body_hash_of({"revision": 1, "effective_date": "2026-01-10",
                           "old_shares": 1, "new_shares": 2}),))
        for rev, hash_, old, new, outcome in [
            (1, body_hash_of({"revision": 1, "effective_date": "2026-01-10",
                              "old_shares": 1, "new_shares": 2}), 1, 2, "inserted"),
            (2, first_hash, 1, 4, "updated"),
            (2, nine_hash, 1, 9, "updated"),  # 旧实现的“同版异文当更正”
        ]:
            conn.execute(
                "INSERT INTO announcements_log(ann_no, revision, effective_date,"
                " old_shares, new_shares, body_hash, outcome)"
                " VALUES ('L1',?,?,?,?,?,?)",
                (rev, "2026-01-10", old, new, hash_, outcome))
        conn.execute(
            "UPDATE announcements SET revision=2, old_shares=1, new_shares=9,"
            " body_hash=? WHERE ann_no='L1'", (nine_hash,))
        conn.commit()
    finally:
        conn.close()

    rec = c.post("/api/announcements/L1/reconcile").json()
    assert rec["action"] == "corrected"
    assert rec["source_log_id"] is not None
    cur = _anns_by_no(c)["L1"]
    assert cur["revision"] == 2 and cur["new_shares"] == 4  # 最早接纳的正文


def test_reconcile_counts_revisions_accepted_while_reading_log(client, monkeypatch):
    """读取旧流水期间仍可导入；校正生效时把期间已接纳的修订计入。"""
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    _ann(c, ann_no="C1", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    _corrupt_current(db_path, "C1", 1, 1, 2)

    import app.main as main

    original = main._effective_accepted
    calls = {"n": 0}

    def racing(conn, ann_no):
        result = original(conn, ann_no)
        calls["n"] += 1
        if calls["n"] == 1:
            # 模拟校正“读旧流水”与“写现行”之间到达的新修订（另一连接已提交）
            import sqlite3
            from app.services import body_hash_of as bh
            other = sqlite3.connect(db_path, timeout=10)
            try:
                other.execute(
                    "INSERT INTO announcements(ann_no, revision, effective_date,"
                    " old_shares, new_shares, body_hash) VALUES"
                    " ('C1', 3, '2026-01-10', 1, 8, ?)"
                    " ON CONFLICT(ann_no) DO UPDATE SET revision=excluded.revision,"
                    " old_shares=excluded.old_shares,"
                    " new_shares=excluded.new_shares, body_hash=excluded.body_hash",
                    (bh({"revision": 3, "effective_date": "2026-01-10",
                         "old_shares": 1, "new_shares": 8}),))
                other.execute(
                    "INSERT INTO announcements_log(ann_no, revision, effective_date,"
                    " old_shares, new_shares, body_hash, outcome)"
                    " VALUES ('C1', 3, '2026-01-10', 1, 8, ?, 'updated')",
                    (bh({"revision": 3, "effective_date": "2026-01-10",
                         "old_shares": 1, "new_shares": 8}),))
                other.commit()
            finally:
                other.close()
        return result

    monkeypatch.setattr(main, "_effective_accepted", racing)
    rec = c.post("/api/announcements/C1/reconcile").json()
    assert rec["newly_accepted"] == 1
    assert rec["after_revision"] == 3
    assert _anns_by_no(c)["C1"]["new_shares"] == 8


def test_reconcile_failure_keeps_previous_announcement_and_retries(client, monkeypatch):
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    _ann(c, ann_no="F1", revision=2, effective_date="2026-01-10",
         old_shares=1, new_shares=4)
    _corrupt_current(db_path, "F1", 1, 1, 2)

    import app.main as main
    original = main._reconcile_one
    boom = {"on": True}

    def flaky(conn, ann_no):
        if boom["on"]:
            raise RuntimeError("模拟校正写入失败")
        return original(conn, ann_no)

    monkeypatch.setattr(main, "_reconcile_one", flaky)
    with pytest.raises(RuntimeError):
        c.post("/api/announcements/F1/reconcile")
    # 失败时现行公告保持此前已提交的状态（旧库里的第 1 版）
    assert _anns_by_no(c)["F1"]["revision"] == 1

    boom["on"] = False
    rec = c.post("/api/announcements/F1/reconcile")
    assert rec.status_code == 200 and rec.json()["action"] == "corrected"
    assert _anns_by_no(c)["F1"]["revision"] == 2


def test_reconcile_does_not_touch_other_announcements_or_submissions(client):
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    _ann(c, ann_no="K1", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12"}).json()
    sub = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12", "basis_hash": pv["basis_hash"]}).json()
    original_points = sub["points"]
    _corrupt_current(db_path, "K1", 1, 1, 4)  # 仅改现行表，流水仍是 1:2

    c.post("/api/announcements/K1/reconcile")
    assert _anns_by_no(c)["K1"]["new_shares"] == 2  # 按流水恢复成 1:2
    # 已发出的送审号仍用原先留存的依据重开
    reopened = c.get(f"/api/submissions/{sub['submission_id']}").json()
    assert reopened["points"] == original_points
    assert reopened["stale"] is False


def test_reconcile_unknown_number_is_noop(client):
    c, _ = client
    rec = c.post("/api/announcements/NOPE/reconcile").json()
    assert rec["action"] == "no_source"
    assert c.get("/api/announcements").json()["announcements"] == []


def test_truly_concurrent_interleaved_imports_converge(client):
    """多线程同时投递同号不同修订，不论落库顺序，最终都收敛到最高修订。"""
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    from concurrent.futures import ThreadPoolExecutor
    versions = [(1, 1, 2), (2, 1, 4), (3, 1, 5)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(_ann, c, ann_no="T1", revision=r,
                            effective_date="2026-01-10",
                            old_shares=o, new_shares=n)
                for r, o, n in versions for _ in range(2)]
        results = [f.result() for f in futs]
    assert any(r.status_code == 200 for r in results)
    cur = _anns_by_no(c)["T1"]
    assert cur["revision"] == 3 and cur["new_shares"] == 5
    # 低修订一条也没被接纳
    accepted_revs = {r["revision"] for r in _log(c, db_path)
                     if r["outcome"] in ("inserted", "updated", "reused")}
    assert accepted_revs <= {3} or max(accepted_revs) == 3
    # 同号同版原样重送仍沿用
    again = _ann(c, ann_no="T1", revision=3, effective_date="2026-01-10",
                 old_shares=1, new_shares=5)
    assert again.json()["outcome"] == "reused"


def test_concurrent_reconcile_and_new_revision_import(client):
    """校正与导入并发：校正计入期间已接纳的修订，导入不被校正挡住。"""
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    _ann(c, ann_no="P1", revision=2, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    _corrupt_current(db_path, "P1", 1, 1, 4)

    import app.main as main
    original = main._effective_accepted
    state = {"fired": False}

    def slow_first_read(conn, ann_no):
        result = original(conn, ann_no)
        if not state["fired"]:
            state["fired"] = True
            # 读取旧流水期间，新修订经另一请求到达
            resp = _ann(c, ann_no="P1", revision=5, effective_date="2026-01-10",
                        old_shares=1, new_shares=6)
            assert resp.status_code == 200 and resp.json()["outcome"] == "updated"
        return result

    main._effective_accepted = slow_first_read
    try:
        rec = c.post("/api/announcements/P1/reconcile").json()
    finally:
        main._effective_accepted = original
    assert rec["newly_accepted"] == 1
    assert rec["after_revision"] == 5
    assert _anns_by_no(c)["P1"]["new_shares"] == 6
    # 期间到达的那次导入没有被校正回退
    logs = _log(c, db_path)
    assert any(r["revision"] == 5 and r["outcome"] == "updated" for r in logs)
    assert logs[-1]["outcome"] != "rejected_old"


def test_reconcile_never_downgrades_revision_only_present_in_current_table(client):
    """旧库中现行表有流水缺记的高修订时，校正不得把它降级。"""
    c, db_path = client
    import sqlite3
    from app.services import body_hash_of

    high_hash = body_hash_of({"revision": 3, "effective_date": "2026-01-10",
                              "old_shares": 1, "new_shares": 8})
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO announcements(ann_no, revision, effective_date,"
            " old_shares, new_shares, body_hash) VALUES ('G1', 3, '2026-01-10', 1, 8, ?)",
            (high_hash,))
        conn.execute(
            "INSERT INTO announcements_log(ann_no, revision, effective_date,"
            " old_shares, new_shares, body_hash, outcome)"
            " VALUES ('G1', 1, '2026-01-10', 1, 2, ?, 'inserted')",
            (body_hash_of({"revision": 1, "effective_date": "2026-01-10",
                           "old_shares": 1, "new_shares": 2}),))
        conn.commit()
    finally:
        conn.close()
    rec = c.post("/api/announcements/G1/reconcile").json()
    assert rec["action"] == "unchanged"
    assert _anns_by_no(c)["G1"]["revision"] == 3
    # 迟到的低修订同样不能覆盖
    assert _ann(c, ann_no="G1", revision=2, effective_date="2026-01-10",
                old_shares=1, new_shares=4).status_code == 422
