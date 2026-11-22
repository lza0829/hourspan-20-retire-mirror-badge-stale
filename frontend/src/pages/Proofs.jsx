import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [companion, setCompanion] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () => {
    api("/api/proofs").then(setRows).catch((e) => setErr(String(e.message || e)));
    Promise.resolve(api("/api/retire/badges/verify")).then(setCompanion).catch(() => setCompanion(null));
  };

  useEffect(() => { reload(); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div data-plant="20-retire-mirror-badge-stale" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>当前镜 hist_id / row_hash 必须与 is_current 历史行一致；失败须可观测。</p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建核证</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      
      <div className="panel">
        <h2>
          退役角标核验（只读 · 架构乙现场权威）{" "}
          {companion && (
            <span className={`badge ${companion.ok ? "ok" : "bad"}`}>
              {companion.ok ? "闭合" : `破闭 · 违例 ${companion.summary["违例"]}`}
            </span>
          )}
        </h2>
        {companion ? (
          <table>
            <thead>
              <tr><th>门店</th><th>角标</th><th>closed_hist_id</th><th>镜态</th><th>对齐</th></tr>
            </thead>
            <tbody>
              {companion.rows.map((r) => (
                <tr key={r.store_id}>
                  <td>{r.store_id}</td>
                  <td>
                    {r.badge ? (
                      <span className={`badge ${r.badge === "retired" ? "retired" : "ok"}`}>{r.badge}</span>
                    ) : (
                      <span className="badge bad">缺失</span>
                    )}
                  </td>
                  <td className="mono">{r.closed_hist_id || "—"}</td>
                  <td className="mono">{r.mirror_status || "无镜"}</td>
                  <td>
                    <span className={`badge ${r.aligned ? "ok" : "bad"}`}>
                      {r.aligned ? "一致" : "分叉"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">（空）</p>
        )}
      </div>
      <div className="panel">
        <h2>scd_proofs</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
