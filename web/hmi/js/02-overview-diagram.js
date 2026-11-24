// 主页交直流+氢能全景能量流渲染 & 秦岭 SVG 接线图缩放平移与实时遥测叠加

function setEfCard(id, { cur, tgt, max, meta }) {
  const el = document.getElementById(id);
  if (!el) return;
  const setText = (sel, txt) => {
    const node = el.querySelector(sel);
    if (node) node.textContent = txt;
  };
  if (cur !== undefined) setText("[data-ef-cur]", cur);
  if (tgt !== undefined) setText("[data-ef-tgt]", tgt);
  if (max !== undefined) setText("[data-ef-max]", max);
  if (meta !== undefined) setText("[data-ef-meta]", meta);
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

  // 能量流各节点
  setEfCard("efDcWind", {
    cur: `${fmtNum(dcWindCur, 1)} kW`,
    tgt: `${fmtNum(m.dcWindTargetKw, 1)} kW`,
    max: `${fmtNum(m.dcWindMaxAvailableKw, 1)} kW`,
    meta: "10 台"
  });
  setEfCard("efDcPv", {
    cur: `${fmtNum(dcPvCur, 1)} kW`,
    tgt: `${fmtNum(m.dcPvTargetKw, 1)} kW`,
    max: `${fmtNum(m.dcPvMaxAvailableKw, 1)} kW`,
    meta: "3 台"
  });
  setEfCard("efDcStorage", {
    cur: `${fmtNum(dcStCur, 1)} kW`,
    tgt: `${fmtNum(m.dcStorageTargetKw, 1)} kW`,
    meta: `SOC ${fmtPct(m.dcStorageSoc)}`
  });
  setEfCard("efDcLoad", { cur: `${fmtNum(dcLoadKw, 1)} kW`, meta: "750V" });

  setEfCard("efAcWind", {
    cur: `${fmtNum(acWindCur, 1)} kW`,
    tgt: `${fmtNum(m.acWindTargetKw, 1)} kW`,
    max: `${fmtNum(m.acWindMaxAvailableKw, 1)} kW`,
    meta: "1 台"
  });
  setEfCard("efAcPv", {
    cur: `${fmtNum(acPvCur, 1)} kW`,
    tgt: `${fmtNum(m.acPvTargetKw, 1)} kW`,
    max: `${fmtNum(m.acPvMaxAvailableKw, 1)} kW`,
    meta: "1 台"
  });
  setEfCard("efAcStorage", {
    cur: `${fmtNum(acStCur, 1)} kW`,
    tgt: `${fmtNum(m.acStorageTargetKw, 1)} kW`,
    meta: `SOC ${fmtPct(m.acStorageSoc)}`
  });
  setEfCard("efDiesel", {
    cur: `${fmtNum(dieselVal, 1)} kW`,
    tgt: `${fmtNum(m.totalDieselTargetKw ?? m.dieselTargetKw, 1)} kW`,
    meta: `下限 ${fmtNum(m.totalDieselMinKw ?? m.dieselMinKw, 0)} kW`
  });
  setEfCard("efAcLoad", { cur: `${fmtNum(acLoadKw, 1)} kW`, meta: "380V" });

  setEfCard("efAcdc", {
    cur: `${fmtNum(acdcCur, 1)} kW`,
    tgt: `${fmtNum(m.acdcTargetKw, 1)} kW`,
    meta: "2×300 kW"
  });
  setEfCard("efFuelCell", {
    cur: `${fmtNum(m.fuelCellCurrentKw, 1)} kW`,
    meta: `${fmtNum(m.fuelCellFlowCurrentNm3h, 2)} Nm³/h`
  });
  setEfCard("efHydroTank", {
    cur: `SOC ${fmtPct(m.hydrogenStorageSoc)}`,
    meta: `${fmtNum(m.hydrogenStoragePressureMpa, 2)} MPa`
  });
  setEfCard("efElectrolyzer", {
    cur: `${fmtNum(m.electrolyzerCurrentKw, 1)} kW`,
    meta: `${fmtNum(m.electrolyzerFlowCurrentNm3h, 2)} Nm³/h`
  });

  document.getElementById("efGreenPower").textContent = `${fmtNum(renTotal, 1)} kW`;
  document.getElementById("efGreenShare").textContent = `${fmtNum(greenShare, 1)}%`;
  const dqBadge = document.getElementById("efDataQualityBadge");
  const dqOk = !latest || latest.data_quality === "ok";
  dqBadge.className = `badge ${dqOk ? "badge-ok" : "badge-warn"}`;
  dqBadge.textContent = dqOk ? "OK" : String(latest.data_quality || "DEGRADED").toUpperCase();

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
          <th>${t("col.device")}</th>
          <th class="num">${t("lbl.current")} (kW)</th>
          <th class="num">${t("lbl.target")} (kW)</th>
          <th class="num">ΔP (kW)</th>
          <th>状态</th>
        </tr></thead>
        <tbody>
          ${changedRows.map((r) => `
            <tr>
              <td>${escapeHtml(r.dev_name)}</td>
              <td class="num">${fmtNum(r.current_kw, 2)}</td>
              <td class="num">${fmtNum(r.target_kw, 2)}</td>
              <td class="num ${Number(r.delta_kw) >= 0 ? "val-pos" : "val-neg"}">${Number(r.delta_kw) >= 0 ? "+" : ""}${fmtNum(r.delta_kw, 2)}</td>
              <td><span class="badge ${r.dispatch_status === "dispatched" ? "badge-ok" : "badge-info"}">${escapeHtml(r.dispatch_status)}</span></td>
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
      const overlay = document.createElementNS("http://www.w3.org/2000/svg", "g");
      overlay.setAttribute("id", "agcTelemetryOverlayLayer");
      svgEl.appendChild(overlay);
      svgEl.addEventListener("click", handleDiagramElementClick);
    }
    AGCState.diagramLoaded = true;
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

function updateDiagramTelemetryOverlay() {
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
    renderDiagramDeviceCard(AGCState.diagramSelectedDev);
  }
}

function handleDiagramElementClick(ev) {
  const target = ev.target.closest("use[name], g[name]");
  if (!target) return;
  const devName = target.getAttribute("name");
  if (!devName) return;
  AGCState.diagramSelectedDev = devName;
  renderDiagramDeviceCard(devName);
}

function renderDiagramDeviceCard(devName) {
  const card = document.getElementById("diagramDeviceCard");
  if (!card || !AGCState.snapshot) return;
  const dev = (AGCState.snapshot.devices || []).find((d) => d.dev_name === devName);
  const meas = ((AGCState.snapshot.measurements && AGCState.snapshot.measurements.scada) || []).filter(
    (m) => m.dev_name === devName && m.valid
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
      btn.onclick = () => openManualCmdModal(dev.dev_type, dev.dev_name, setTypes[0], "yt");
      acts.appendChild(btn);
    }
    const ykType = ["ACBreak", "DCBreak"].includes(dev.dev_type) ? "closed_status" : "run_stat";
    const btnYk = document.createElement("button");
    btnYk.type = "button";
    btnYk.className = "btn btn-sm";
    btnYk.textContent = t("cmd.yk");
    btnYk.onclick = () => openManualCmdModal(dev.dev_type, dev.dev_name, ykType, "yk");
    acts.appendChild(btnYk);
  }
  card.hidden = false;
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
  document.getElementById("diagramShowOverlay")?.addEventListener("change", updateDiagramTelemetryOverlay);
  document.getElementById("diagCardClose")?.addEventListener("click", () => {
    AGCState.diagramSelectedDev = null;
    document.getElementById("diagramDeviceCard").hidden = true;
  });
}
