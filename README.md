# 时档（HourSpan）

**骨架：** 门店营业属性贴源入档 → 业务键变更检测 → Type2 历史行开闭 → 当前镜投影 → 历史与当前互证

- 领域：数据工程（去皮=维表 SCD / 历史开闭 / 当前镜 / 核证；不是门店后台 CRUD）
- 端口：前端 **6200**，接口 **11200**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/hourspan-NN-*`
- 栈：Python 3.12 + FastAPI + SQLite；前端 **React + Vite**
- 避让：勿撞筑层分层汇总、价舱改价调度、信汇多源去重、变流 CDC 游标、格湾占用态

本地冒烟：

```powershell
cd claude-workspace/hourspan/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ../frontend
npm install
npm run dev
```

或：`docker compose up -d --build`（6200 / 11200）

## 维态退役管道（logical_delete_close，主链路）

业务状态入约定退役集（`settings.retire_status_set`，默认 `closed,retired`）即触发维态退役，
不是改文案、不是物理删 hist。

- **落库形态（甲）**：hist 退役态 + is_current 规则。退役 = `dim_store_hours_hist` 里一段
  `biz_status∈退役集`、`is_current=1` 的正式历史段；前一营业段写 `valid_to`+`is_current=0` 关闭；
  当前镜指向退役段、镜态=退役态。退役段即退役痕迹（`valid_from`=退役时刻、`closed_by_run`=关段跑次）。
- **失败闭（甲）**：关段 / 退役段 / 刷镜 / 卸角标 / 搁置留因同一事务，提交前跑不变量
  （禁双当前、关闭段必有 `valid_to`、镜↔当前段一致、退役镜不得挂营业当前段、角标↔现场互证），
  违例整体回滚。
- **复活守卫**：键已退役后，仅快照**显式** `biz_status=open`（入档记 `biz_status_explicit`，
  缺省补的 open 不算）才准写回营业有效镜；其余一律进 `quarantine` 留因，维与镜保持退役。
- **只读核证**：`GET /api/retirements`（退役段 × 镜态 × 拒绝原因 × 全局不变量），前端「退役」页展示。

## 退役镜角标（retire_mirror_badge）

镜侧可写/退役角标（`mirror_write_badges`，每店一行）：`writable`=营业镜投影、键可入档；
`retired`=退役镜投影、可写角标已卸，`closed_hist_id`=镜所指退役当前段 id，
与「该键营业 is_current 段为空、镜态∈退役集」互证。

- **架构抉择（全局唯一，乙·现场权威）**：入档写闸只扫现场 `is_current`/镜态
  （`logical_delete_close.decide`），**不采信角标**；角标是镜态在同事务内落库的投影，
  仅供展示与核证。角标与现场分叉 → 核证必须失败。
  推论：无显式复活时，角标漂移/篡改无法把退役键重新可写——写闸根本不看角标。
- **同事务卸标**：退役闭段成功时，`scd.apply_type2` 在同一事务内逐键卸可写角标并写
  退役角标快照（`store_id`、`closed_hist_id`、`badge=retired`、时刻）；提交前不变量含
  角标全量检项，缺一角标即整笔回滚，半截失败不留残态。
- **硬约束**：禁闭段成功而角标仍可写；禁角标指向未闭/邻键/不存在 hist；
  禁无显式复活靠角标漂移重新可写；禁邻键角标被串改（逐键 upsert，只动本键）。
- **只读核证接口（新增）**：`GET /api/retire/badges/verify` —— 角标 × 现场镜态 ×
  互证检项（`retired_mirror_badge_still_writable` / `open_mirror_badge_still_retired` /
  `retired_badge_hist_mismatch` / `badge_points_at_unclosed_hist` / `badge_hist_cross_store` /
  `badge_hist_orphan` / `badge_missing` / `badge_without_mirror` / …）。
  `GET /api/retire/badges` 为角标清单；`POST /api/retire/badges/sync` 为唯一修复路径
  （以现场镜态为权威重投影，绝不把退役镜的键漂回 writable）。
- **协同面**：退役闭段（scd）→ 入档写闸（decide，只看现场）→ 当前镜页角标列 →
  退役页「退役角标核验」面板 + 核证页角标核验表，四处同一口径。

夹具验证路径（入档 → 检测带 `batch_id` → 历史维应用 Type2）：

1. `retire_close_batch.json`：ST-EAST-01→closed、ST-WEST-01→retired；两店退役段落库、镜转退役、
   **同事务卸角标**（`badge=retired`、`closed_hist_id`=退役段），邻键角标不动。
2. `retire_sneak_batch.json`：无 `biz_status` 字段的营业快照 + `suspended` 快照回灌已退役键
   → 必须搁置留因（`quarantine.reason=retired_no_explicit_revival`），镜/历史/角标保持退役可证。
3. `retire_revive_batch.json`：显式 `biz_status=open` → 关退役段、开新营业段、镜回营业、
   角标回 `writable`（唯一回可写路径）。
4. 人为只改角标（如把退役键角标改回 `writable`）：`GET /api/retire/badges/verify` 必失败，
   写闸仍按现场搁置（架构乙；架构甲此处会放行，两架构结论可区分），应用整笔回滚，
   `POST /api/retire/badges/sync` 修复后管线恢复。

一键演练（含上述 1–4 与半截失败回滚）：`python scripts/verify_retire_badge.py`
