// 主页交直流+氢能全景能量流渲染 & 秦岭 SVG 接线图缩放平移与实时遥测叠加

function setEfNode(id, { cur, sub, meta, dir, ringPct }) {
  const el = document.getElementById(id);
  if (!el) return;
  const setText = (sel, txt) => {
    const node = el.querySelector(sel);
    if (node) node.textContent = txt;
  };
  if (cur !== undefined) setText("[data-ef-cur]", cur);
  if (sub !== undefined) setText("[data-ef-sub]", sub);
  if (meta !== undefined) setText("[data-ef-meta]", meta);
  if (dir !== undefined) setText("[data-ef-dir]", dir);
  if (ringPct !== undefined) {
    const ring = el.querySelector("[data-ef-ring]");
    if (ring) {
      const r = Number(ring.getAttribute("r") || 20);
      const circ = 2 * Math.PI * r;
      const clamped = Math.max(0, Math.min(100, Number(ringPct) || 0));
      ring.setAttribute("stroke-dasharray", circ.toFixed(2));
      ring.setAttribute("stroke-dashoffset", (circ * (1 - clamped / 100)).toFixed(2));
    }
  }
}

function setEfFlow(pathId, powerKw, { reverse = false, refKw = 120 } = {}) {
  const path = document.getElementById(pathId);
  if (!path) return;
  const mag = Math.abs(Number(powerKw) || 0);
  path.classList.remove("is-forward", "is-reverse");
  if (mag < 0.25) {
    path.style.strokeWidth = "2";
    return;
  }
  const norm = Math.max(0, Math.min(1, mag / Math.max(10, refKw)));
  const dur = (2.1 - norm * 1.45).toFixed(2);
  const width = (2.2 + norm * 2.4).toFixed(2);
  path.style.setProperty("--ef-dur", `${dur}s`);
  path.style.strokeWidth = width;
  path.classList.add(reverse ? "is-reverse" : "is-forward");
}

function pctRatio(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return 0;
  const n = Number(v);
  return Math.abs(n) <= 1.5 ? n * 100 : n;
}

function renderOverviewPage() {
  const snap = AGCState.snapshot;
  if (!snap) return;
  const agc = snap.agc_status || {};
  const env = snap.environment || {};
  const latest = snap.latest_strategy || (AGCState.control && AGCState.control.latest) || null;
  const m = (latest && latest.metrics) || {};

  // 若尚未运行策略，从实时遥测估算基础汇总
  const scada = (snap.measurements && snap.measurements.scada) || [];
  const validMeas = scada.filter((x) => Number(x.valid) === 1).length;

  let loadKw = m.loadKw;
  let acLoadKw = m.acLoadKw;
  let dcLoadKw = m.dcLoadKw;
  let acWindCur = m.acWindCurrentKw;
  let dcWindCur = m.dcWindCurrentKw;
  let acPvCur = m.acPvCurrentKw;
  let dcPvCur = m.dcPvCurrentKw;
  let acStCur = m.acStorageCurrentKw;
  let dcStCur = m.dcStorageCurrentKw;
  let dieselCur = m.totalDieselCurrentKw ?? m.dieselCurrentKw;
  let acdcCur = m.acdcCurrentKw;

  if (loadKw === undefined) {
    acLoadKw = scada.filter((x) => x.dev_type === "ACLoad" && x.meas_type === "P_LOAD" && x.valid).reduce((a, b) => a + Number(b.value || 0), 0);
    dcLoadKw = scada.filter((x) => x.dev_type === "DCLoad" && x.meas_type === "P_LOAD" && x.valid).reduce((a, b) => a + Number(b.value || 0), 0);
    loadKw = acLoadKw + dcLoadKw;
  }

  const renTotal = (Number(acWindCur || 0) + Number(dcWindCur || 0) + Number(acPvCur || 0) + Number(dcPvCur || 0));
  const stTotal = Number(acStCur || 0) + Number(dcStCur || 0);
  const dieselVal = Number(dieselCur || 0);
  const totalSupply = Math.max(0.01, renTotal + Math.max(0, dieselVal) + Math.max(0, stTotal) + Math.max(0, Number(m.fuelCellCurrentKw || 0)));
  const greenShare = Math.min(100, (renTotal / totalSupply) * 100);

  document.getElementById("ovTotalLoad").textContent = fmtNum(loadKw, 1);
  document.getElementById("ovTotalRenewable").textContent = fmtNum(renTotal, 1);
  document.getElementById("ovGreenShare").textContent = fmtNum(greenShare, 1);
  document.getElementById("ovTotalStorage").textContent = fmtNum(stTotal, 1);
  document.getElementById("ovTotalDiesel").textContent = fmtNum(dieselVal, 1);
  document.getElementById("ovMeasCount").textContent = String(validMeas);
  document.getElementById("ovAgeText").textContent = `${fmtNum(agc.age_seconds, 1)}s`;

  // 矢量能量流各节点、环形 SOC 与动态支路
  const dcGenSum = Number(dcWindCur || 0) + Number(dcPvCur || 0) + Math.max(0, Number(dcStCur || 0));
  const acGenSum = Number(acWindCur || 0) + Number(acPvCur || 0) + Math.max(0, Number(acStCur || 0)) + Math.max(0, dieselVal);
  const dcSumEl = document.getElementById("efDcSummary");
  if (dcSumEl) dcSumEl.textContent = `发 ${fmtNum(dcGenSum, 1)} / 荷 ${fmtNum(dcLoadKw, 1)} kW`;
  const acSumEl = document.getElementById("efAcSummary");
  if (acSumEl) acSumEl.textContent = `发 ${fmtNum(acGenSum, 1)} / 荷 ${fmtNum(acLoadKw, 1)} kW`;

  const loadTotalSafe = Math.max(0.01, Number(loadKw || 0));
  const dcLoadPct = `${fmtNum((Number(dcLoadKw || 0) / loadTotalSafe) * 100, 1)}%`;
  const acLoadPct = `${fmtNum((Number(acLoadKw || 0) / loadTotalSafe) * 100, 1)}%`;

  setEfNode("efDcWind", {
    cur: `${fmtNum(dcWindCur, 1)} kW`,
    sub: `目标 ${fmtNum(m.dcWindTargetKw, 1)} · 可发 ${fmtNum(m.dcWindMaxAvailableKw, 1)}`,
    meta: " · 10台"
  });
  setEfFlow("efFlowDcWind", dcWindCur, { refKw: 80 });

  setEfNode("efDcPv", {
    cur: `${fmtNum(dcPvCur, 1)} kW`,
    sub: `目标 ${fmtNum(m.dcPvTargetKw, 1)} · 可发 ${fmtNum(m.dcPvMaxAvailableKw, 1)}`,
    meta: " · 3台"
  });
  setEfFlow("efFlowDcPv", dcPvCur, { refKw: 80 });

  const dcStVal = Number(dcStCur || 0);
  setEfNode("efDcStorage", {
    cur: `${fmtNum(dcStCur, 1)} kW`,
    sub: `目标 ${fmtNum(m.dcStorageTargetKw, 1)} · ${dcStVal > 0.2 ? "放电" : dcStVal < -0.2 ? "充电" : "静置"}`,
    meta: ` · SOC ${fmtPct(m.dcStorageSoc)}`,
    ringPct: pctRatio(m.dcStorageSoc)
  });
  setEfFlow("efFlowDcStorage", dcStVal, { reverse: dcStVal < 0, refKw: 120 });

  setEfNode("efDcLoad", {
    cur: `${fmtNum(dcLoadKw, 1)} kW`,
    sub: `占总负荷 ${dcLoadPct}`,
    meta: " · 750V"
  });
  setEfFlow("efFlowDcLoad", dcLoadKw, { refKw: 150 });

  setEfNode("efAcWind", {
    cur: `${fmtNum(acWindCur, 1)} kW`,
    sub: `目标 ${fmtNum(m.acWindTargetKw, 1)} · 可发 ${fmtNum(m.acWindMaxAvailableKw, 1)}`,
    meta: " · 1台"
  });
  setEfFlow("efFlowAcWind", acWindCur, { refKw: 60 });

  setEfNode("efAcPv", {
    cur: `${fmtNum(acPvCur, 1)} kW`,
    sub: `目标 ${fmtNum(m.acPvTargetKw, 1)} · 可发 ${fmtNum(m.acPvMaxAvailableKw, 1)}`,
    meta: " · 1台"
  });
  setEfFlow("efFlowAcPv", acPvCur, { refKw: 60 });

  const acStVal = Number(acStCur || 0);
  setEfNode("efAcStorage", {
    cur: `${fmtNum(acStCur, 1)} kW`,
    sub: `目标 ${fmtNum(m.acStorageTargetKw, 1)} · ${acStVal > 0.2 ? "放电" : acStVal < -0.2 ? "充电" : "静置"}`,
    meta: ` · SOC ${fmtPct(m.acStorageSoc)}`,
    ringPct: pctRatio(m.acStorageSoc)
  });
  setEfFlow("efFlowAcStorage", acStVal, { reverse: acStVal < 0, refKw: 150 });

  setEfNode("efDiesel", {
    cur: `${fmtNum(dieselVal, 1)} kW`,
    sub: `目标 ${fmtNum(m.totalDieselTargetKw ?? m.dieselTargetKw, 1)} kW`,
    meta: ` · 下限 ${fmtNum(m.totalDieselMinKw ?? m.dieselMinKw, 0)}`
  });
  setEfFlow("efFlowDiesel", dieselVal, { refKw: 260 });

  setEfNode("efAcLoad", {
    cur: `${fmtNum(acLoadKw, 1)} kW`,
    sub: `占总负荷 ${acLoadPct}`,
    meta: " · 380V"
  });
  setEfFlow("efFlowAcLoad", acLoadKw, { refKw: 300 });

  const acdcNum = Number(acdcCur || 0);
  const acdcDirTxt = acdcNum > 0.2 ? "DC → AC" : acdcNum < -0.2 ? "AC → DC" : "平衡";
  setEfNode("efAcdc", {
    cur: `${fmtNum(acdcCur, 1)} kW`,
    sub: `${acdcDirTxt} · 目标 ${fmtNum(m.acdcTargetKw, 1)}`,
    meta: " · 2×300 kW",
    dir: acdcNum > 0.2 ? "━━▶" : acdcNum < -0.2 ? "◀━━" : "◀━▶"
  });
  setEfFlow("efFlowAcdcL", acdcNum, { reverse: acdcNum < 0, refKw: 120 });
  setEfFlow("efFlowAcdcR", acdcNum, { reverse: acdcNum < 0, refKw: 120 });

  const elzVal = Number(m.electrolyzerCurrentKw || 0);
  const fcVal = Number(m.fuelCellCurrentKw || 0);
  setEfNode("efElectrolyzer", {
    cur: `${fmtNum(elzVal, 1)} kW`,
    meta: `${fmtNum(m.electrolyzerFlowCurrentNm3h, 2)} Nm³/h`
  });
  setEfFlow("efFlowElzIn", elzVal, { refKw: 100 });
  setEfFlow("efFlowElzH2", elzVal, { refKw: 100 });

  setEfNode("efHydroTank", {
    cur: fmtPct(m.hydrogenStorageSoc),
    meta: `${fmtNum(m.hydrogenStoragePressureMpa, 2)} MPa`,
    ringPct: pctRatio(m.hydrogenStorageSoc)
  });

  setEfNode("efFuelCell", {
    cur: `${fmtNum(fcVal, 1)} kW`,
    meta: `${fmtNum(m.fuelCellFlowCurrentNm3h, 2)} Nm³/h`
  });
  setEfFlow("efFlowFcH2", fcVal, { refKw: 80 });
  setEfFlow("efFlowFcOut", fcVal, { refKw: 80 });

  document.getElementById("efGreenPower").textContent = `${fmtNum(renTotal, 1)} kW`;
  document.getElementById("efGreenShare").textContent = `${fmtNum(greenShare, 1)}%`;
  const dqOk = !latest || latest.data_quality === "ok";
  const dqDot = document.getElementById("efQualityDot");
  if (dqDot) {
    dqDot.classList.toggle("is-ok", dqOk);
    dqDot.classList.toggle("is-warn", !dqOk);
  }
  const dqBadge = document.getElementById("efDataQualityBadge");
  if (dqBadge) {
    dqBadge.classList.toggle("is-warn", !dqOk);
    dqBadge.textContent = dqOk ? "OK" : String(latest.data_quality || "DEGRADED").toUpperCase();
  }

  // 环境与数据源
  document.getElementById("ovSampleTime").textContent = (env.sample_timestamp || "").replace("T", " ").slice(11) || "--";
  document.getElementById("ovWindSpeed").textContent = `${fmtNum(env.wind_speed, 2)} m/s`;
  document.getElementById("ovSolarIrr").textContent = `${fmtNum(env.solar_irradiance, 1)} W/m²`;
  document.getElementById("ovAirTemp").textContent = `${fmtNum(env.air_temperature, 1)} °C`;
  const model = snap.model || {};
  document.getElementById("ovModelDb").textContent = (model.model_db || "devices.sqlite3").split("\\").pop();
  document.getElementById("ovModelDb").title = model.model_db || "";
  document.getElementById("ovScadaDb").textContent = (model.scada_db || "scdfe_d.sqlite3").split("\\").pop();
  document.getElementById("ovScadaDb").title = model.scada_db || "";
  document.getElementById("ovAgcDb").textContent = (agc.agc_db || "agc.sqlite3").split("\\").pop();
  document.getElementById("ovAgcDb").title = agc.agc_db || "";
  const sc = (snap.static_meta && snap.static_meta.scada_command) || {};
  document.getElementById("ovScadaCmd").textContent = sc.available ? `YT:${sc.yt_count} / YK:${sc.yk_count}` : "--";

  // 决策摘要与变动指令
  document.getElementById("ovDecisionCycle").textContent = `#${latest ? latest.cycle : 0}`;
  const decWrap = document.getElementById("ovDecisionList");
  const lines = (latest && latest.decision_detail) || [];
  if (!lines.length) {
    decWrap.innerHTML = `<div class="muted" style="padding:12px;">点击“单次计算”或启动自动控制以生成首轮策略</div>`;
  } else {
    decWrap.innerHTML = lines.map((l) => `<div class="decision-line">${escapeHtml(l)}</div>`).join("");
  }

  const changedRows = ((latest && latest.command_rows) || []).filter((r) => r.changed);
  document.getElementById("ovChangedCount").textContent = String(changedRows.length);
  const tblWrap = document.getElementById("ovChangedTable");
  if (!changedRows.length) {
    tblWrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">当前周期无变动指令</td></tr></tbody></table>`;
  } else {
    tblWrap.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>分类</th>
          <th>${t("col.device")}</th>
          <th class="num">${t("lbl.current")} (kW)</th>
          <th class="num">${t("lbl.target")} (kW)</th>
          <th class="num">ΔP (kW)</th>
          <th>状态</th>
          <th>策略说明</th>
        </tr></thead>
        <tbody>
          ${changedRows.map((r) => `
            <tr>
              <td>${escapeHtml(r.category || "--")}</td>
              <td><strong>${escapeHtml(r.dev_name)}</strong></td>
              <td class="num">${fmtNum(r.current_kw, 2)}</td>
              <td class="num">${fmtNum(r.target_kw, 2)}</td>
              <td class="num ${Number(r.delta_kw) >= 0 ? "val-pos" : "val-neg"}">${Number(r.delta_kw) >= 0 ? "+" : ""}${fmtNum(r.delta_kw, 2)}</td>
              <td><span class="badge ${r.dispatch_status === "dispatched" ? "badge-ok" : "badge-info"}">${escapeHtml(r.dispatch_status)}</span></td>
              <td class="ellipsis" title="${escapeHtml(r.status_label || "")}">${escapeHtml(r.status_label || "--")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>`;
  }
}

// =========================================================================
// SVG 接线图加载、缩放平移与实时量测叠加
// =========================================================================
async function ensureDiagramLoaded() {
  if (AGCState.diagramLoaded) {
    updateDiagramTelemetryOverlay();
    return;
  }
  const stage = document.getElementById("diagramStage");
  if (!stage) return;
  try {
    const resp = await fetch("/api/diagram/svg");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const svgText = await resp.text();
    stage.innerHTML = svgText;
    const svgEl = stage.querySelector("svg");
    if (svgEl) {
      const vb = (svgEl.getAttribute("viewBox") || "0 0 2518 2143").split(/[\s,]+/).map(Number);
      svgEl.setAttribute("width", String(vb[2] || 2518));
      svgEl.setAttribute("height", String(vb[3] || 2143));
      const ns = "http://www.w3.org/2000/svg";
      const flowLayer = document.createElementNS(ns, "g");
      flowLayer.setAttribute("id", "agcFlowAnimationLayer");
      svgEl.appendChild(flowLayer);
      const overlay = document.createElementNS(ns, "g");
      overlay.setAttribute("id", "agcTelemetryOverlayLayer");
      svgEl.appendChild(overlay);
      svgEl.addEventListener("click", handleDiagramElementClick);
    }
    AGCState.diagramLoaded = true;
    AGCState._lastFlowSignature = "";
    fitDiagramToViewport();
    updateDiagramTelemetryOverlay();
  } catch (err) {
    stage.innerHTML = `<div style="padding:24px;" class="muted">接线图加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function applyDiagramTransform() {
  const stage = document.getElementById("diagramStage");
  if (!stage) return;
  stage.style.transform = `translate(${AGCState.diagramTx}px, ${AGCState.diagramTy}px) scale(${AGCState.diagramScale})`;
  const txt = document.getElementById("diagramZoomText");
  if (txt) txt.textContent = `${Math.round(AGCState.diagramScale * 100)}%`;
}

function fitDiagramToViewport() {
  const vp = document.getElementById("diagramViewport");
  const svgEl = document.querySelector("#diagramStage svg");
  if (!vp || !svgEl) return;
  const vw = vp.clientWidth || 1000;
  const vh = vp.clientHeight || 680;
  const sw = Number(svgEl.getAttribute("width")) || 2518;
  const sh = Number(svgEl.getAttribute("height")) || 2143;
  const scale = Math.min((vw - 24) / sw, (vh - 24) / sh);
  AGCState.diagramScale = Math.max(0.15, Math.min(2, scale));
  AGCState.diagramTx = (vw - sw * AGCState.diagramScale) / 2;
  AGCState.diagramTy = (vh - sh * AGCState.diagramScale) / 2;
  applyDiagramTransform();
}

// =========================================================================
// SVG 实时潮流与氢气流向动画引擎（按功率/流量大小与方向动态决定箭头尺寸与流速）
// =========================================================================
function parseSvgPolylinePoints(dText) {
  const nums = (String(dText || "").match(/[-+]?\d*\.?\d+/g) || []).map(Number);
  const pts = [];
  for (let i = 0; i + 1 < nums.length; i += 2) {
    const x = nums[i];
    const y = nums[i + 1];
    if (!pts.length || Math.hypot(x - pts[pts.length - 1][0], y - pts[pts.length - 1][1]) > 0.5) {
      pts.push([x, y]);
    }
  }
  return pts;
}

function pointsToPathD(pts) {
  if (!pts.length) return "";
  return pts.map((p, idx) => `${idx === 0 ? "M" : "L"} ${p[0]} ${p[1]}`).join(" ");
}

function polylineLength(pts) {
  let len = 0;
  for (let i = 1; i < pts.length; i++) {
    len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  }
  return len;
}

function flowTierStyle(magnitude, isHydrogen) {
  const absVal = Math.abs(Number(magnitude) || 0);
  const norm = isHydrogen
    ? Math.log10(1 + absVal / 0.5) / Math.log10(1 + 20 / 0.5)
    : Math.log10(1 + absVal / 2.5) / Math.log10(1 + 220 / 2.5);
  const clamped = Math.max(0, Math.min(1, norm));
  const tier = Math.max(1, Math.min(5, Math.ceil(clamped * 5)));
  // 5 档平滑缩放：功率越大，箭头越大、线宽越粗、行进越快
  const table = [
    { scale: 0.78, strokeWidth: 3.2, speedPx: 36, dashDur: 2.3 },
    { scale: 0.96, strokeWidth: 4.0, speedPx: 58, dashDur: 1.6 },
    { scale: 1.16, strokeWidth: 4.8, speedPx: 88, dashDur: 1.15 },
    { scale: 1.38, strokeWidth: 5.8, speedPx: 125, dashDur: 0.8 },
    { scale: 1.62, strokeWidth: 6.8, speedPx: 168, dashDur: 0.55 }
  ];
  return { tier, ...table[tier - 1] };
}

function buildDiagramFlowContext(snapshot) {
  const devices = snapshot.devices || [];
  const scada = (snapshot.measurements && snapshot.measurements.scada) || [];
  const devByKey = new Map(); // "ACGenerator-1" -> dev
  const measByDevKey = new Map(); // "ACGenerator-1" -> { P_GEN, P_LOAD, P_AC, P_DC, FLOW, ... }
  const measByTypeName = new Map();

  scada.forEach((m) => {
    if (!m.valid || m.value === null || m.value === undefined) return;
    const k = `${m.dev_type}|${m.dev_name}`;
    if (!measByTypeName.has(k)) measByTypeName.set(k, {});
    measByTypeName.get(k)[m.meas_type] = Number(m.value);
  });

  // 节点 -> 关联设备列表（用于沿断路器非母线端追溯真实电源/负荷/变流器）
  const acNodeDevs = new Map();
  const dcNodeDevs = new Map();
  const pushNode = (map, nodeId, devId) => {
    if (nodeId === undefined || nodeId === null || nodeId === "") return;
    const k = String(nodeId);
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(devId);
  };

  devices.forEach((d) => {
    const devId = `${d.dev_type}-${d.idx}`;
    devByKey.set(devId, d);
    const m = measByTypeName.get(`${d.dev_type}|${d.dev_name}`) || {};
    measByDevKey.set(devId, m);
    const raw = d.raw || {};
    const blk = d.dev_type;
    if (blk.startsWith("AC") || blk === "AcE2Hydro") {
      pushNode(acNodeDevs, raw.node, devId);
      pushNode(acNodeDevs, raw.i_node, devId);
      pushNode(acNodeDevs, raw.j_node, devId);
    }
    if (blk.startsWith("DC") || blk === "Hydro2DcE") {
      if (blk === "DCACConverter") {
        pushNode(acNodeDevs, raw.ac_node, devId);
        pushNode(dcNodeDevs, raw.dc_node, devId);
      } else {
        pushNode(dcNodeDevs, raw.node, devId);
        pushNode(dcNodeDevs, raw.i_node, devId);
        pushNode(dcNodeDevs, raw.j_node, devId);
      }
    }
  });

  function isClosed(devId) {
    const d = devByKey.get(devId);
    if (!d) return true;
    if (d.dev_type === "ACBreak" || d.dev_type === "DCBreak") {
      const m = measByDevKey.get(devId) || {};
      const pos = m.CLOSED_STATUS !== undefined ? m.CLOSED_STATUS : d.closed_status;
      return Number(pos ?? 1) !== 0 && Number(d.run_stat ?? 1) !== 0;
    }
    return Number(d.run_stat ?? 1) !== 0;
  }

  // 解析某条 Segment 路径（sourceId -> targetId）的有功功率（kW）或氢流量（Nm³/h）
  // 返回 { value: signedNumber, kind: "ac" | "dc" | "hydro" }，正值表示沿 source -> target 流动
  function resolveSegmentFlow(sourceId, targetId) {
    if (!sourceId || !targetId) return null;

    // 1. 氢能管道（AcE2Hydro-1 -> HydroStorage-1, Hydro2DcE-1 -> HydroStorage-1）
    if (sourceId.startsWith("AcE2Hydro-") || targetId.startsWith("AcE2Hydro-")) {
      const hId = sourceId.startsWith("AcE2Hydro-") ? sourceId : targetId;
      const m = measByDevKey.get(hId) || {};
      const flow = Number(m.FLOW ?? 0);
      const sign = sourceId === hId ? 1 : -1;
      return { value: flow * sign, kind: "hydro" };
    }
    if (sourceId.startsWith("Hydro2DcE-") || targetId.startsWith("Hydro2DcE-")) {
      const hId = sourceId.startsWith("Hydro2DcE-") ? sourceId : targetId;
      const m = measByDevKey.get(hId) || {};
      const flow = Number(m.FLOW ?? 0);
      // 燃料电池消耗储氢罐氢气：由 HydroStorage 流向 Hydro2DcE
      const sign = targetId === hId ? 1 : -1;
      return { value: flow * sign, kind: "hydro" };
    }

    if (!isClosed(sourceId) || !isClosed(targetId)) {
      return { value: 0, kind: sourceId.startsWith("DC") ? "dc" : "ac" };
    }

    const isDc = sourceId.startsWith("DC") || targetId.startsWith("DC");
    const kind = isDc ? "dc" : "ac";

    // 2. 发电机直接连断路器（如 ACGenerator-23 -> ACBreak-10）
    if (sourceId.startsWith("ACGenerator-") || sourceId.startsWith("DCGenerator-")) {
      const m = measByDevKey.get(sourceId) || {};
      return { value: Number(m.P_GEN ?? 0), kind };
    }

    // 3. 断路器直接连负荷（如 DCBreak-33 -> DCLoad-1）
    if (targetId.startsWith("DCLoad-") || targetId.startsWith("ACLoad-")) {
      const m = measByDevKey.get(targetId) || {};
      return { value: Number(m.P_LOAD ?? 0), kind };
    }

    // 4. 断路器连母线（DCBreak-* -> DCRealBs-1 或 ACBreak-* -> ACRealBs-1）
    if ((sourceId.startsWith("DCBreak-") && targetId.startsWith("DCRealBs-")) ||
        (sourceId.startsWith("ACBreak-") && targetId.startsWith("ACRealBs-"))) {
      const brk = devByKey.get(sourceId);
      const bus = devByKey.get(targetId);
      if (!brk || !bus) return null;
      const busNode = String((bus.raw || {}).node ?? "");
      const raw = brk.raw || {};
      const otherNode = String(raw.i_node) === busNode ? String(raw.j_node) : String(raw.i_node);
      const nodeMap = isDc ? dcNodeDevs : acNodeDevs;
      const neighbors = (nodeMap.get(otherNode) || []).filter((id) => id !== sourceId);

      // 沿单跳（含零阻抗支路/自适应支路）查找真实功率实体
      const visited = new Set([sourceId, targetId]);
      const queue = [...neighbors];
      while (queue.length) {
        const curId = queue.shift();
        if (visited.has(curId)) continue;
        visited.add(curId);
        const d = devByKey.get(curId);
        if (!d) continue;
        const m = measByDevKey.get(curId) || {};

        if (d.dev_type === "ACGenerator" || d.dev_type === "DCGenerator") {
          return { value: Number(m.P_GEN ?? 0), kind };
        }
        if (d.dev_type === "ACLoad" || d.dev_type === "DCLoad") {
          // 负荷从母线吸收功率：流向为母线 -> 断路器（负向）
          return { value: -Number(m.P_LOAD ?? 0), kind };
        }
        if (d.dev_type === "AcE2Hydro") {
          return { value: -Number(m.P_LOAD ?? 0), kind };
        }
        if (d.dev_type === "Hydro2DcE") {
          return { value: Number(m.P_GEN ?? 0), kind };
        }
        if (d.dev_type === "DCACConverter") {
          if (isDc) {
            // 直流侧：风机变流器 ac_p>0 送直流（dc_p=-3.08 流出到直流母线）；DCAC变流器 dc_p=+21.16 送直流母线
            const pVal = String(d.device_kind) === "acdc-converter" ? -Number(m.P_DC ?? 0) : Number(m.P_DC ?? 0);
            return { value: pVal, kind: "dc" };
          }
          // 交流侧：ac_p > 0 送入交流母线；ac_p < 0 从交流母线吸收
          return { value: Number(m.P_AC ?? 0), kind: "ac" };
        }
        if (d.dev_type === "DCDCConverter") {
          // 光伏变流器 j_p=-13.08（送直流母线），储能变流器 i_p=+0.12（吸收充电）或负值（放电送母线）
          const cRaw = d.raw || {};
          const atJ = String(cRaw.j_node) === otherNode;
          const pTerminal = atJ ? -Number(m.P_J ?? 0) : -Number(m.P_I ?? 0);
          return { value: pTerminal, kind: "dc" };
        }
        if (d.dev_type === "ACBranch" || d.dev_type === "DCBranch" || d.dev_type === "ACZeroBranch" || d.dev_type === "ACBreak" || d.dev_type === "DCBreak") {
          const bRaw = d.raw || {};
          const nextNode = String(bRaw.i_node) === otherNode ? String(bRaw.j_node) : String(bRaw.i_node);
          (nodeMap.get(nextNode) || []).forEach((nid) => {
            if (!visited.has(nid)) queue.push(nid);
          });
        }
      }
    }
    return null;
  }

  function resolveBranchUseFlow(devId) {
    const d = devByKey.get(devId);
    if (!d || !isClosed(devId)) return null;
    const m = measByDevKey.get(devId) || {};
    const kind = d.dev_type.startsWith("DC") ? "dc" : "ac";
    if (m.P_I !== undefined) {
      return { value: Number(m.P_I), kind };
    }
    return null;
  }

  return { resolveSegmentFlow, resolveBranchUseFlow };
}

function updateDiagramFlowAnimations() {
  const flowLayer = document.getElementById("agcFlowAnimationLayer");
  const stage = document.getElementById("diagramStage");
  if (!flowLayer || !stage || !AGCState.snapshot) return;

  const enabled = document.getElementById("diagramShowFlow")?.checked !== false;
  if (!enabled) {
    if (AGCState._lastFlowSignature !== "OFF") {
      flowLayer.innerHTML = "";
      AGCState._lastFlowSignature = "OFF";
    }
    return;
  }

  const webSet = (AGCState.runtimeSettings && AGCState.runtimeSettings.web) || {};
  const elecThreshold = Math.max(0, Number(webSet.diagram_flow_electric_threshold_kw ?? 0.1));
  const hydroThreshold = Math.max(0, Number(webSet.diagram_flow_hydrogen_threshold_nm3_h ?? 0.1));
  const dark = document.documentElement.dataset.colorScheme === "dark";

  const colorByKind = {
    dc: dark ? "#22D3EE" : "#0284C7",
    ac: dark ? "#FBBF24" : "#EA580C",
    hydro: dark ? "#34D399" : "#059669"
  };

  const ctx = buildDiagramFlowContext(AGCState.snapshot);
  const items = [];

  // 1. 收集 Segment_Layer 全部电/氢连接路径
  stage.querySelectorAll("#Segment_Layer path[d]").forEach((pEl) => {
    const id = pEl.getAttribute("id") || "";
    const src = pEl.getAttribute("source-dev-id") || "";
    const tgt = pEl.getAttribute("target-dev-id") || "";
    const flow = ctx.resolveSegmentFlow(src, tgt);
    if (!flow) return;
    const thr = flow.kind === "hydro" ? hydroThreshold : elecThreshold;
    if (Math.abs(flow.value) <= thr) return;

    const rawPts = parseSvgPolylinePoints(pEl.getAttribute("d"));
    if (rawPts.length < 2) return;
    const pts = flow.value >= 0 ? rawPts : [...rawPts].reverse();
    const len = polylineLength(pts);
    if (len < 12) return;
    const style = flowTierStyle(flow.value, flow.kind === "hydro");
    items.push({
      id: `seg:${id}`,
      d: pointsToPathD(pts),
      len,
      dir: flow.value >= 0 ? 1 : -1,
      kind: flow.kind,
      color: colorByKind[flow.kind],
      ...style
    });
  });

  // 2. 收集自适应支路（ACBranch / DCBranch / ACZeroBranch）的水平连线
  stage.querySelectorAll("#ACBranch_Layer use[dev-id], #DCBranch_Layer use[dev-id], #ACZeroBranch_Layer use[dev-id]").forEach((uEl) => {
    const devId = uEl.getAttribute("dev-id") || "";
    const flow = ctx.resolveBranchUseFlow(devId);
    if (!flow || Math.abs(flow.value) <= elecThreshold) return;
    const x = Number(uEl.getAttribute("x") || 0);
    const y = Number(uEl.getAttribute("y") || 0);
    const w = Number(uEl.getAttribute("width") || 150);
    const h = Number(uEl.getAttribute("height") || 36);
    const cy = y + h / 2;
    const pts = flow.value >= 0 ? [[x + 8, cy], [x + w - 8, cy]] : [[x + w - 8, cy], [x + 8, cy]];
    const len = Math.abs(w - 16);
    if (len < 12) return;
    const style = flowTierStyle(flow.value, false);
    items.push({
      id: `br:${devId}`,
      d: pointsToPathD(pts),
      len,
      dir: flow.value >= 0 ? 1 : -1,
      kind: flow.kind,
      color: colorByKind[flow.kind],
      ...style
    });
  });

  // 仅当方向、档位、主题或边集变化时重建 SVG 动画节点，防止轮询刷新闪烁
  const sig = `${dark ? "D" : "L"}|` + items.map((it) => `${it.id}:${it.dir}:${it.tier}`).join(",");
  if (sig === AGCState._lastFlowSignature) return;
  AGCState._lastFlowSignature = sig;

  flowLayer.innerHTML = "";
  const ns = "http://www.w3.org/2000/svg";

  items.forEach((it) => {
    const g = document.createElementNS(ns, "g");

    // 动态流光虚线层
    const line = document.createElementNS(ns, "path");
    line.setAttribute("d", it.d);
    line.setAttribute("class", "agc-flow-line");
    line.setAttribute("stroke", it.color);
    line.style.strokeWidth = `${it.strokeWidth}px`;
    line.style.setProperty("--flow-dur", `${it.dashDur}s`);
    g.appendChild(line);

    // 沿路径行进的实体燕尾箭头（按路径长度动态配置 1~3 个均匀错开箭头）
    const durSec = Math.max(0.45, it.len / it.speedPx);
    const arrowCount = it.len > 260 ? 3 : it.len > 110 ? 2 : 1;
    for (let k = 0; k < arrowCount; k++) {
      const arrow = document.createElementNS(ns, "polygon");
      // 燕尾尖头箭头，根据功率档位等比缩放
      const s = it.scale;
      const ptsStr = `${(-6.5 * s).toFixed(1)},${(-4.6 * s).toFixed(1)} ${(7.5 * s).toFixed(1)},0 ${(-6.5 * s).toFixed(1)},${(4.6 * s).toFixed(1)} ${(-3.2 * s).toFixed(1)},0`;
      arrow.setAttribute("points", ptsStr);
      arrow.setAttribute("fill", it.color);
      arrow.setAttribute("stroke", dark ? "#040812" : "#FFFFFF");
      arrow.setAttribute("stroke-width", "1.1");
      arrow.setAttribute("class", "agc-flow-arrow");

      const anim = document.createElementNS(ns, "animateMotion");
      anim.setAttribute("path", it.d);
      anim.setAttribute("dur", `${durSec.toFixed(2)}s`);
      anim.setAttribute("rotate", "auto");
      anim.setAttribute("repeatCount", "indefinite");
      anim.setAttribute("begin", `${(-(durSec * k) / arrowCount).toFixed(2)}s`);
      arrow.appendChild(anim);
      g.appendChild(arrow);
    }

    flowLayer.appendChild(g);
  });
}

function updateDiagramTelemetryOverlay() {
  updateDiagramFlowAnimations();
  const overlay = document.getElementById("agcTelemetryOverlayLayer");
  const stage = document.getElementById("diagramStage");
  if (!overlay || !stage || !AGCState.snapshot) return;
  const show = document.getElementById("diagramShowOverlay")?.checked !== false;
  overlay.innerHTML = "";
  if (!show) return;

  const scada = (AGCState.snapshot.measurements && AGCState.snapshot.measurements.scada) || [];
  const byDev = new Map();
  scada.forEach((m) => {
    if (!m.valid) return;
    if (!byDev.has(m.dev_name)) byDev.set(m.dev_name, {});
    byDev.get(m.dev_name)[m.meas_type] = m.value;
  });

  const pal = chartThemePalette();
  const uses = stage.querySelectorAll("use[name]");
  const ns = "http://www.w3.org/2000/svg";

  uses.forEach((u) => {
    const name = u.getAttribute("name") || "";
    const rec = byDev.get(name);
    if (!rec) return;
    const x = Number(u.getAttribute("x") || 0);
    const y = Number(u.getAttribute("y") || 0);
    const w = Number(u.getAttribute("width") || 120);
    let text = "";
    if (rec.P_GEN !== undefined) text = `${Number(rec.P_GEN).toFixed(1)}kW`;
    else if (rec.P_LOAD !== undefined) text = `${Number(rec.P_LOAD).toFixed(1)}kW`;
    else if (rec.P_AC !== undefined) text = `${Number(rec.P_AC).toFixed(1)}kW`;
    else if (rec.P_I !== undefined) text = `${Number(rec.P_I).toFixed(1)}kW`;
    if (rec.SOC !== undefined) {
      const sp = Number(rec.SOC) <= 1.5 ? Number(rec.SOC) * 100 : Number(rec.SOC);
      text += (text ? " " : "") + `${sp.toFixed(0)}%`;
    }
    if (!text) return;

    const g = document.createElementNS(ns, "g");
    const rect = document.createElementNS(ns, "rect");
    const padX = 8;
    const tw = text.length * 10.5 + padX * 2;
    const rx = x + w / 2 - tw / 2;
    const ry = y - 24;
    rect.setAttribute("x", String(rx));
    rect.setAttribute("y", String(ry));
    rect.setAttribute("width", String(tw));
    rect.setAttribute("height", "22");
    rect.setAttribute("rx", "5");
    rect.setAttribute("fill", pal.labelBg);
    rect.setAttribute("stroke", pal.crosshair);
    rect.setAttribute("stroke-width", "1.4");
    g.appendChild(rect);

    const tx = document.createElementNS(ns, "text");
    tx.setAttribute("x", String(rx + tw / 2));
    tx.setAttribute("y", String(ry + 16));
    tx.setAttribute("text-anchor", "middle");
    tx.setAttribute("fill", pal.textPrimary);
    tx.setAttribute("class", "diagram-overlay-tag");
    tx.textContent = text;
    g.appendChild(tx);
    overlay.appendChild(g);
  });

  if (AGCState.diagramSelectedDev) {
    const dlg = document.getElementById("diagramDeviceDialog");
    if (dlg && dlg.open) {
      renderDiagramDeviceCard(AGCState.diagramSelectedDev, false);
    }
  }
}

function handleDiagramElementClick(ev) {
  const target = ev.target.closest("use[name], g[name]");
  if (!target) return;
  const devName = target.getAttribute("name");
  if (!devName) return;
  AGCState.diagramSelectedDev = devName;
  renderDiagramDeviceCard(devName, true);
}

function renderDiagramDeviceCard(devName, openDialog = false) {
  const dlg = document.getElementById("diagramDeviceDialog");
  if (!dlg || !AGCState.snapshot) return;
  const dev = (AGCState.snapshot.devices || []).find((d) => d.dev_name === devName);
  const meas = ((AGCState.snapshot.measurements && AGCState.snapshot.measurements.scada) || []).filter(
    (m) => m.dev_name === devName && m.valid && (!dev || m.dev_type === dev.dev_type)
  );
  document.getElementById("diagCardTitle").textContent = devName;
  const kv = document.getElementById("diagCardMetrics");
  const rows = [];
  if (dev) {
    rows.push(["类型", dev.dev_type]);
    rows.push(["控制模式", dev.mode || "--"]);
    rows.push(["运行状态", dev.run_stat ? "运行" : "停运"]);
  }
  meas.forEach((m) => {
    const valStr = m.meas_type === "SOC" ? fmtPct(m.value) : `${fmtNum(m.value, 2)} ${m.unit || ""}`.trim();
    rows.push([m.meas_type, valStr]);
  });
  kv.innerHTML = rows.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd class="mono">${escapeHtml(v)}</dd></div>`).join("");

  const acts = document.getElementById("diagCardActions");
  acts.innerHTML = "";
  if (dev) {
    const setTypes = dev.set_types || [];
    if (setTypes.length) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-primary btn-sm";
      btn.textContent = `${t("cmd.yt")} (${setTypes[0]})`;
      btn.onclick = () => {
        if (dlg.open) dlg.close();
        openManualCmdModal(dev.dev_type, dev.dev_name, setTypes[0], "yt");
      };
      acts.appendChild(btn);
    }
    const ykType = ["ACBreak", "DCBreak"].includes(dev.dev_type) ? "closed_status" : "run_stat";
    const btnYk = document.createElement("button");
    btnYk.type = "button";
    btnYk.className = "btn btn-sm";
    btnYk.textContent = t("cmd.yk");
    btnYk.onclick = () => {
      if (dlg.open) dlg.close();
      openManualCmdModal(dev.dev_type, dev.dev_name, ykType, "yk");
    };
    acts.appendChild(btnYk);
  }
  if (openDialog && !dlg.open) {
    dlg.showModal();
  }
}

function bindDiagramInteractions() {
  const vp = document.getElementById("diagramViewport");
  if (!vp) return;
  let dragging = false;
  let sx = 0;
  let sy = 0;

  vp.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.12 : 0.89;
    const rect = vp.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;
    const nextScale = Math.max(0.12, Math.min(4, AGCState.diagramScale * factor));
    const ratio = nextScale / AGCState.diagramScale;
    AGCState.diagramTx = mx - (mx - AGCState.diagramTx) * ratio;
    AGCState.diagramTy = my - (my - AGCState.diagramTy) * ratio;
    AGCState.diagramScale = nextScale;
    applyDiagramTransform();
  }, { passive: false });

  vp.addEventListener("mousedown", (ev) => {
    if (ev.target.closest(".diagram-floating-card")) return;
    dragging = true;
    sx = ev.clientX - AGCState.diagramTx;
    sy = ev.clientY - AGCState.diagramTy;
  });
  window.addEventListener("mousemove", (ev) => {
    if (!dragging) return;
    AGCState.diagramTx = ev.clientX - sx;
    AGCState.diagramTy = ev.clientY - sy;
    applyDiagramTransform();
  });
  window.addEventListener("mouseup", () => { dragging = false; });

  document.getElementById("diagramZoomIn")?.addEventListener("click", () => {
    AGCState.diagramScale = Math.min(4, AGCState.diagramScale * 1.2);
    applyDiagramTransform();
  });
  document.getElementById("diagramZoomOut")?.addEventListener("click", () => {
    AGCState.diagramScale = Math.max(0.12, AGCState.diagramScale / 1.2);
    applyDiagramTransform();
  });
  document.getElementById("diagramFitBtn")?.addEventListener("click", fitDiagramToViewport);
  document.getElementById("diagramShowFlow")?.addEventListener("change", () => {
    AGCState._lastFlowSignature = "";
    updateDiagramFlowAnimations();
  });
  document.getElementById("diagramShowOverlay")?.addEventListener("change", updateDiagramTelemetryOverlay);
  const closeDiagDialog = () => {
    AGCState.diagramSelectedDev = null;
    const dlg = document.getElementById("diagramDeviceDialog");
    if (dlg && dlg.open) dlg.close();
  };
  document.getElementById("diagCardClose")?.addEventListener("click", closeDiagDialog);
  document.getElementById("diagCardCancel")?.addEventListener("click", closeDiagDialog);
}
