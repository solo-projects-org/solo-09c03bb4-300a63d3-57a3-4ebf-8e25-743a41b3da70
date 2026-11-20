"""FastAPI 入口：行情/公告导入、折算预览、锁定送审、凭号重开。"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.db import db_cursor, init_db
from app.models import (
    AnnouncementIn,
    AnnouncementResult,
    LockInRequest,
    LockInResult,
    PreviewResult,
    PriceImportResult,
    PriceIn,
    ReconcileResult,
    ReopenResult,
)
from app.services import (
    basis_hash_of,
    body_hash_of,
    build_series,
    relevant_events,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="拆并股收盘价折算", version="1.0.0", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ---------------------------------------------------------------- 行情导入

class PriceBatch(BaseModel):
    prices: list[PriceIn] = Field(min_length=1)


@app.post("/api/prices/import", response_model=PriceImportResult)
def import_prices(batch: PriceBatch):
    """一次导入单股日收盘价（分）。

    同日期同价视为重复跳过；同日期不同价属冲突，整批退回（409），
    底表保持只增不改不删，送审与更正共用此底表。
    """
    received = len(batch.prices)
    seen: dict[str, int] = {}
    for item in batch.prices:
        if item.trade_date in seen and seen[item.trade_date] != item.close_cents:
            raise HTTPException(
                status_code=422,
                detail=f"批次内同一交易日 {item.trade_date} 出现不同收盘价",
            )
        seen[item.trade_date] = item.close_cents

    conflicts: list[dict] = []
    with db_cursor() as conn:
        for trade_date, close_cents in seen.items():
            row = conn.execute(
                "SELECT close_cents FROM prices WHERE trade_date=?", (trade_date,)
            ).fetchone()
            if row is not None and row["close_cents"] != close_cents:
                conflicts.append({
                    "trade_date": trade_date,
                    "existing_cents": row["close_cents"],
                    "submitted_cents": close_cents,
                })
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={"message": "底表已存在同日期不同收盘价，整批未写入",
                        "conflicts": conflicts},
            )
        inserted = 0
        for trade_date, close_cents in seen.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO prices(trade_date, close_cents) VALUES (?,?)",
                (trade_date, close_cents),
            )
            inserted += cur.rowcount
    return PriceImportResult(
        received=received,
        inserted=inserted,
        skipped_identical=received - inserted,
        conflicts=conflicts,
    )


@app.get("/api/prices")
def list_prices(start_date: str | None = None, end_date: str | None = None):
    sql = "SELECT trade_date, close_cents FROM prices"
    where, params = [], []
    if start_date:
        where.append("trade_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("trade_date <= ?")
        params.append(end_date)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY trade_date"
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"prices": [dict(r) for r in rows]}


# ---------------------------------------------------------------- 公告导入

def _current_announcements(conn) -> list[dict]:
    """每号取最高修订（ann_no 为主键，表中即现行版本）。"""
    rows = conn.execute(
        "SELECT ann_no, revision, effective_date, old_shares, new_shares, body_hash "
        "FROM announcements ORDER BY ann_no"
    ).fetchall()
    return [dict(r) for r in rows]


def _accepted_versions(conn, ann_no: str) -> dict[int, dict]:
    """该编号接收流水里已接纳的全部版本：每版取最早接纳的那一次。

    只认 inserted/updated/reused（rejected_* 是退回，不算接纳）。同一修订号若因
    交错导入出现了两条不同正文的接纳记录，以流水 id 最小（最早接纳）的为准，
    保证“同号同版异文”判定不受到达顺序影响。
    """
    rows = conn.execute(
        "SELECT id, revision, effective_date, old_shares, new_shares, body_hash,"
        " outcome, received_at FROM announcements_log"
        " WHERE ann_no=? AND outcome IN ('inserted','updated','reused')"
        " ORDER BY id",
        (ann_no,),
    ).fetchall()
    accepted: dict[int, dict] = {}
    for row in rows:
        accepted.setdefault(int(row["revision"]), dict(row))
    return accepted


def _effective_accepted(conn, ann_no: str) -> dict[int, dict]:
    """已接纳版本全集 = 接收流水 ∪ 现行公告。

    现行表若有一条流水里没有的修订（旧库/人工预置），也应视为已接纳，
    否则迟到的低修订会把它覆盖掉、校正也会把它误降级；同一修订号两者都有时，
    正文以流水里最早接纳的那一次为准（现行表若被旧 bug 改坏，交给按号校正修复）。
    """
    accepted = _accepted_versions(conn, ann_no)
    row = conn.execute(
        "SELECT revision, effective_date, old_shares, new_shares, body_hash"
        " FROM announcements WHERE ann_no=?",
        (ann_no,),
    ).fetchone()
    if row is not None:
        rev = int(row["revision"])
        accepted.setdefault(rev, {
            "id": None, "revision": rev, "effective_date": row["effective_date"],
            "old_shares": int(row["old_shares"]),
            "new_shares": int(row["new_shares"]), "body_hash": row["body_hash"],
        })
    return accepted


def _log_rejection(conn, ann: AnnouncementIn, digest: str,
                   outcome: str, note: str, status_code: int,
                   detail: str) -> None:
    """退回也要留审计流水：先提交流水再抛错，避免被事务回滚吞掉。"""
    conn.execute(
        "INSERT INTO announcements_log(ann_no, revision, effective_date,"
        " old_shares, new_shares, body_hash, outcome, note)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
         ann.new_shares, digest, outcome, note),
    )
    conn.commit()
    raise HTTPException(status_code=status_code, detail=detail)


@app.post("/api/announcements/import", response_model=AnnouncementResult)
def import_announcement(ann: AnnouncementIn):
    """公告收发规则（以该编号已接纳的最高修订为准）：

    - 修订号低于已接纳的最高修订 → 退回 422，记 rejected_old（即便它比现行表里的
      版本高也不得覆盖；交错的低修订不能盖掉高修订）；
    - 等于最高修订：正文与该版最早接纳的正文一致 → reused 沿用；
      正文不同 → 409 退回，记 rejected_diff（同版异文永远不会被当成更正）；
    - 高于最高修订 → 更正现行版本，记 updated/inserted（旧版仅留流水）。
    """
    payload = ann.model_dump()
    digest = body_hash_of(payload)
    with db_cursor() as conn:
        # 写事务先取锁，避免两个并发导入各自读到旧状态后互相覆盖。
        conn.execute("BEGIN IMMEDIATE")
        accepted = _effective_accepted(conn, ann.ann_no)
        highest_rev = max(accepted) if accepted else None
        current = accepted.get(highest_rev) if highest_rev is not None else None

        if highest_rev is not None and ann.revision < highest_rev:
            _log_rejection(
                conn, ann, digest, "rejected_old",
                "修订号低于已接纳的最高修订，退回", 422,
                f"公告 {ann.ann_no} 已接纳的最高修订为第 {highest_rev} 版，"
                f"收到第 {ann.revision} 版（更低），退回",
            )

        if current is not None and ann.revision == highest_rev:
            if digest == current["body_hash"]:
                conn.execute(
                    "INSERT INTO announcements_log(ann_no, revision, effective_date,"
                    " old_shares, new_shares, body_hash, outcome, note)"
                    " VALUES (?,?,?,?,?,?, 'reused', '同号同版原样重送，沿用原公告')",
                    (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
                     ann.new_shares, digest),
                )
                return AnnouncementResult(**payload, outcome="reused",
                                          note="同号同版原样重送，沿用原公告")
            _log_rejection(
                conn, ann, digest, "rejected_diff",
                "同号同版异文，退回", 409,
                f"公告 {ann.ann_no} 第 {ann.revision} 版已接纳且正文不同，异文退回",
            )

        outcome = "inserted" if current is None else "updated"
        note = "首次导入" if current is None else f"更高修订，替代第 {highest_rev} 版"
        conn.execute(
            "INSERT INTO announcements(ann_no, revision, effective_date, old_shares,"
            " new_shares, body_hash) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(ann_no) DO UPDATE SET revision=excluded.revision,"
            " effective_date=excluded.effective_date, old_shares=excluded.old_shares,"
            " new_shares=excluded.new_shares, body_hash=excluded.body_hash",
            (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
             ann.new_shares, digest),
        )
        conn.execute(
            "INSERT INTO announcements_log(ann_no, revision, effective_date,"
            " old_shares, new_shares, body_hash, outcome, note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
             ann.new_shares, digest, outcome, note),
        )
        return AnnouncementResult(**payload, outcome=outcome, note=note)


@app.get("/api/announcements")
def list_announcements():
    with db_cursor() as conn:
        anns = _current_announcements(conn)
    return {"announcements": anns}


# ------------------------------------------------------------ 旧库按号校正

def _restore_target(accepted: dict[int, dict]) -> dict | None:
    """按规则给出应恢复的现行版本：已接纳的最高修订，正文取该版最早接纳的一次。"""
    if not accepted:
        return None
    highest = max(accepted)
    return accepted[highest]


def _reconcile_one(conn, ann_no: str) -> dict:
    """校正一个编号的现行公告。调用方负责事务边界与写锁。

    目标版本从“流水 ∪ 现行表”里取最高修订（现行表里已存在、流水缺记的版本
    仍算已接纳，避免把合法的高版本误降级）；正文以流水里该版最早接纳的为准。
    """
    accepted = _effective_accepted(conn, ann_no)
    target = _restore_target(accepted)
    if target is None:
        return {"ann_no": ann_no, "action": "no_source",
                "note": "接收流水无已接纳版本，保持现行公告不变",
                "before_revision": None, "after_revision": None, "newly_accepted": 0}

    current = conn.execute(
        "SELECT revision, effective_date, old_shares, new_shares, body_hash"
        " FROM announcements WHERE ann_no=?",
        (ann_no,),
    ).fetchone()

    if current is not None and current["body_hash"] == target["body_hash"] \
            and int(current["revision"]) == int(target["revision"]):
        action = "unchanged"
        note = "现行公告已符合接收流水规则，无需改动"
    elif current is None:
        action = "restored"
        note = "现行公告缺失，按接收流水恢复"
    else:
        action = "corrected"
        note = (f"按接收流水恢复为第 {target['revision']} 版"
                f"（原为第 {current['revision']} 版）")

    if action != "unchanged":
        conn.execute(
            "INSERT INTO announcements(ann_no, revision, effective_date, old_shares,"
            " new_shares, body_hash) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(ann_no) DO UPDATE SET revision=excluded.revision,"
            " effective_date=excluded.effective_date, old_shares=excluded.old_shares,"
            " new_shares=excluded.new_shares, body_hash=excluded.body_hash",
            (ann_no, target["revision"], target["effective_date"],
             target["old_shares"], target["new_shares"], target["body_hash"]),
        )
    conn.execute(
        "INSERT INTO reconciliation_log(ann_no, before_revision, before_body_hash,"
        " after_revision, after_body_hash, source_log_id, action, note)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (ann_no,
         None if current is None else int(current["revision"]),
         None if current is None else current["body_hash"],
         int(target["revision"]), target["body_hash"],
         None if target.get("id") is None else int(target["id"]),
         action, note),
    )
    return {
        "ann_no": ann_no,
        "action": action,
        "note": note,
        "revision": int(target["revision"]),
        "effective_date": target["effective_date"],
        "old_shares": int(target["old_shares"]),
        "new_shares": int(target["new_shares"]),
        "before_revision": None if current is None else int(current["revision"]),
        "after_revision": int(target["revision"]),
        "source_log_id": None if target.get("id") is None else int(target["id"]),
    }


def _reconcile_announcement(ann_no: str) -> dict:
    """按编号校正旧库：先读接收流水（不持写锁，期间仍可导入），
    再在写事务里重读流水，把读取期间新接纳的修订一并计入，只更新现行公告。

    失败时整笔事务回滚，此前已提交的现行公告原样保留，可重复调用重试。
    """
    with db_cursor() as conn:
        before = _effective_accepted(conn, ann_no)
        before_ids = {int(v["id"]) for v in before.values() if v.get("id") is not None}

    with db_cursor() as conn:
        conn.execute("BEGIN IMMEDIATE")
        after = _effective_accepted(conn, ann_no)
        result = _reconcile_one(conn, ann_no)
        new_ids = {int(v["id"]) for v in after.values() if v.get("id") is not None}
        result["newly_accepted"] = len(new_ids - before_ids)
        if result["newly_accepted"]:
            result["note"] += (f"；读取旧流水期间又接纳 {result['newly_accepted']} 个修订，"
                               "已一并计入")
        return result


@app.post("/api/announcements/{ann_no}/reconcile", response_model=ReconcileResult)
def reconcile_announcement(ann_no: str):
    """按公告编号，用接收流水恢复符合规则的现行公告（可重试、只动现行公告）。"""
    return _reconcile_announcement(ann_no)


# ---------------------------------------------------------------- 预览/送审

def _load_prices(conn, start_date: str, end_date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT trade_date, close_cents FROM prices"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) for r in rows]


def _validate_window(conn, start_date: str, end_date: str, base_date: str) -> None:
    if not (start_date <= end_date):
        raise HTTPException(status_code=422, detail="start_date 不能晚于 end_date")
    if not (start_date <= base_date <= end_date):
        raise HTTPException(status_code=422,
                            detail="基准交易日必须落在所选区间内")
    row = conn.execute(
        "SELECT 1 FROM prices WHERE trade_date=?", (base_date,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=422,
                            detail=f"基准交易日 {base_date} 在行情底表中不存在")
    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM prices WHERE trade_date BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchone()["c"]
    if cnt == 0:
        raise HTTPException(status_code=422,
                            detail="所选区间内没有任何行情数据")


def _preview(conn, start_date: str, end_date: str, base_date: str,
             client_basis_hash: str | None) -> PreviewResult:
    _validate_window(conn, start_date, end_date, base_date)
    anns = _current_announcements(conn)
    current_hash = basis_hash_of(anns)
    events = relevant_events(start_date, end_date, base_date, anns)
    prices = _load_prices(conn, start_date, end_date)
    points = build_series(prices, base_date, events)
    stale = client_basis_hash is not None and client_basis_hash != current_hash
    note = ""
    if stale:
        note = "现行公告与预览所据依据不同，须刷新后再锁定"
    return PreviewResult(
        start_date=start_date,
        end_date=end_date,
        base_date=base_date,
        basis_hash=current_hash,
        basis=events,
        points=points,
        stale=stale,
        note=note,
    )


@app.get("/api/adjustments/preview", response_model=PreviewResult)
def preview(start_date: str, end_date: str, base_date: str,
            basis_hash: str | None = None):
    """区间折算预览。可带预览时拿到的 basis_hash，用于检测现行公告是否已变更。"""
    with db_cursor() as conn:
        return _preview(conn, start_date, end_date, base_date, basis_hash)


@app.post("/api/submissions", response_model=LockInResult)
def lock_submission(req: LockInRequest):
    """锁定预览并取得送审号；依据与现行公告不一致（stale）时拒绝，须先刷新。"""
    with db_cursor() as conn:
        preview_data = _preview(conn, req.start_date, req.end_date,
                                req.base_date, req.basis_hash)
        if preview_data.stale:
            raise HTTPException(
                status_code=409,
                detail={"message": preview_data.note,
                        "current_basis_hash": preview_data.basis_hash},
            )
        submission_id = "SB-" + uuid.uuid4().hex[:16].upper()
        conn.execute(
            "INSERT INTO submissions(submission_id, start_date, end_date,"
            " base_date, basis_hash) VALUES (?,?,?,?,?)",
            (submission_id, req.start_date, req.end_date, req.base_date,
             preview_data.basis_hash),
        )
        # 只留重算所需的依据，不复制行情底表
        for e in preview_data.basis:
            conn.execute(
                "INSERT INTO submission_basis(submission_id, ann_no, revision,"
                " effective_date, old_shares, new_shares) VALUES (?,?,?,?,?,?)",
                (submission_id, e["ann_no"], e["revision"], e["effective_date"],
                 e["old_shares"], e["new_shares"]),
            )
        return LockInResult(submission_id=submission_id,
                            basis_hash=preview_data.basis_hash,
                            points=preview_data.points)


@app.get("/api/submissions/{submission_id}", response_model=ReopenResult)
def reopen_submission(submission_id: str):
    """凭送审号重开：按快照依据 + 同一行情底表重现原序列。

    即使公告随后被更正、或进程重启，序列仍与锁定时一致；
    同时返回现行依据指纹与 stale 标记供研究人员比对。
    """
    with db_cursor() as conn:
        sub = conn.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (submission_id,)
        ).fetchone()
        if sub is None:
            raise HTTPException(status_code=404,
                                detail=f"送审号 {submission_id} 不存在")
        basis_rows = conn.execute(
            "SELECT ann_no, revision, effective_date, old_shares, new_shares"
            " FROM submission_basis WHERE submission_id=? ORDER BY effective_date, ann_no",
            (submission_id,),
        ).fetchall()
        basis = [dict(r) for r in basis_rows]
        prices = _load_prices(conn, sub["start_date"], sub["end_date"])
        points = build_series(prices, sub["base_date"], basis)

        current_hash = basis_hash_of(_current_announcements(conn))
        stale = current_hash != sub["basis_hash"]
        note = ("公告已更正：以下为按送审时依据快照重现的原序列"
                if stale else "现行公告与送审依据一致")
        return ReopenResult(
            submission_id=submission_id,
            start_date=sub["start_date"],
            end_date=sub["end_date"],
            base_date=sub["base_date"],
            basis_hash=sub["basis_hash"],
            current_basis_hash=current_hash,
            stale=stale,
            note=note,
            points=points,
        )


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
