import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Retire() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  const reload = () =>
    api("/api/retirements").then(setData).catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reload(); }, []);

  const parseMember = (q) => {
    try { return JSON.parse(q.member_json); } catch { return {}; }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>退役核证</h1>
          <p>
            维态退役管道（只读）：biz_status∈退役集 → 同事务关段+退役段+刷镜；
            无显式 open 复活标记的快照必搁置留因。退役段即退役痕迹，不物理删 hist。
          </p>
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      {data && (
        <>
          <div className="grid-cards">
            <div className="card">
              <div className="label">退役集</div>
              <div className="value mono">{data.retired_set.join(" / ")}</div>
            </div>
            <div className="card">
              <div className="label">复活标记</div>
              <div className="value mono">显式 {data.revive_status}</div>
            </div>
            <div className="card">
              <div className="label">退役段</div>
              <div className="value">{data.summary["退役段"]}</div>
            </div>
            <div className="card">
              <div className="label">搁置</div>
              <div className="value">{data.summary["搁置"]}</div>
            </div>
            <div className="card">
              <div className="label">全局违例</div>
              <div className="value">{data.summary["全局违例"]}</div>
            </div>
            <div className="card">
              <div className="label">管道态</div>
              <div className="value">
                <span className={`badge ${data.ok ? "ok" : "bad"}`}>
                  {data.ok ? "闭合" : "破闭"}
                </span>
              </div>
            </div>
          </div>

          <div className="panel">
            <h2>验证路径（夹具）</h2>
            <p className="muted mono">
              retire_close_batch.json（营业→退役）→ retire_sneak_batch.json（无复活标记，必搁置）
              → retire_revive_batch.json（显式 open 复活）。入档 → 检测（带 batch_id）→ 应用 Type2。
            </p>
          </div>

          {data.violations.length > 0 && (
            <div className="panel">
              <h2>全局违例（失败闭应已回滚，出现即事故）</h2>
              <table>
                <thead><tr><th>kind</th><th>门店</th><th>细节</th></tr></thead>
                <tbody>
                  {data.violations.map((v, i) => (
                    <tr key={i}>
                      <td className="mono">{v.kind}</td>
                      <td>{v.store_id || "—"}</td>
                      <td className="mono">{JSON.stringify(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="panel">
            <h2>退役段 × 镜态</h2>
            <table>
              <thead>
                <tr>
                  <th>历史段</th><th>门店</th><th>状态</th><th>valid_from</th><th>valid_to</th>
                  <th>当前</th><th>镜态</th><th>镜 hist</th><th>段检</th>
                </tr>
              </thead>
              <tbody>
                {data.segments.map(({ segment: s, mirror, checks }) => (
                  <tr key={s.id}>
                    <td className="mono">{s.id}</td>
                    <td>{s.store_id}</td>
                    <td><span className={`badge ${s.biz_status}`}>{s.biz_status}</span></td>
                    <td>{s.valid_from}</td>
                    <td>{s.valid_to || "—"}</td>
                    <td>{s.is_current ? "是" : "否"}</td>
                    <td>
                      {mirror ? (
                        <span className={`badge ${mirror.biz_status}`}>{mirror.biz_status}</span>
                      ) : (
                        <span className="badge bad">无镜</span>
                      )}
                    </td>
                    <td className="mono">{mirror ? mirror.hist_id : "—"}</td>
                    <td>
                      {checks.map((c) => (
                        <span key={c.check} className={`badge ${c.ok ? "ok" : "bad"}`} title={c.check}>
                          {c.ok ? "✓" : "✗"} {c.check}
                        </span>
                      ))}
                    </td>
                  </tr>
                ))}
                {data.segments.length === 0 && (
                  <tr><td colSpan="9" className="muted">尚无退役段：跑 retire_close_batch.json 入档→检测→应用</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="panel">
            <h2>搁置队列（拒绝原因留证）</h2>
            <table>
              <thead>
                <tr><th>id</th><th>reason</th><th>门店</th><th>快照</th><th>镜态→快照态</th><th>说明</th><th>时间</th></tr>
              </thead>
              <tbody>
                {data.quarantine.map((q) => {
                  const m = parseMember(q);
                  return (
                    <tr key={q.id}>
                      <td>{q.id}</td>
                      <td className="mono">{q.reason}</td>
                      <td>{m.store_id || "—"}</td>
                      <td className="mono">{m.snapshot_id || "—"}</td>
                      <td className="mono">
                        {m.mirror_status || "—"} → {m.snap_status || "—"}
                        {m.snap_explicit === 0 ? "（缺省）" : ""}
                      </td>
                      <td>{m.detail || q.member_json}</td>
                      <td>{q.created_at}</td>
                    </tr>
                  );
                })}
                {data.quarantine.length === 0 && (
                  <tr><td colSpan="7" className="muted">暂无搁置：跑 retire_sneak_batch.json 验证拒绝路径</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="panel">
            <div className="actions">
              <button className="secondary" onClick={reload}>刷新</button>
            </div>
          </div>
        </>
      )}
    </>
  );
}
