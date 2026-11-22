"""维态退役管道 · logical_delete_close

业务状态进入约定退役集（settings.retire_status_set，默认 closed/retired）时，
键不得再被当作营业有效：必须关现行段、落退役段、刷镜，并对无显式复活标记的
后续快照实施搁置留因。

落库形态（择一，本题取 **甲**）：
  甲、hist 退役态 + is_current 规则 —— 退役是 dim_store_hours_hist 里一段
      biz_status∈退役集 且 is_current=1 的正式历史段；前一营业段同事务写
      valid_to + is_current=0 关闭；当前镜指向退役段、镜态=退役态。
      退役段本身即退役痕迹（谁关的看 closed_by_run，何时退的看 valid_from）。
      不物理删 hist，不建影子文案。

失败闭（择一，本题取 **甲**）：
  甲、同事务 —— 关段 / 退役段 / 刷镜 / 搁置留因在同一 SQLite 事务内，
      提交前跑 check_invariants，任一违例整体回滚，返回 violations。
      禁止镜已退役仍另有 is_current 营业段；禁止退役成功却无 valid_to。

复活：仅当快照显式 biz_status=open（source_snapshots.biz_status_explicit=1，
由入档按"快照 JSON 是否自带该字段"判定）才允许把已退役键写回营业有效镜；
其余状态（含缺省补的 open）一律进 quarantine 留因，镜与历史保持退役态。

只读核证 verify()：退役段 + 镜态 + 拒绝原因（quarantine）+ 全局不变量，
供 GET /api/retirements 与前端退役页使用；全程只 SELECT。

角标协同（retire_mirror_badge，架构乙·现场权威）：写闸 decide() 只扫现场镜态，
不采信角标；角标是闭段同事务内落库的镜态投影。check_invariants 第 5 组检项
直接复用 retire_mirror_badge.badge_violations —— 闭段成功而角标仍可写、
角标指向未闭/邻键 hist、退役角标与营业 is_current 段并存，任一即整体回滚。
"""

from app.modules import retire_mirror_badge as rbadge

DEFAULT_RETIRED_SET = ("closed", "retired")
DEFAULT_REVIVE_STATUS = "open"


def retired_set(conn) -> frozenset:
    """约定退役集：settings.retire_status_set 逗号分隔，缺省 closed,retired。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key='retire_status_set'"
    ).fetchone()
    raw = (row["value"] if row else "") or ""
    items = {x.strip() for x in raw.split(",") if x.strip()}
    return frozenset(items or DEFAULT_RETIRED_SET)


def revive_status(conn) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='revive_status'"
    ).fetchone()
    return (row["value"].strip() if row and row["value"] else "") or DEFAULT_REVIVE_STATUS


def is_retired(status, retired) -> bool:
    return (status or "") in retired


def decide(snap_status, snap_explicit, mirror_status, retired, revive):
    """退役守卫（纯函数，检测页预判与入档应用强制执行共用同一判定）。

    返回 (action, reason)：action ∈ {"apply", "shelve"}。
    镜未退役 → apply（含首次退役本身）；镜已退役 → 仅退役态延续或显式复活放行，
    其余 shelve 并给原因。
    """
    if mirror_status is None or not is_retired(mirror_status, retired):
        return "apply", None
    if is_retired(snap_status, retired):
        return "apply", None  # 退役态延续（重复退役/退役集内迁移）
    if snap_status == revive and snap_explicit:
        return "apply", None  # 显式复活
    if snap_status == revive and not snap_explicit:
        why = "快照缺省未带 biz_status，不算显式复活标记"
    else:
        why = f"快照 biz_status={snap_status!r}，非显式 {revive!r} 复活"
    return "shelve", (
        f"键已退役（镜态 {mirror_status}∈退役集），{why}；"
        f"不得写回营业有效镜，搁置留因"
    )


SHELVE_REASON = "retired_no_explicit_revival"


def check_invariants(conn, retired) -> list:
    """失败闭不变量（事务提交前跑；违例即整体回滚）。只 SELECT。"""
    violations = []
    # 1) 禁止双当前：每店 is_current=1 至多一段
    for r in conn.execute(
        """SELECT store_id, COUNT(*) AS c FROM dim_store_hours_hist
           WHERE is_current=1 GROUP BY store_id HAVING c>1"""
    ).fetchall():
        violations.append(
            {"kind": "double_current", "store_id": r["store_id"], "count": r["c"]}
        )
    # 2) 关闭段必须有 valid_to（禁止退役成功却无 valid_to）
    for r in conn.execute(
        "SELECT id, store_id FROM dim_store_hours_hist WHERE is_current=0 AND valid_to IS NULL"
    ).fetchall():
        violations.append(
            {"kind": "closed_segment_without_valid_to", "store_id": r["store_id"], "hist_id": r["id"]}
        )
    # 3) 镜 ↔ is_current 段一致；镜退役则当前段必须退役，镜营业则当前段不得退役
    mirrors = conn.execute("SELECT * FROM dim_store_hours_current").fetchall()
    for m in mirrors:
        h = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
            (m["store_id"],),
        ).fetchone()
        if not h:
            violations.append(
                {"kind": "mirror_without_current_segment", "store_id": m["store_id"]}
            )
            continue
        if h["id"] != m["hist_id"] or h["row_hash"] != m["row_hash"]:
            violations.append(
                {
                    "kind": "mirror_segment_mismatch",
                    "store_id": m["store_id"],
                    "mirror_hist_id": m["hist_id"],
                    "current_hist_id": h["id"],
                }
            )
        mirror_retired = is_retired(m["biz_status"], retired)
        seg_retired = is_retired(h["biz_status"], retired)
        if mirror_retired and not seg_retired:
            violations.append(
                {
                    "kind": "retired_mirror_with_open_current_segment",
                    "store_id": m["store_id"],
                    "hist_id": h["id"],
                    "hist_status": h["biz_status"],
                }
            )
        if not mirror_retired and seg_retired:
            violations.append(
                {
                    "kind": "open_mirror_with_retired_current_segment",
                    "store_id": m["store_id"],
                    "hist_id": h["id"],
                    "hist_status": h["biz_status"],
                }
            )
    # 4) is_current 段必须有镜行承接（每店一镜）
    for r in conn.execute(
        """SELECT h.store_id, h.id FROM dim_store_hours_hist h
           LEFT JOIN dim_store_hours_current c ON c.store_id=h.store_id
           WHERE h.is_current=1 AND c.store_id IS NULL"""
    ).fetchall():
        violations.append(
            {"kind": "current_segment_without_mirror", "store_id": r["store_id"], "hist_id": r["id"]}
        )
    # 5) 角标 ↔ 现场互证（同事务卸标缺一回滚；禁角标仍可写/指未闭/串邻键/漂移回可写）
    violations.extend(rbadge.badge_violations(conn, retired))
    return violations


def verify(conn) -> dict:
    """退役核证（只读）：退役段 + 镜态 + 拒绝原因 + 角标 + 全局不变量。"""
    rbadge.ensure_schema(conn)
    retired = retired_set(conn)
    revive = revive_status(conn)
    placeholders = ",".join("?" * len(retired))
    segs = conn.execute(
        f"""SELECT * FROM dim_store_hours_hist
            WHERE biz_status IN ({placeholders})
            ORDER BY store_id, valid_from, id""",
        tuple(sorted(retired)),
    ).fetchall()
    segments = []
    seg_bad = 0
    for s in segs:
        mirror = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?", (s["store_id"],)
        ).fetchone()
        checks = []
        if s["is_current"]:
            ok = bool(
                mirror
                and mirror["hist_id"] == s["id"]
                and mirror["biz_status"] == s["biz_status"]
                and is_retired(mirror["biz_status"], retired)
            )
            checks.append({"check": "退役当前段须被镜承接且镜态退役", "ok": ok})
        else:
            checks.append({"check": "退役历史段须已写 valid_to", "ok": s["valid_to"] is not None})
        if not all(c["ok"] for c in checks):
            seg_bad += 1
        segments.append(
            {
                "segment": dict(s),
                "mirror": dict(mirror) if mirror else None,
                "checks": checks,
            }
        )
    quarantine = [
        dict(r)
        for r in conn.execute("SELECT * FROM quarantine ORDER BY id DESC").fetchall()
    ]
    violations = check_invariants(conn, retired)
    badge_badges = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM mirror_write_badges ORDER BY store_id"
        ).fetchall()
    ]
    return {
        "retired_set": sorted(retired),
        "revive_status": revive,
        "shelve_reason": SHELVE_REASON,
        "segments": segments,
        "quarantine": quarantine,
        "violations": violations,
        "badges": badge_badges,
        "ok": not violations and seg_bad == 0,
        "summary": {
            "退役段": len(segments),
            "退役段违例": seg_bad,
            "全局违例": len(violations),
            "角标违例": sum(1 for v in violations if str(v.get("kind", "")).startswith("badge") or "badge" in str(v.get("kind", ""))),
            "搁置": len(quarantine),
        },
    }
