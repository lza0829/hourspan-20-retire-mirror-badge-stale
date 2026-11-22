#!/usr/bin/env python3
"""退役镜角标（retire_mirror_badge）全链路核验 · 架构乙「现场权威」

演练路径（夹具 + 人为改角标）：
  S0 基线：种子镜 → 全键可写角标，核证闭合
  S1 retire_close：退役闭段同事务卸角标（retired + closed_hist_id=退役段），邻键不串改
  S2 retire_sneak：二次入档依现场搁置（写闸只扫现场镜态），角标保持 retired
  S3 人为只改角标→writable：核证必失败 + 写闸不采信角标仍搁置（与架构甲可区分）
     + 应用整笔回滚（失败闭）→ sync 修复后管线恢复
  S4 人为指未闭/邻键/不存在 hist：核证逐类检出 → sync 修复
  S5 半截卸标失败：整笔回滚（镜/段/角标/搁置不留半截）
  S6 显式复活：唯一回可写路径，退役段闭合并写 valid_to
  S7 常规营业变更：角标保持可写，核证闭合

退出码：0=全过；1=有失败。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="hs_badge_verify_")
os.environ["DATA_DIR"] = TMP
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")
sys.path.insert(0, str(ROOT / "backend"))

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.modules import logical_delete_close as ldc  # noqa: E402
from app.modules import retire_mirror_badge as rbadge  # noqa: E402
from app.services import detect, ingest, scd  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(f"{name}: {detail}")


def badge_of(conn, sid):
    r = conn.execute(
        "SELECT * FROM mirror_write_badges WHERE store_id=?", (sid,)
    ).fetchone()
    return dict(r) if r else None


def mirror_of(conn, sid):
    r = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (sid,)
    ).fetchone()
    return dict(r) if r else None


def counts(conn):
    return {
        t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        for t in (
            "dim_store_hours_hist",
            "dim_store_hours_current",
            "quarantine",
            "mirror_write_badges",
        )
    }


def pipe(fixture, batch_scoped=True):
    """入档 → 检测 → 应用，返回 (apply_result, detect_result)。"""
    r = ingest.run_batch(fixture)
    d = detect.run_detect(r["batch_id"] if batch_scoped else None)
    a = scd.apply_type2(d["detect_run_id"])
    return a, d


print("== S0 基线：种子镜全键可写角标 ==")
seed.init_db()
conn = connect()
retired = ldc.retired_set(conn)
v = rbadge.verify_badges(conn, retired)
check("6 键角标齐备", counts(conn)["mirror_write_badges"] == 6)
check("全部 writable 且 closed_hist_id 为空",
      all(b["badge"] == "writable" and b["closed_hist_id"] is None
          for b in conn.execute("SELECT * FROM mirror_write_badges").fetchall()))
check("角标核证闭合", v["ok"], json.dumps(v["violations"], ensure_ascii=False))
check("退役核证闭合", ldc.verify(conn)["ok"])
conn.close()

print("\n== S1 retire_close：退役闭段同事务卸角标 ==")
conn = connect()
before_neighbors = {
    sid: badge_of(conn, sid)
    for sid in ("ST-EAST-02", "ST-SOUTH-01", "ST-WEST-02", "ST-NORTH-01")
}
conn.close()
a, _ = pipe("retire_close_batch.json")
check("应用成功", a.get("ok") is True, json.dumps(a, ensure_ascii=False))
check("两键 badge_unloaded=True",
      all(x.get("badge_unloaded") for x in a.get("applied", [])))
conn = connect()
for sid in ("ST-EAST-01", "ST-WEST-01"):
    b, m = badge_of(conn, sid), mirror_of(conn, sid)
    check(f"{sid} 角标=retired", b and b["badge"] == "retired", str(b))
    check(f"{sid} closed_hist_id=退役当前段（与镜互证）",
          b and m and b["closed_hist_id"] == m["hist_id"])
    check(f"{sid} closed_hist_id 非空且时刻已写",
          bool(b and b["closed_hist_id"] and b["updated_at"]))
    seg = conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE id=?", (m["hist_id"],)
    ).fetchone()
    check(f"{sid} 角标所指段=退役当前段(is_current=1, 态∈退役集)",
          bool(seg and seg["is_current"] == 1 and seg["biz_status"] in retired))
    open_cur = conn.execute(
        """SELECT COUNT(*) AS c FROM dim_store_hours_hist
           WHERE store_id=? AND is_current=1 AND biz_status NOT IN ('closed','retired')""",
        (sid,),
    ).fetchone()["c"]
    check(f"{sid} 营业 is_current 段为空（互证）", open_cur == 0)
for sid, prev in before_neighbors.items():
    check(f"邻键 {sid} 角标未被串改", badge_of(conn, sid) == prev,
          f"{prev} -> {badge_of(conn, sid)}")
v = rbadge.verify_badges(conn, retired)
check("角标核证闭合", v["ok"], json.dumps(v["violations"], ensure_ascii=False))
check("退役核证闭合", ldc.verify(conn)["ok"])
conn.close()

print("\n== S2 retire_sneak：二次入档依现场搁置，角标保持 retired ==")
a, _ = pipe("retire_sneak_batch.json")
check("应用成功（搁置留因）", a.get("ok") is True, json.dumps(a, ensure_ascii=False))
check("两快照均搁置 retired_no_explicit_revival",
      a.get("shelved_count") == 2
      and all(s["reason"] == "retired_no_explicit_revival" for s in a.get("shelved", [])))
conn = connect()
check("搁置后角标仍 retired（未漂移）",
      all(badge_of(conn, s)["badge"] == "retired"
          for s in ("ST-EAST-01", "ST-WEST-01")))
check("镜保持退役态",
      all(mirror_of(conn, s)["biz_status"] in retired
          for s in ("ST-EAST-01", "ST-WEST-01")))
conn.close()

print("\n== S3 人为只改角标→writable：核证必失败；写闸不采信角标（架构乙↔甲可区分） ==")
conn = connect()
conn.execute(
    "UPDATE mirror_write_badges SET badge='writable', closed_hist_id=NULL "
    "WHERE store_id='ST-EAST-01'"
)
conn.commit()
v = rbadge.verify_badges(conn, retired)
kinds = {x["kind"] for x in v["violations"]}
check("角标核证必须失败", not v["ok"])
check("检出 retired_mirror_badge_still_writable",
      "retired_mirror_badge_still_writable" in kinds, str(kinds))
check("退役核证同步失败（不变量含角标）", not ldc.verify(conn)["ok"])
# 写闸只扫现场：角标说可写，判定仍搁置 —— 架构甲（角标权威）此处会放行，可区分
action, _reason = ldc.decide("open", 0, "closed", retired, "open")
check("写闸不采信角标：人为可写角标下仍 shelve（依现场）", action == "shelve")
d = detect.run_detect(None)
east_chg = [c for c in d["changes"] if c["store_id"] == "ST-EAST-01"]
check("检测预判仍 shelve（角标分叉不影响写闸）",
      bool(east_chg) and east_chg[0]["decision"] == "shelve",
      json.dumps(east_chg, ensure_ascii=False))
pre = counts(conn)
a = scd.apply_type2(d["detect_run_id"])
check("分叉下应用整笔回滚（失败闭）", a.get("ok") is False)
check("回滚违例含角标类",
      any("badge" in str(x.get("kind", "")) for x in a.get("violations", [])),
      json.dumps(a.get("violations", []), ensure_ascii=False))
check("回滚后 hist/镜/搁置/角标计数不变", counts(conn) == pre,
      f"{pre} -> {counts(conn)}")
check("角标仍被篡改态（回滚不掩篡改，留待修复）",
      badge_of(conn, "ST-EAST-01")["badge"] == "writable")
r = rbadge.sync_from_mirror(conn)
check("sync 以现场为权威修复角标", r["ok"] and r["repaired"] >= 1)
check("修复后角标回 retired 且核证闭合",
      badge_of(conn, "ST-EAST-01")["badge"] == "retired"
      and rbadge.verify_badges(conn, retired)["ok"])
conn.close()
a = scd.apply_type2(d["detect_run_id"])
check("修复后管线恢复（搁置落库）", a.get("ok") is True and a.get("shelved_count") >= 1)

print("\n== S4 人为指未闭/邻键/不存在 hist：核证逐类检出 ==")
conn = connect()
south_hist = mirror_of(conn, "ST-SOUTH-01")["hist_id"]
west_hist = mirror_of(conn, "ST-WEST-01")["hist_id"]
conn.execute(
    "UPDATE mirror_write_badges SET closed_hist_id=? WHERE store_id='ST-SOUTH-01'",
    (south_hist,),
)
conn.commit()
kinds = {x["kind"] for x in rbadge.verify_badges(conn, retired)["violations"]}
check("可写角标挂 closed_hist_id 被检出", "writable_badge_with_closed_hist" in kinds, str(kinds))
check("角标指向未闭 hist 被检出", "badge_points_at_unclosed_hist" in kinds, str(kinds))
conn.execute(
    "UPDATE mirror_write_badges SET badge='retired' WHERE store_id='ST-SOUTH-01'"
)
conn.commit()
kinds = {x["kind"] for x in rbadge.verify_badges(conn, retired)["violations"]}
check("营业镜挂退役角标被检出", "open_mirror_badge_still_retired" in kinds, str(kinds))
check("退役角标与营业 is_current 段并存被检出",
      "retired_badge_with_open_current_segment" in kinds, str(kinds))
conn.execute(
    "UPDATE mirror_write_badges SET closed_hist_id=? WHERE store_id='ST-EAST-01'",
    (west_hist,),
)
conn.commit()
kinds = {x["kind"] for x in rbadge.verify_badges(conn, retired)["violations"]}
check("角标指向邻键 hist 被检出", "badge_hist_cross_store" in kinds, str(kinds))
conn.execute(
    "UPDATE mirror_write_badges SET closed_hist_id='HIST-NOPE' WHERE store_id='ST-EAST-01'"
)
conn.commit()
kinds = {x["kind"] for x in rbadge.verify_badges(conn, retired)["violations"]}
check("角标指向不存在 hist 被检出", "badge_hist_orphan" in kinds, str(kinds))
r = rbadge.sync_from_mirror(conn)
check("sync 修复全部分叉且核证闭合", r["ok"] and rbadge.verify_badges(conn, retired)["ok"])
conn.close()

print("\n== S5 半截卸标失败：整笔回滚 ==")
r = ingest.run_batch("retire_revive_batch.json")
d = detect.run_detect(r["batch_id"])
conn = connect()
pre = counts(conn)
pre_mirror = mirror_of(conn, "ST-EAST-01")
pre_badge = badge_of(conn, "ST-EAST-01")
conn.close()
orig_upsert = rbadge.upsert_badge


def boom(*args, **kwargs):
    raise RuntimeError("模拟半截卸标失败")


rbadge.upsert_badge = boom
try:
    a = scd.apply_type2(d["detect_run_id"])
finally:
    rbadge.upsert_badge = orig_upsert
check("卸标异常 → 应用失败", a.get("ok") is False, json.dumps(a, ensure_ascii=False))
conn = connect()
check("镜未半截刷成营业（回滚）", mirror_of(conn, "ST-EAST-01") == pre_mirror)
check("角标未半截改写（回滚）", badge_of(conn, "ST-EAST-01") == pre_badge)
check("hist/镜/搁置/角标计数不变（无半截落库）", counts(conn) == pre,
      f"{pre} -> {counts(conn)}")
conn.close()

print("\n== S6 显式复活：唯一回可写路径 ==")
a = scd.apply_type2(d["detect_run_id"])
check("复活应用成功", a.get("ok") is True, json.dumps(a, ensure_ascii=False))
check("applied 标记 revived=True",
      any(x.get("revived") for x in a.get("applied", [])))
conn = connect()
b, m = badge_of(conn, "ST-EAST-01"), mirror_of(conn, "ST-EAST-01")
check("镜回营业 open", m["biz_status"] == "open")
check("角标回 writable 且 closed_hist_id 清空",
      b["badge"] == "writable" and b["closed_hist_id"] is None)
old_seg = conn.execute(
    """SELECT * FROM dim_store_hours_hist
       WHERE store_id='ST-EAST-01' AND biz_status IN ('closed','retired')
       ORDER BY valid_from DESC LIMIT 1"""
).fetchone()
check("退役段已闭合（valid_to + is_current=0）",
      old_seg["valid_to"] is not None and old_seg["is_current"] == 0)
check("核证闭合", rbadge.verify_badges(conn, retired)["ok"] and ldc.verify(conn)["ok"])
conn.close()

print("\n== S7 常规营业变更：角标保持可写 ==")
a, _ = pipe("ingest_change_batch.json")
check("营业变更应用成功", a.get("ok") is True, json.dumps(a, ensure_ascii=False))
conn = connect()
check("营业键角标保持 writable",
      all(badge_of(conn, s)["badge"] == "writable"
          for s in ("ST-EAST-01", "ST-SOUTH-01")))
check("核证闭合", rbadge.verify_badges(conn, retired)["ok"] and ldc.verify(conn)["ok"])
conn.close()

print("\n" + ("=" * 60))
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"ALL PASS · 架构乙（现场权威）· 数据目录 {TMP}")
