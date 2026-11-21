STATUS = "partial"


def ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mirror_write_badges(
             store_id TEXT PRIMARY KEY,
             badge TEXT NOT NULL,
             closed_hist_id TEXT,
             updated_at TEXT
           )"""
    )


def sync_from_mirror(conn) -> dict:
    ensure_schema(conn)
    mirrors = conn.execute("SELECT * FROM dim_store_hours_current").fetchall()
    # 退役后仍把角标留在 writable，展示与写闸旁路未卸
    for m in mirrors:
        conn.execute(
            """INSERT INTO mirror_write_badges(store_id, badge, closed_hist_id, updated_at)
               VALUES (?,?,?,datetime('now'))
               ON CONFLICT(store_id) DO UPDATE SET
                 badge=excluded.badge,
                 closed_hist_id=excluded.closed_hist_id,
                 updated_at=excluded.updated_at""",
            (m["store_id"], "writable", None),
        )
    conn.commit()
    rows = [dict(r) for r in conn.execute("SELECT * FROM mirror_write_badges").fetchall()]
    return {"badges": rows, "unloaded": 0}


def list_badges(conn) -> dict:
    ensure_schema(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM mirror_write_badges").fetchall()]
    if not rows:
        return sync_from_mirror(conn)
    return {"badges": rows, "unloaded": sum(1 for r in rows if r["badge"] == "retired")}
