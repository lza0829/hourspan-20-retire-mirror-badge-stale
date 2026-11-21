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
- **失败闭（甲）**：关段 / 退役段 / 刷镜 / 搁置留因同一事务，提交前跑不变量
  （禁双当前、关闭段必有 `valid_to`、镜↔当前段一致、退役镜不得挂营业当前段），违例整体回滚。
- **复活守卫**：键已退役后，仅快照**显式** `biz_status=open`（入档记 `biz_status_explicit`，
  缺省补的 open 不算）才准写回营业有效镜；其余一律进 `quarantine` 留因，维与镜保持退役。
- **只读核证**：`GET /api/retirements`（退役段 × 镜态 × 拒绝原因 × 全局不变量），前端「退役」页展示。

夹具验证路径（入档 → 检测带 `batch_id` → 历史维应用 Type2）：

1. `retire_close_batch.json`：ST-EAST-01→closed、ST-WEST-01→retired；两店退役段落库、镜转退役。
2. `retire_sneak_batch.json`：无 `biz_status` 字段的营业快照 + `suspended` 快照回灌已退役键
   → 必须搁置留因（`quarantine.reason=retired_no_explicit_revival`），镜与历史保持退役可证。
3. `retire_revive_batch.json`：显式 `biz_status=open` → 关退役段、开新营业段、镜回营业（复活另测）。
