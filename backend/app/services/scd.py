import json
from uuid import uuid4

from app.db import connect
from app.modules import logical_delete_close as ldc
from app.modules import retire_mirror_badge as rbadge
from app.seed import now_iso


def apply_type2(detect_run_id: str):
    """Apply a detect run to hist + mirror.

    退役管道（logical_delete_close，落库甲 + 失败闭甲）：
      - 快照 biz_status∈退役集 → 同事务内关现行段（valid_to+is_current=0）、
        开退役段（is_current=1）、刷镜为退役态；
      - 同事务逐键投影镜侧角标（retire_mirror_badge，架构乙·现场权威）：
        退役→卸可写角标并写退役角标快照（closed_hist_id=退役当前段），
        营业/显式复活→可写角标；只动本键，邻键不串改；
      - 键已退役而快照无显式 biz_status=open 复活标记 → 不动维与镜与角标，
        同事务写 quarantine 留因；
      - 提交前跑不变量（双当前 / 关闭段缺 valid_to / 镜段不一 / 退役镜挂营业段 /
        角标↔现场互证），任一违例整体回滚——关段、退役痕迹、刷镜、卸角标、
        搁置要么全成要么全败。
    """
    conn = connect()
    now = now_iso()
    try:
        run = conn.execute(
            "SELECT * FROM detect_runs WHERE id=?", (detect_run_id,)
        ).fetchone()
        if not run:
            return {"ok": False, "message": "检测跑次不存在"}
        retired = ldc.retired_set(conn)
        revive = ldc.revive_status(conn)
        report = json.loads(run["report_json"])
        changes = report.get("changes", [])
        applied = []
        shelved = []
        for ch in changes:
            store_id = ch["store_id"]
            snap = conn.execute(
                "SELECT * FROM source_snapshots WHERE id=?", (ch["snapshot_id"],)
            ).fetchone()
            if not snap:
                continue
            mirror = conn.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id=?",
                (store_id,),
            ).fetchone()
            # 退役守卫：以应用时刻的镜态重判（检测时的 decision 只是预判）
            action, reason = ldc.decide(
                snap["biz_status"],
                snap["biz_status_explicit"],
                mirror["biz_status"] if mirror else None,
                retired,
                revive,
            )
            if action == "shelve":
                conn.execute(
                    "INSERT INTO quarantine(reason, member_json, created_at) VALUES (?,?,?)",
                    (
                        ldc.SHELVE_REASON,
                        json.dumps(
                            {
                                "store_id": store_id,
                                "snapshot_id": snap["id"],
                                "detect_run_id": detect_run_id,
                                "mirror_status": mirror["biz_status"] if mirror else None,
                                "snap_status": snap["biz_status"],
                                "snap_explicit": snap["biz_status_explicit"],
                                "detail": reason,
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                shelved.append(
                    {
                        "store_id": store_id,
                        "snapshot_id": snap["id"],
                        "reason": ldc.SHELVE_REASON,
                        "detail": reason,
                    }
                )
                continue
            cur_hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
                (store_id,),
            ).fetchone()
            valid_from = snap["as_of_ts"]
            if cur_hist:
                conn.execute(
                    """UPDATE dim_store_hours_hist
                       SET valid_to=?, is_current=0, closed_by_run=?
                       WHERE id=?""",
                    (valid_from, detect_run_id, cur_hist["id"]),
                )
            new_id = f"HIST-{uuid4().hex[:10]}"
            rh = snap["row_hash"]
            retiring = ldc.is_retired(snap["biz_status"], retired)
            reviving = bool(
                mirror and ldc.is_retired(mirror["biz_status"], retired) and not retiring
            )
            conn.execute(
                """INSERT INTO dim_store_hours_hist(
                  id, store_id, open_time, close_time, weekday_mask, biz_status,
                  valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id,
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    valid_from,
                    None,
                    1,
                    snap["id"],
                    rh,
                    None,
                ),
            )
            conn.execute(
                """INSERT INTO dim_store_hours_current(
                  store_id, open_time, close_time, weekday_mask, biz_status,
                  hist_id, as_of_ts, row_hash
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(store_id) DO UPDATE SET
                  open_time=excluded.open_time,
                  close_time=excluded.close_time,
                  weekday_mask=excluded.weekday_mask,
                  biz_status=excluded.biz_status,
                  hist_id=excluded.hist_id,
                  as_of_ts=excluded.as_of_ts,
                  row_hash=excluded.row_hash
                """,
                (
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    new_id,
                    valid_from,
                    rh,
                ),
            )
            # 角标同事务投影（架构乙·现场权威）：退役→同成败卸可写角标并写退役角标
            # 快照（store_id、closed_hist_id=退役当前段、badge=retired、时刻）；
            # 营业/显式复活→可写角标。只动本键，邻键不串改；缺一由提交前不变量
            # 检出并整体回滚，不许半截落库。
            badge, badge_closed_ref = rbadge.desired_badge(
                snap["biz_status"], new_id, retired
            )
            rbadge.upsert_badge(conn, store_id, badge, badge_closed_ref, now)
            applied.append(
                {
                    "store_id": store_id,
                    "hist_id": new_id,
                    "closed": cur_hist["id"] if cur_hist else None,
                    "retired": retiring,
                    "badge_unloaded": retiring,
                    "badge": badge,
                    "badge_closed_hist_id": badge_closed_ref,
                    "revived": reviving,
                }
            )
        # 失败闭：提交前不变量（含角标↔现场互证），违例整体回滚
        # （含本事务已写的关段/退役段/刷镜/搁置留因/角标，缺一不留半截）
        violations = ldc.check_invariants(conn, retired)
        if violations:
            conn.rollback()
            return {
                "ok": False,
                "message": "失败闭：不变量违例，本批已整体回滚",
                "violations": violations,
                "detect_run_id": detect_run_id,
            }
        conn.commit()
        return {
            "ok": True,
            "applied": applied,
            "count": len(applied),
            "shelved": shelved,
            "shelved_count": len(shelved),
            "detect_run_id": detect_run_id,
        }
    except Exception as exc:  # 失败闭：任何异常都不许半截落库
        conn.rollback()
        return {"ok": False, "message": f"失败闭：{exc}", "detect_run_id": detect_run_id}
    finally:
        conn.close()


def rebuild_proofs():
    """Attest: each current mirror hist_id must point to is_current=1 hist with same hash."""
    conn = connect()
    now = now_iso()
    try:
        mirrors = conn.execute("SELECT * FROM dim_store_hours_current ORDER BY store_id").fetchall()
        results = []
        for m in mirrors:
            hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (m["hist_id"],)
            ).fetchone()
            valid = 1
            detail = {}
            if not hist:
                valid = 0
                detail["error"] = "mirror_hist_missing"
            elif hist["is_current"] != 1:
                valid = 0
                detail["error"] = "mirror_not_current_hist"
            elif hist["row_hash"] != m["row_hash"]:
                valid = 0
                detail["error"] = "hash_mismatch"
            elif hist["store_id"] != m["store_id"]:
                valid = 0
                detail["error"] = "store_mismatch"
            else:
                detail["ok"] = True
            conn.execute(
                """INSERT INTO scd_proofs(
                  store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    m["store_id"],
                    hist["id"] if hist else "",
                    m["hist_id"],
                    m["row_hash"],
                    valid,
                    json.dumps(detail, ensure_ascii=False),
                    now,
                ),
            )
            results.append({"store_id": m["store_id"], "valid": bool(valid), "detail": detail})
        conn.commit()
        return {
            "checked": len(results),
            "failed": sum(1 for r in results if not r["valid"]),
            "results": results,
        }
    finally:
        conn.close()
