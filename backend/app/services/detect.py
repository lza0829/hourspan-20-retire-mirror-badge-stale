import json
from uuid import uuid4

from app.db import connect
from app.modules import logical_delete_close as ldc
from app.seed import now_iso


def run_detect(batch_id: str | None = None):
    """Compare latest snapshots (optionally one batch) vs current mirror; report changes."""
    conn = connect()
    run_id = f"DET-{uuid4().hex[:8]}"
    now = now_iso()
    try:
        retired = ldc.retired_set(conn)
        revive = ldc.revive_status(conn)
        if batch_id:
            snaps = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY store_id, as_of_ts",
                (batch_id,),
            ).fetchall()
        else:
            snaps = conn.execute(
                """
                SELECT s.* FROM source_snapshots s
                JOIN (
                  SELECT store_id, MAX(as_of_ts) AS mx FROM source_snapshots GROUP BY store_id
                ) t ON s.store_id=t.store_id AND s.as_of_ts=t.mx
                ORDER BY s.store_id
                """
            ).fetchall()

        changes = []
        for snap in snaps:
            cur = conn.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id=?",
                (snap["store_id"],),
            ).fetchone()
            # 退役守卫预判（与入档应用共用同一纯函数；应用时以当时镜态重判为准）
            action, reason = ldc.decide(
                snap["biz_status"],
                snap["biz_status_explicit"],
                cur["biz_status"] if cur else None,
                retired,
                revive,
            )
            guard = {"decision": action}
            if reason:
                guard["reason"] = reason
            if cur and ldc.is_retired(cur["biz_status"], retired):
                guard["retired_current"] = True
            if not cur:
                changes.append(
                    {
                        "store_id": snap["store_id"],
                        "kind": "new",
                        "snapshot_id": snap["id"],
                        "from_hash": None,
                        "to_hash": snap["row_hash"],
                        **guard,
                    }
                )
            elif cur["row_hash"] != snap["row_hash"]:
                changes.append(
                    {
                        "store_id": snap["store_id"],
                        "kind": "attr_change",
                        "snapshot_id": snap["id"],
                        "from_hash": cur["row_hash"],
                        "to_hash": snap["row_hash"],
                        "from": {
                            "open_time": cur["open_time"],
                            "close_time": cur["close_time"],
                            "weekday_mask": cur["weekday_mask"],
                            "biz_status": cur["biz_status"],
                        },
                        "to": {
                            "open_time": snap["open_time"],
                            "close_time": snap["close_time"],
                            "weekday_mask": snap["weekday_mask"],
                            "biz_status": snap["biz_status"],
                        },
                        **guard,
                    }
                )
        report = {"changes": changes, "scanned": len(snaps)}
        conn.execute(
            """INSERT INTO detect_runs(id, batch_id, created_at, changed_count, report_json)
               VALUES (?,?,?,?,?)""",
            (
                run_id,
                batch_id,
                now,
                len(changes),
                json.dumps(report, ensure_ascii=False),
            ),
        )
        conn.commit()
        return {"detect_run_id": run_id, "changed_count": len(changes), "changes": changes}
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "store_id" in out and "biz_key" not in out:
        out["biz_key"] = out.get("store_id")
    if "hist_id" in out and "segment_ref" not in out:
        out["segment_ref"] = out.get("hist_id")
    if "row_hash" in out and "content_digest" not in out:
        out["content_digest"] = out.get("row_hash")
    if "aligned" not in out:
        out["aligned"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True, "scope": "hourspan"}
    if extra:
        data.update(extra)
    return data

