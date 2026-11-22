"""退役镜角标 · retire_mirror_badge

架构抉择（全局唯一，取 **乙 · 现场权威**）：
  以现场 is_current/镜态为入档写闸权威 —— 写闸（logical_delete_close.decide）
  只扫现场镜态，绝不采信角标；角标是镜态在同事务内落库的投影，仅供展示与核证。
  角标与现场分叉 → 核证必须失败（badge_violations 同时供事务内不变量与只读核证）。
  推论：无显式复活时，角标漂移/篡改无法把退役键重新可写——写闸根本不看角标。

角标语义（mirror_write_badges，每店一行）：
  - writable：营业镜投影，键可入档；closed_hist_id 必须为 NULL。
  - retired ：退役镜投影，可写角标已卸；closed_hist_id = 镜所指退役当前段 id，
    与「该键营业 is_current 段为空、镜态∈退役集」互证。

事务纪律（失败闭甲）：
  - 退役闭段 / 卸可写角标 / 写退役角标快照同库同事务（scd.apply_type2 逐键投影），
    提交前 check_invariants 含角标全量检项，缺一角标即整体回滚，不许半截落库；
  - 本模块写函数只执行 SQL、不 commit，事务边界归连接属主；
  - sync_from_mirror 是唯一修复路径：以现场镜态为权威重投影全部角标
    （退役镜 → retired，营业镜 → writable），绝不把退役镜的键漂回 writable。
"""

BADGE_WRITABLE = "writable"
BADGE_RETIRED = "retired"

SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS mirror_write_badges(
  store_id TEXT PRIMARY KEY,
  badge TEXT NOT NULL,
  closed_hist_id TEXT,
  updated_at TEXT
)"""


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA_SQL)


def desired_badge(mirror_status, mirror_hist_id, retired) -> tuple:
    """纯函数：现场镜态 → 应有角标（退役镜→(retired, 段id)；营业镜→(writable, NULL)）。"""
    if (mirror_status or "") in retired:
        return BADGE_RETIRED, mirror_hist_id
    return BADGE_WRITABLE, None


def upsert_badge(conn, store_id, badge, closed_hist_id, now) -> None:
    """单键角标投影：只动本键（邻键不串改）；不 commit，与闭段同事务。"""
    conn.execute(
        """INSERT INTO mirror_write_badges(store_id, badge, closed_hist_id, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(store_id) DO UPDATE SET
             badge=excluded.badge,
             closed_hist_id=excluded.closed_hist_id,
             updated_at=excluded.updated_at""",
        (store_id, badge, closed_hist_id, now),
    )


def project_for_mirror(conn, mirror, retired, now) -> tuple:
    """按镜行重投影该键角标（同事务内调用），返回 (badge, closed_hist_id)。"""
    badge, closed_hist_id = desired_badge(mirror["biz_status"], mirror["hist_id"], retired)
    upsert_badge(conn, mirror["store_id"], badge, closed_hist_id, now)
    return badge, closed_hist_id


def sync_from_mirror(conn) -> dict:
    """修复/对账（路由级，自带 commit）：以现场镜态为权威重投影全部角标。

    只增改本键角标、清理无镜孤儿角标；退役镜的键只会投影为 retired，
    不会借本路径漂移回 writable。返回逐键动作与修复后仍存的违例。
    """
    ensure_schema(conn)
    retired = _retired_set(conn)
    now = _now_iso(conn)
    mirrors = conn.execute(
        "SELECT * FROM dim_store_hours_current ORDER BY store_id"
    ).fetchall()
    before = {
        r["store_id"]: r
        for r in conn.execute("SELECT * FROM mirror_write_badges").fetchall()
    }
    actions = []
    for m in mirrors:
        badge, closed_hist_id = desired_badge(m["biz_status"], m["hist_id"], retired)
        prev = before.get(m["store_id"])
        changed = (
            prev is None
            or prev["badge"] != badge
            or prev["closed_hist_id"] != closed_hist_id
        )
        if changed:
            upsert_badge(conn, m["store_id"], badge, closed_hist_id, now)
            actions.append(
                {
                    "store_id": m["store_id"],
                    "from": (
                        {"badge": prev["badge"], "closed_hist_id": prev["closed_hist_id"]}
                        if prev
                        else None
                    ),
                    "to": {"badge": badge, "closed_hist_id": closed_hist_id},
                }
            )
    # 孤儿角标（镜已不在）整键清理，不触碰他键
    orphans = conn.execute(
        """DELETE FROM mirror_write_badges
           WHERE store_id NOT IN (SELECT store_id FROM dim_store_hours_current)"""
    ).rowcount
    violations = badge_violations(conn, retired)
    conn.commit()
    rows = [dict(r) for r in conn.execute("SELECT * FROM mirror_write_badges").fetchall()]
    return {
        "ok": not violations,
        "repaired": len(actions),
        "orphans_purged": orphans,
        "actions": actions,
        "violations": violations,
        "badges": rows,
        "unloaded": sum(1 for r in rows if r["badge"] == BADGE_RETIRED),
    }


def list_badges(conn) -> dict:
    """只读：角标 × 现场镜态（LEFT JOIN 双向，缺角标/孤儿角标均可见）。"""
    ensure_schema(conn)
    retired = _retired_set(conn)
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT m.store_id,
                      b.badge, b.closed_hist_id, b.updated_at,
                      m.biz_status AS mirror_status, m.hist_id AS mirror_hist_id
               FROM dim_store_hours_current m
               LEFT JOIN mirror_write_badges b ON b.store_id = m.store_id
               UNION ALL
               SELECT b.store_id,
                      b.badge, b.closed_hist_id, b.updated_at,
                      NULL AS mirror_status, NULL AS mirror_hist_id
               FROM mirror_write_badges b
               WHERE b.store_id NOT IN (SELECT store_id FROM dim_store_hours_current)
               ORDER BY 1"""
        ).fetchall()
    ]
    for r in rows:
        want_badge, want_hist = (
            desired_badge(r["mirror_status"], r["mirror_hist_id"], retired)
            if r["mirror_status"] is not None
            else (None, None)
        )
        r["expected_badge"] = want_badge
        r["expected_closed_hist_id"] = want_hist
        r["aligned"] = (
            r["badge"] == want_badge and r["closed_hist_id"] == want_hist
        )
    return {
        "badges": rows,
        "unloaded": sum(1 for r in rows if r["badge"] == BADGE_RETIRED),
    }


def badge_violations(conn, retired) -> list:
    """角标 ↔ 现场互证检项（事务内不变量与只读核证共用同一套）。只 SELECT。

    覆盖硬约束：闭段成功而角标仍可写 / 角标指向未闭 hist /
    无显式复活角标漂移回可写 / 邻键角标被串改 / 退役角标与营业 is_current 段并存。
    """
    ensure_schema(conn)
    violations = []
    mirrors = {
        m["store_id"]: m
        for m in conn.execute("SELECT * FROM dim_store_hours_current").fetchall()
    }
    badges = {
        b["store_id"]: b
        for b in conn.execute("SELECT * FROM mirror_write_badges").fetchall()
    }
    hists = {
        h["id"]: h
        for h in conn.execute(
            "SELECT id, store_id, biz_status, is_current, valid_to FROM dim_store_hours_hist"
        ).fetchall()
    }
    placeholders = ",".join("?" * len(retired))

    # 1) 每镜必有角标，且角标 == 现场应有投影（值与 closed_hist_id 都核对）
    for store_id, m in mirrors.items():
        want_badge, want_hist = desired_badge(m["biz_status"], m["hist_id"], retired)
        b = badges.get(store_id)
        if b is None:
            violations.append(
                {
                    "kind": "badge_missing",
                    "store_id": store_id,
                    "detail": "镜行存在但角标缺失（闭段同事务卸标缺一回滚的残留）",
                    "expected_badge": want_badge,
                }
            )
            continue
        if b["badge"] != want_badge:
            violations.append(
                {
                    "kind": (
                        "retired_mirror_badge_still_writable"
                        if want_badge == BADGE_RETIRED
                        else "open_mirror_badge_still_retired"
                    ),
                    "store_id": store_id,
                    "mirror_status": m["biz_status"],
                    "badge": b["badge"],
                    "expected_badge": want_badge,
                    "detail": (
                        "镜已退役而角标仍可写（禁闭段成功而角标仍可写）"
                        if want_badge == BADGE_RETIRED
                        else "镜已营业而退役角标未卸（禁无显式复活外的角标漂移）"
                    ),
                }
            )
        elif want_badge == BADGE_RETIRED and b["closed_hist_id"] != want_hist:
            violations.append(
                {
                    "kind": "retired_badge_hist_mismatch",
                    "store_id": store_id,
                    "closed_hist_id": b["closed_hist_id"],
                    "expected_hist_id": want_hist,
                    "detail": "退役角标快照必须指向镜所承接的退役当前段（与镜态互证）",
                }
            )
        elif want_badge == BADGE_WRITABLE and b["closed_hist_id"] is not None:
            violations.append(
                {
                    "kind": "writable_badge_with_closed_hist",
                    "store_id": store_id,
                    "closed_hist_id": b["closed_hist_id"],
                    "detail": "可写角标不得挂 closed_hist_id（退役快照只属退役角标）",
                }
            )

    # 2) 角标引用完整性：不得指向未闭 / 不存在 / 邻键 hist；退役角标与营业当前段互斥
    for store_id, b in badges.items():
        cid = b["closed_hist_id"]
        if b["badge"] == BADGE_RETIRED:
            open_cur = conn.execute(
                f"""SELECT id FROM dim_store_hours_hist
                    WHERE store_id=? AND is_current=1
                      AND biz_status NOT IN ({placeholders})""",
                (store_id, *sorted(retired)),
            ).fetchone()
            if open_cur:
                violations.append(
                    {
                        "kind": "retired_badge_with_open_current_segment",
                        "store_id": store_id,
                        "hist_id": open_cur["id"],
                        "detail": "退役角标键仍存营业 is_current 段（与 is_current 空互证失败）",
                    }
                )
        if cid is None:
            continue
        h = hists.get(cid)
        if h is None:
            violations.append(
                {
                    "kind": "badge_hist_orphan",
                    "store_id": store_id,
                    "closed_hist_id": cid,
                    "detail": "角标指向不存在的 hist",
                }
            )
            continue
        if h["store_id"] != store_id:
            violations.append(
                {
                    "kind": "badge_hist_cross_store",
                    "store_id": store_id,
                    "closed_hist_id": cid,
                    "hist_store_id": h["store_id"],
                    "detail": "角标指向邻键 hist（禁邻键角标被串改）",
                }
            )
            continue
        if h["is_current"] == 1 and (h["biz_status"] or "") not in retired:
            violations.append(
                {
                    "kind": "badge_points_at_unclosed_hist",
                    "store_id": store_id,
                    "closed_hist_id": cid,
                    "hist_status": h["biz_status"],
                    "detail": "角标指向未闭 hist（营业当前段），禁角标指向未闭 hist",
                }
            )

    # 3) 孤儿角标：镜已不在而角标犹存
    for store_id in badges:
        if store_id not in mirrors:
            violations.append(
                {
                    "kind": "badge_without_mirror",
                    "store_id": store_id,
                    "detail": "角标存在但无镜行承接",
                }
            )
    return violations


def verify_badges(conn, retired) -> dict:
    """退役角标核证（只读）：角标 × 现场镜态 × 互证检项。全程只 SELECT。"""
    ensure_schema(conn)
    listed = list_badges(conn)
    rows = []
    for r in listed["badges"]:
        checks = []
        if r["mirror_status"] is None:
            checks.append({"check": "角标须有镜行承接", "ok": False})
        else:
            checks.append({"check": "角标须有镜行承接", "ok": True})
            checks.append(
                {
                    "check": "角标值须等于现场应有投影（退役↔retired / 营业↔writable）",
                    "ok": r["badge"] == r["expected_badge"],
                }
            )
            checks.append(
                {
                    "check": "closed_hist_id 须与镜所指段互证（退役段id / 营业为NULL）",
                    "ok": r["closed_hist_id"] == r["expected_closed_hist_id"],
                }
            )
        rows.append({**r, "checks": checks})
    violations = badge_violations(conn, retired)
    return {
        "authority": "onsite_is_current",
        "architecture": (
            "乙·现场权威：入档写闸只扫现场 is_current/镜态；角标为同事务投影，"
            "仅展示+核证；角标与现场分叉时本核证必须失败"
        ),
        "rows": rows,
        "violations": violations,
        "ok": not violations,
        "summary": {
            "角标": len(rows),
            "已卸(retired)": sum(1 for r in rows if r["badge"] == BADGE_RETIRED),
            "可写(writable)": sum(1 for r in rows if r["badge"] == BADGE_WRITABLE),
            "违例": len(violations),
        },
    }


def _retired_set(conn) -> frozenset:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='retire_status_set'"
    ).fetchone()
    raw = (row["value"] if row else "") or ""
    items = {x.strip() for x in raw.split(",") if x.strip()}
    return frozenset(items or ("closed", "retired"))


def _now_iso(conn) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now') AS t").fetchone()["t"]
