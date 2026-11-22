import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Current() {
  const [rows, setRows] = useState([]);
  const [badgeInfo, setBadgeInfo] = useState(null);
  const [err, setErr] = useState("");

  const reload = () => {
    api("/api/current").then(setRows).catch((e) => setErr(String(e.message || e)));
    api("/api/retire/badges/verify").then(setBadgeInfo).catch(() => setBadgeInfo(null));
  };

  useEffect(() => { reload(); }, []);

  const badgeOf = (sid) => badgeInfo?.rows?.find((b) => b.store_id === sid);

  return (
    <>
      <div data-plant="20-retire-mirror-badge-stale" className="plant-mark" style={{ display: "none" }} />
      <div className="topbar">
        <div>
          <h1>当前镜</h1>
          <p>
            dim_store_hours_current：每店一行，指向 is_current 历史行。
            镜侧角标为同事务投影（架构乙·现场权威）：退役镜↔retired、营业镜↔writable，分叉即核证失败。
          </p>
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      {badgeInfo && !badgeInfo.ok && (
        <div className="panel">
          <h2>
            角标核验{" "}
            <span className="badge bad">破闭 · 违例 {badgeInfo.summary["违例"]}</span>
          </h2>
          <p className="muted">
            角标与现场分叉（写闸不采信角标，仍按现场判定；请到「退役」页查看违例并以现场为准修复）。
          </p>
          <table>
            <thead><tr><th>kind</th><th>门店</th><th>细节</th></tr></thead>
            <tbody>
              {badgeInfo.violations.map((v, i) => (
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
        <h2>
          当前镜 × 镜侧角标{" "}
          {badgeInfo && (
            <span className={`badge ${badgeInfo.ok ? "ok" : "bad"}`}>
              {badgeInfo.ok ? "角标核验闭合" : "角标核验破闭"}
            </span>
          )}
        </h2>
        <table>
          <thead>
            <tr>
              <th>门店</th><th>开-闭</th><th>mask</th><th>状态</th><th>角标</th>
              <th>hist_id</th><th>as_of</th><th>hash</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const b = badgeOf(r.store_id);
              return (
                <tr key={r.store_id}>
                  <td>{r.store_id}</td>
                  <td>{r.open_time}–{r.close_time}</td>
                  <td className="mono">{r.weekday_mask}</td>
                  <td><span className={`badge ${r.biz_status}`}>{r.biz_status}</span></td>
                  <td>
                    {b ? (
                      <span
                        className={`badge ${b.badge === "retired" ? "retired" : "ok"}`}
                        title={b.closed_hist_id ? `closed_hist_id=${b.closed_hist_id}` : "可写角标"}
                      >
                        {b.badge}
                      </span>
                    ) : (
                      <span className="badge bad">缺失</span>
                    )}
                  </td>
                  <td className="mono">{r.hist_id}</td>
                  <td>{r.as_of_ts}</td>
                  <td className="mono">{r.row_hash}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
