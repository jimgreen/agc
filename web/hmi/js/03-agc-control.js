// AGC 控制决策页核心模块：启停、单次计算、开环/闭环、控制参数、储能降额、统计指标、策略表、趋势曲线、控制日志

const TREND_SERIES_META = [
  { key: "load_kw", label: "总负荷", group: "系统", axis: "left", unit: "kW" },
  { key: "renewable_current_kw", label: "新能源实时", group: "系统", axis: "left", unit: "kW" },
  { key: "renewable_target_kw", label: "新能源目标", group: "系统", axis: "left", unit: "kW", dashed: true },
  { key: "renewable_max_available_kw", label: "新能源最大可发", group: "系统", axis: "left", unit: "kW", dashed: true },
  { key: "storage_current_kw", label: "储能实时", group: "系统", axis: "left", unit: "kW" },
  { key: "storage_target_kw", label: "储能目标", group: "系统", axis: "left", unit: "kW", dashed: true },
  { key: "diesel_current_kw", label: "柴发实时", group: "系统", axis: "left", unit: "kW" },
  { key: "diesel_target_kw", label: "柴发目标", group: "系统", axis: "left", unit: "kW", dashed: true },
  { key: "acdc_current_kw", label: "ACDC 变流实时", group: "系统", axis: "left", unit: "kW" },
  { key: "acdc_target_kw", label: "ACDC 变流目标", group: "系统", axis: "left", unit: "kW", dashed: true },
  { key: "ac_renewable_current_kw", label: "交流新能源实时", group: "交流/直流", axis: "left", unit: "kW" },
  { key: "dc_renewable_current_kw", label: "直流新能源实时", group: "交流/直流", axis: "left", unit: "kW" },
  { key: "ac_storage_current_kw", label: "交流储能实时", group: "交流/直流", axis: "left", unit: "kW" },
  { key: "dc_storage_current_kw", label: "直流储能实时", group: "交流/直流", axis: "left", unit: "kW" },
  { key: "electrolyzer_current_kw", label: "电制氢实时", group: "氢能", axis: "left", unit: "kW" },
  { key: "fuel_cell_current_kw", label: "燃料电池实时", group: "氢能", axis: "left", unit: "kW" },
  { key: "storage_soc", label: "总储能 SOC", group: "SOC (%)", axis: "right", unit: "%", isRatio: true },
  { key: "ac_storage_soc", label: "交流储能 SOC", group: "SOC (%)", axis: "right", unit: "%", isRatio: true },
  { key: "dc_storage_soc", label: "直流储能 SOC", group: "SOC (%)", axis: "right", unit: "%", isRatio: true },
  { key: "hydrogen_soc", label: "储氢罐 SOC", group: "SOC (%)", axis: "right", unit: "%", isRatio: true }
];

const STRATEGY_TAB_CATEGORY_MAP = {
  "all": null,
  "ac-wind": "交流风电",
  "dc-wind": "直流风电",
  "ac-pv": "交流光伏",
  "dc-pv": "直流光伏",
  "ac-grid-storage": "交流跟网储能",
  "dc-grid-storage": "直流跟网储能",
  "ac-balance-storage": "交流平衡储能",
  "dc-balance-storage": "直流平衡储能",
  "diesel": "柴油发电",
  "converter": "交直流变流器",
  "hydrogen": "氢"
};

const PARAM_FIELDS = [
  "interval_seconds",
  "command_valid_minutes",
  "step_coefficient",
  "storage_step_ratio",
  "diesel_step_ratio",
  "converter_ramp_step_ratio",
  "soc_min",
  "soc_max",
  "soc_deadband",
  "storage_soc_correction_step_scale",
  "grid_forming_storage_protection_ratio",
  "diesel_power_protection_ratio",
  "hydrogen_closed_loop_enabled",
  "hydrogen_pressure_deadband_ratio",
  "electrolyzer_power_min_ratio",
  "electrolyzer_power_max_ratio",
  "electrolyzer_power_step_ratio",
  "electrolyzer_storage_soc_start_minimum",
  "fuel_cell_power_min_ratio",
  "fuel_cell_power_max_ratio",
  "fuel_cell_power_step_ratio",
  "fuel_cell_storage_soc_limit",
  "optimization_renewable_curtailment_weight",
  "optimization_diesel_output_weight",
  "optimization_max_iterations",
  "optimization_balance_tolerance_kw",
  "optimization_bound_tolerance_kw",
  "optimization_ftol"
];

let agcPresetsCache = null;

async function refreshAgcControlData() {
  const data = await apiGet("/api/agc/control");
  AGCState.control = data;
  renderAgcControlPage();
}

async function refreshAgcTrendData() {
  const qs = new URLSearchParams({
    window: String(AGCState.trendWindow),
    limit: "600"
  });
  if (AGCState.trendEndMs) qs.set("end_epoch_ms", String(AGCState.trendEndMs));
  const data = await apiGet(`/api/agc/trend?${qs.toString()}`);
  AGCState.trendData = data;
  renderAgcTrendChart();
}

async function refreshAgcControlLogs() {
  const kw = document.getElementById("agcLogKeyword")?.value || "";
  const level = document.getElementById("agcLogLevel")?.value || "all";
  const pageSize = AGCState.runtimeSettings?.web?.runtime_log_page_size || 20;
  const qs = new URLSearchParams({
    page: String(AGCState.controlLogPage),
    page_size: String(pageSize),
    level,
    keyword: kw
  });
  const data = await apiGet(`/api/agc/logs?${qs.toString()}`);
  renderAgcControlLogTable(data);
}

function renderAgcControlPage() {
  const c = AGCState.control;
  if (!c) return;

  const badge = document.getElementById("agcStateBadge");
  badge.className = `badge ${c.auto_enabled ? "badge-ok" : "badge-neutral"}`;
  badge.textContent = c.auto_enabled ? t("state.running") : t("state.stopped");

  const toggleBtn = document.getElementById("agcAutoToggleBtn");
  toggleBtn.textContent = c.auto_enabled ? t("agc.stopAuto") : t("agc.startAuto");
  toggleBtn.className = `btn ${c.auto_enabled ? "btn-danger" : "btn-primary"}`;

  document.querySelectorAll("#agcLoopModeGroup button").forEach((btn) => {
    const active = btn.dataset.loopMode === c.loop_mode;
    btn.classList.toggle("is-active", active);
  });

  const env = c.environment || {};
  document.getElementById("agcEnvWind").textContent = fmtNum(env.wind_speed, 2);
  document.getElementById("agcEnvSolar").textContent = fmtNum(env.solar_irradiance, 1);
  document.getElementById("agcLastCalc").textContent = (c.last_calculated_at || "--").replace("T", " ");
  document.getElementById("agcStatusLine").textContent = c.status_text || "--";

  renderAgcMetricsTable();
  renderAgcStrategyTable();
}

function renderAgcMetricsTable() {
  const wrap = document.getElementById("agcMetricTableWrap");
  if (!wrap) return;
  const m = (AGCState.control && AGCState.control.latest && AGCState.control.latest.metrics) || {};
  const tab = AGCState.metricTab;
  let rows = [];
  if (tab === "ac") {
    rows = [
      ["新能源 (kW)", fmtNum(m.acRenewableCurrentKw, 2), fmtNum(m.acRenewableTargetKw, 2)],
      ["新能源最大可发 (kW)", fmtNum(m.acRenewableMaxAvailableKw, 2), "--"],
      ["风电 (kW)", fmtNum(m.acWindCurrentKw, 2), fmtNum(m.acWindTargetKw, 2)],
      ["光伏 (kW)", fmtNum(m.acPvCurrentKw, 2), fmtNum(m.acPvTargetKw, 2)],
      ["储能 (kW)", fmtNum(m.acStorageCurrentKw, 2), fmtNum(m.acStorageTargetKw, 2)],
      ["储能 SOC (%)", fmtPct(m.acStorageSoc), "--"],
      ["跟网储能 (kW)", fmtNum(m.acGridFollowingStorageCurrentKw, 2), fmtNum(m.acGridFollowingStorageTargetKw, 2)],
      ["构网储能 (kW)", fmtNum(m.acGridFormingStorageCurrentKw, 2), fmtNum(m.acGridFormingStorageTargetKw, 2)],
      ["柴发 (kW)", fmtNum(m.acDieselCurrentKw ?? m.dieselCurrentKw, 2), fmtNum(m.acDieselTargetKw ?? m.dieselTargetKw, 2)],
      ["柴发下限 (kW)", fmtNum(m.acDieselMinKw ?? m.dieselMinKw, 2), "--"],
      ["负荷 (kW)", fmtNum(m.acLoadKw, 2), "--"]
    ];
  } else if (tab === "dc") {
    rows = [
      ["新能源 (kW)", fmtNum(m.dcRenewableCurrentKw, 2), fmtNum(m.dcRenewableTargetKw, 2)],
      ["新能源最大可发 (kW)", fmtNum(m.dcRenewableMaxAvailableKw, 2), "--"],
      ["风电 (kW)", fmtNum(m.dcWindCurrentKw, 2), fmtNum(m.dcWindTargetKw, 2)],
      ["光伏 (kW)", fmtNum(m.dcPvCurrentKw, 2), fmtNum(m.dcPvTargetKw, 2)],
      ["储能 (kW)", fmtNum(m.dcStorageCurrentKw, 2), fmtNum(m.dcStorageTargetKw, 2)],
      ["储能 SOC (%)", fmtPct(m.dcStorageSoc), "--"],
      ["跟网储能 (kW)", fmtNum(m.dcGridFollowingStorageCurrentKw, 2), fmtNum(m.dcGridFollowingStorageTargetKw, 2)],
      ["构网储能 (kW)", fmtNum(m.dcGridFormingStorageCurrentKw, 2), fmtNum(m.dcGridFormingStorageTargetKw, 2)],
      ["负荷 (kW)", fmtNum(m.dcLoadKw, 2), "--"]
    ];
  } else if (tab === "hydrogen") {
    rows = [
      ["电制氢功率 (kW)", fmtNum(m.electrolyzerCurrentKw, 2), fmtNum(m.electrolyzerTargetKw, 2)],
      ["产氢流量 (Nm³/h)", fmtNum(m.electrolyzerFlowCurrentNm3h, 2), fmtNum(m.electrolyzerFlowTargetNm3h, 2)],
      ["燃料电池功率 (kW)", fmtNum(m.fuelCellCurrentKw, 2), fmtNum(m.fuelCellTargetKw, 2)],
      ["耗氢流量 (Nm³/h)", fmtNum(m.fuelCellFlowCurrentNm3h, 2), fmtNum(m.fuelCellFlowTargetNm3h, 2)],
      ["储氢罐压力 (MPa)", fmtNum(m.hydrogenStoragePressureMpa, 2), "--"],
      ["储氢罐 SOC (%)", fmtPct(m.hydrogenStorageSoc), "--"],
      ["储氢净流量 (Nm³/h)", fmtNum(m.hydrogenStorageFlowNm3h, 2), "--"]
    ];
  } else {
    const renCur = Number(m.acRenewableCurrentKw || 0) + Number(m.dcRenewableCurrentKw || 0);
    const renTgt = Number(m.acRenewableTargetKw || 0) + Number(m.dcRenewableTargetKw || 0);
    const renMax = Number(m.acRenewableMaxAvailableKw || 0) + Number(m.dcRenewableMaxAvailableKw || 0);
    const stCur = Number(m.acStorageCurrentKw || 0) + Number(m.dcStorageCurrentKw || 0);
    const stTgt = Number(m.acStorageTargetKw || 0) + Number(m.dcStorageTargetKw || 0);
    rows = [
      ["总新能源 (kW)", fmtNum(renCur, 2), fmtNum(renTgt, 2)],
      ["最大可发 (kW)", fmtNum(renMax, 2), "--"],
      ["总储能 (kW)", fmtNum(stCur, 2), fmtNum(stTgt, 2)],
      ["总柴发 (kW)", fmtNum(m.totalDieselCurrentKw ?? m.dieselCurrentKw, 2), fmtNum(m.totalDieselTargetKw ?? m.dieselTargetKw, 2)],
      ["总负荷 (kW)", fmtNum(m.loadKw, 2), "--"],
      ["ACDC 变流 (kW)", fmtNum(m.acdcCurrentKw, 2), fmtNum(m.acdcTargetKw, 2)],
      ["弃电功率 (kW)", fmtNum(m.curtailKw, 2), "--"]
    ];
  }
  wrap.innerHTML = `
    <table class="data-table">
      <thead><tr><th>指标</th><th class="num">${t("lbl.current")}</th><th class="num">${t("lbl.target")}</th></tr></thead>
      <tbody>
        ${rows.map(([k, a, b]) => `<tr><td>${escapeHtml(k)}</td><td class="num">${escapeHtml(a)}</td><td class="num">${escapeHtml(b)}</td></tr>`).join("")}
      </tbody>
    </table>`;
}

function renderAgcStrategyTable() {
  const wrap = document.getElementById("agcStrategyTableWrap");
  if (!wrap) return;
  const allRows = (AGCState.control && AGCState.control.latest && AGCState.control.latest.command_rows) || [];
  const catMatch = STRATEGY_TAB_CATEGORY_MAP[AGCState.strategyTab];
  const changedOnly = document.getElementById("agcChangedOnlyCheck")?.checked;

  const filtered = allRows.filter((r) => {
    if (changedOnly && !r.changed) return false;
    if (!catMatch) return true;
    const cat = String(r.category || "");
    if (AGCState.strategyTab === "hydrogen") return cat.includes("氢") || cat.includes("燃");
    return cat === catMatch;
  });

  document.getElementById("agcStrategySummary").textContent = `${filtered.length} / ${allRows.length}`;
  if (!filtered.length) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">暂无匹配的策略明细</td></tr></tbody></table>`;
    return;
  }
  wrap.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>分类</th>
          <th>${t("col.device")}</th>
          <th>模式</th>
          <th class="num">${t("lbl.current")} (kW)</th>
          <th class="num">${t("lbl.maxAvail")} (kW)</th>
          <th class="num">${t("lbl.target")} (kW)</th>
          <th class="num">ΔP (kW)</th>
          <th class="num">SOC (%)</th>
          <th>执行状态</th>
          <th>策略说明</th>
        </tr>
      </thead>
      <tbody>
        ${filtered.map((r) => {
          const deltaCls = r.changed ? (Number(r.delta_kw) >= 0 ? "val-pos" : "val-neg") : "";
          const badgeCls =
            r.dispatch_status === "dispatched" ? "badge-ok"
            : r.dispatch_status === "failed" || r.dispatch_status === "blocked" ? "badge-danger"
            : r.changed ? "badge-info" : "badge-neutral";
          return `
            <tr class="${r.changed ? "is-selected" : ""}">
              <td>${escapeHtml(r.category)}</td>
              <td><strong>${escapeHtml(r.dev_name)}</strong></td>
              <td class="mono">${escapeHtml(r.control_mode || "--")}</td>
              <td class="num">${fmtNum(r.current_kw, 2)}</td>
              <td class="num">${fmtNum(r.available_kw, 2)}</td>
              <td class="num">${fmtNum(r.target_kw, 2)}</td>
              <td class="num ${deltaCls}">${r.delta_kw !== null && r.delta_kw !== undefined ? (Number(r.delta_kw) >= 0 ? "+" : "") + fmtNum(r.delta_kw, 2) : "--"}</td>
              <td class="num">${r.soc !== null && r.soc !== undefined ? fmtPct(r.soc) : "--"}</td>
              <td><span class="badge ${badgeCls}">${escapeHtml(r.dispatch_status)}</span></td>
              <td class="ellipsis" title="${escapeHtml(r.status_label)}">${escapeHtml(r.status_label)}</td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

function renderSeriesPicker() {
  const listEl = document.getElementById("agcSeriesList");
  if (!listEl) return;
  const kw = (document.getElementById("agcSeriesSearch")?.value || "").trim().toLowerCase();
  const pal = chartThemePalette();
  const groups = new Map();
  TREND_SERIES_META.forEach((s, idx) => {
    if (kw && !s.label.toLowerCase().includes(kw) && !s.group.toLowerCase().includes(kw)) return;
    if (!groups.has(s.group)) groups.set(s.group, []);
    groups.get(s.group).push({ ...s, color: pal.colors[idx % pal.colors.length] });
  });

  let html = "";
  groups.forEach((items, gName) => {
    html += `<div class="series-group-title">${escapeHtml(gName)}</div>`;
    items.forEach((s) => {
      const checked = AGCState.selectedSeries.has(s.key) ? "checked" : "";
      html += `
        <label class="series-item">
          <input type="checkbox" data-series-key="${s.key}" ${checked} />
          <span class="series-dot" style="background:${s.color}"></span>
          <span>${escapeHtml(s.label)}</span>
        </label>`;
    });
  });
  listEl.innerHTML = html;
  listEl.querySelectorAll("input[data-series-key]").forEach((cb) => {
    cb.addEventListener("change", () => {
      const k = cb.dataset.seriesKey;
      if (cb.checked) AGCState.selectedSeries.add(k);
      else AGCState.selectedSeries.delete(k);
      renderAgcTrendChart();
    });
  });
}

function renderAgcTrendChart() {
  const canvas = document.getElementById("agcTrendCanvas");
  const legendEl = document.getElementById("agcTrendLegend");
  if (!canvas) return;
  const pts = (AGCState.trendData && AGCState.trendData.points) || [];
  const pal = chartThemePalette();
  const labels = pts.map((p) => p.time_label || (p.recorded_at || "").slice(11, 19));

  const activeSeries = [];
  TREND_SERIES_META.forEach((meta, idx) => {
    if (!AGCState.selectedSeries.has(meta.key)) return;
    const color = pal.colors[idx % pal.colors.length];
    const values = pts.map((p) => {
      const v = p[meta.key];
      if (v === null || v === undefined) return null;
      return meta.isRatio && Number(v) <= 1.5 ? Number(v) * 100 : Number(v);
    });
    activeSeries.push({
      label: meta.label,
      values,
      axis: meta.axis,
      unit: meta.unit,
      dashed: Boolean(meta.dashed),
      color
    });
  });

  if (legendEl) {
    legendEl.innerHTML = activeSeries
      .map((s) => `<span class="legend-pill"><span class="series-dot" style="background:${s.color}"></span>${escapeHtml(s.label)}</span>`)
      .join("");
  }

  drawLineChart(canvas, {
    labels,
    series: activeSeries,
    leftUnit: "kW",
    rightUnit: "%",
    emptyText: "点击单次计算或启动自动控制后记录趋势曲线"
  });
}

function renderAgcControlLogTable(data) {
  const wrap = document.getElementById("agcLogTableWrap");
  const pager = document.getElementById("agcLogPager");
  if (!wrap) return;
  const items = data.items || [];
  if (!items.length) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">暂无控制决策日志</td></tr></tbody></table>`;
  } else {
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr><th>时间</th><th>级别</th><th>主题</th><th>摘要</th></tr></thead>
        <tbody>
          ${items.map((it) => {
            const cls = it.level === "error" ? "badge-danger" : it.level === "warn" ? "badge-warn" : "badge-info";
            return `
              <tr title="${escapeHtml(it.detail || it.summary)}">
                <td class="mono">${escapeHtml((it.logged_at || "").replace("T", " "))}</td>
                <td><span class="badge ${cls}">${escapeHtml(String(it.level || "info").toUpperCase())}</span></td>
                <td>${escapeHtml(it.subject)}</td>
                <td>${escapeHtml(it.summary)}</td>
              </tr>`;
          }).join("")}
        </tbody>
      </table>`;
  }
  if (pager) {
    pager.innerHTML = `
      <span>共 ${data.total} 条 · 第 ${data.page}/${data.pages} 页</span>
      <div class="inline-btn-group">
        <button type="button" class="btn btn-xs" id="agcLogPrev" ${data.page <= 1 ? "disabled" : ""}>上一页</button>
        <button type="button" class="btn btn-xs" id="agcLogNext" ${data.page >= data.pages ? "disabled" : ""}>下一页</button>
      </div>`;
    document.getElementById("agcLogPrev")?.addEventListener("click", () => {
      AGCState.controlLogPage = Math.max(1, AGCState.controlLogPage - 1);
      refreshAgcControlLogs();
    });
    document.getElementById("agcLogNext")?.addEventListener("click", () => {
      AGCState.controlLogPage = Math.min(data.pages, AGCState.controlLogPage + 1);
      refreshAgcControlLogs();
    });
  }
}

// =========================================================================
// 控制参数弹窗 & 储能 SOC 降额曲线弹窗
// =========================================================================
function fillAgcParamsForm(settingsObj, presetName) {
  const presetSel = document.getElementById("agcPresetSelect");
  if (presetSel) presetSel.value = presetName || "custom";
  PARAM_FIELDS.forEach((field) => {
    const el = document.getElementById(`p_${field}`);
    if (!el || settingsObj[field] === undefined) return;
    if (el.type === "checkbox") {
      el.checked = Boolean(settingsObj[field]);
    } else if (el.dataset.percent === "1") {
      el.value = (Number(settingsObj[field]) * 100).toFixed(2).replace(/\.?0+$/, "");
    } else {
      el.value = String(settingsObj[field]);
    }
  });
}

function collectAgcParamsForm() {
  const out = {};
  PARAM_FIELDS.forEach((field) => {
    const el = document.getElementById(`p_${field}`);
    if (!el) return;
    if (el.type === "checkbox") {
      out[field] = Boolean(el.checked);
    } else if (el.dataset.percent === "1") {
      out[field] = Number(el.value) / 100.0;
    } else {
      out[field] = Number(el.value);
    }
  });
  return out;
}

function renderDeratingRows(tbodyId, curve) {
  const body = document.getElementById(tbodyId);
  if (!body) return;
  body.innerHTML = (curve || []).map((pt, idx) => {
    const soc = Number(pt.soc ?? pt[0]) * 100;
    const ratio = Number(pt.power_ratio ?? pt[1]) * 100;
    return `
      <tr>
        <td><input type="number" class="cell-input" data-derate-soc="${idx}" min="0" max="100" step="1" value="${soc.toFixed(0)}" /></td>
        <td><input type="number" class="cell-input" data-derate-ratio="${idx}" min="0" max="100" step="1" value="${ratio.toFixed(0)}" /></td>
      </tr>`;
  }).join("");
}

function collectDeratingRows(tbodyId) {
  const body = document.getElementById(tbodyId);
  if (!body) return [];
  const rows = [];
  body.querySelectorAll("tr").forEach((tr) => {
    const soc = Number(tr.querySelector("[data-derate-soc]")?.value) / 100;
    const ratio = Number(tr.querySelector("[data-derate-ratio]")?.value) / 100;
    if (Number.isFinite(soc) && Number.isFinite(ratio)) {
      rows.push({ soc, power_ratio: ratio });
    }
  });
  return rows;
}

function openDeratingDialog() {
  const s = (AGCState.control && AGCState.control.settings) || {};
  renderDeratingRows("chargeDeratingBody", s.storage_charge_derating_curve || []);
  renderDeratingRows("dischargeDeratingBody", s.storage_discharge_derating_curve || []);
  document.getElementById("deratingDialog")?.showModal();
}

function bindAgcControlEvents() {
  document.getElementById("agcCalcOnceBtn")?.addEventListener("click", async () => {
    try {
      const res = await apiPost("/api/agc/control", { action: "calculate_once" });
      AGCState.control = res.control;
      renderAgcControlPage();
      await Promise.all([refreshAgcTrendData(), refreshAgcControlLogs(), refreshSnapshot()]);
      showToast(`单次计算完成（周期 #${res.control.cycle}）`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  document.getElementById("agcAutoToggleBtn")?.addEventListener("click", async () => {
    const running = Boolean(AGCState.control && AGCState.control.auto_enabled);
    try {
      const res = await apiPost("/api/agc/control", { action: running ? "stop" : "start" });
      AGCState.control = res.control;
      renderAgcControlPage();
      await Promise.all([refreshAgcTrendData(), refreshAgcControlLogs(), refreshSnapshot()]);
      showToast(running ? "已停止实时自动控制" : "已启动实时自动控制", running ? "warn" : "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  document.querySelectorAll("#agcLoopModeGroup button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const targetMode = btn.dataset.loopMode;
      if (AGCState.control && AGCState.control.loop_mode === targetMode) return;
      if (targetMode === "closed") {
        const ok = await confirmDanger({
          title: "确认切换为闭环控制？",
          target: "AGC 闭环四遥下发",
          message: "切换至闭环控制后，AGC 计算产生的变动目标将自动通过 SCADA 四遥接口下发至现场/仿真设备。请确认参数与数据质量无误。",
          confirmText: "确认切至闭环"
        });
        if (!ok) return;
      }
      try {
        const res = await apiPost("/api/agc/control", { action: "set_mode", loop_mode: targetMode });
        AGCState.control = res.control;
        renderAgcControlPage();
        await refreshSnapshot();
        showToast(targetMode === "closed" ? "已切换至闭环控制" : "已切换至开环控制", targetMode === "closed" ? "warn" : "ok");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  document.querySelectorAll("#agcMetricTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#agcMetricTabs button").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      AGCState.metricTab = btn.dataset.metricTab;
      renderAgcMetricsTable();
    });
  });

  document.querySelectorAll("#agcStrategyTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#agcStrategyTabs button").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      AGCState.strategyTab = btn.dataset.strategyTab;
      renderAgcStrategyTable();
    });
  });

  document.getElementById("agcChangedOnlyCheck")?.addEventListener("change", renderAgcStrategyTable);

  document.querySelectorAll("#agcDetailTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#agcDetailTabs button").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      AGCState.detailTab = btn.dataset.detailTab;
      const isTrend = AGCState.detailTab === "trend";
      document.getElementById("agcDetailPaneTrend").hidden = !isTrend;
      document.getElementById("agcTrendToolbar").hidden = !isTrend;
      document.getElementById("agcDetailPaneLogs").hidden = isTrend;
      document.getElementById("agcLogToolbar").hidden = isTrend;
      if (isTrend) renderAgcTrendChart();
      else refreshAgcControlLogs();
    });
  });

  // 趋势窗口与翻页
  document.getElementById("agcTrendWindow")?.addEventListener("change", (ev) => {
    AGCState.trendWindow = Number(ev.target.value) || 60;
    AGCState.trendEndMs = null;
    refreshAgcTrendData();
  });
  document.getElementById("agcTrendPrevBtn")?.addEventListener("click", () => {
    const td = AGCState.trendData;
    if (!td || !td.end_epoch_ms) return;
    AGCState.trendEndMs = td.end_epoch_ms - AGCState.trendWindow * 60 * 1000;
    refreshAgcTrendData();
  });
  document.getElementById("agcTrendNowBtn")?.addEventListener("click", () => {
    AGCState.trendEndMs = null;
    refreshAgcTrendData();
  });
  document.getElementById("agcTrendNextBtn")?.addEventListener("click", () => {
    const td = AGCState.trendData;
    if (!td || !td.end_epoch_ms) return;
    const next = td.end_epoch_ms + AGCState.trendWindow * 60 * 1000;
    AGCState.trendEndMs = td.max_epoch_ms && next >= td.max_epoch_ms ? null : next;
    refreshAgcTrendData();
  });

  // 曲线多选器
  document.getElementById("agcSeriesSearch")?.addEventListener("input", renderSeriesPicker);
  document.getElementById("agcSeriesSelectDefault")?.addEventListener("click", () => {
    AGCState.selectedSeries = new Set(["load_kw", "renewable_current_kw", "renewable_target_kw", "storage_current_kw", "diesel_current_kw", "storage_soc"]);
    renderSeriesPicker();
    renderAgcTrendChart();
  });
  document.getElementById("agcSeriesSelectAll")?.addEventListener("click", () => {
    TREND_SERIES_META.forEach((s) => AGCState.selectedSeries.add(s.key));
    renderSeriesPicker();
    renderAgcTrendChart();
  });
  document.getElementById("agcSeriesClearAll")?.addEventListener("click", () => {
    AGCState.selectedSeries.clear();
    renderSeriesPicker();
    renderAgcTrendChart();
  });

  // 控制日志筛选与清空
  document.getElementById("agcLogKeyword")?.addEventListener("input", () => {
    AGCState.controlLogPage = 1;
    refreshAgcControlLogs();
  });
  document.getElementById("agcLogLevel")?.addEventListener("change", () => {
    AGCState.controlLogPage = 1;
    refreshAgcControlLogs();
  });
  document.getElementById("agcLogClearBtn")?.addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: "确认清空 AGC 控制决策日志？",
      target: "agc_control_logs",
      message: "清空后历史决策日志将不可恢复。",
      confirmText: "确认清空"
    });
    if (!ok) return;
    await apiPost("/api/agc/logs/clear");
    AGCState.controlLogPage = 1;
    await refreshAgcControlLogs();
    showToast("控制决策日志已清空", "ok");
  });

  // 控制参数弹窗
  document.getElementById("openAgcParamsBtn")?.addEventListener("click", async () => {
    if (!agcPresetsCache) {
      const p = await apiGet("/api/agc/presets");
      agcPresetsCache = p.presets || {};
    }
    fillAgcParamsForm((AGCState.control && AGCState.control.settings) || {}, AGCState.control?.preset);
    document.getElementById("agcParamsMessage").textContent = "";
    document.getElementById("agcParamsDialog")?.showModal();
  });

  document.getElementById("agcPresetSelect")?.addEventListener("change", (ev) => {
    const name = ev.target.value;
    if (agcPresetsCache && agcPresetsCache[name]) {
      const merged = { ...(AGCState.control?.settings || {}), ...agcPresetsCache[name] };
      fillAgcParamsForm(merged, name);
    }
  });

  document.querySelectorAll("#agcParamTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#agcParamTabs button").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      const target = btn.dataset.paramTab;
      document.querySelectorAll("[data-param-pane]").forEach((p) => {
        p.hidden = p.dataset.paramPane !== target;
      });
    });
  });

  document.getElementById("saveAgcParamsBtn")?.addEventListener("click", async () => {
    try {
      const params = collectAgcParamsForm();
      const preset = document.getElementById("agcPresetSelect")?.value || "custom";
      const res = await apiPost("/api/agc/control", { action: "save_settings", settings: params, preset });
      AGCState.control = res.control;
      document.getElementById("agcParamsDialog")?.close();
      renderAgcControlPage();
      showToast("AGC 控制参数已保存至 agc.sqlite3", "ok");
    } catch (err) {
      document.getElementById("agcParamsMessage").textContent = err.message;
    }
  });

  // 储能降额曲线弹窗
  document.getElementById("openDeratingQuickBtn")?.addEventListener("click", openDeratingDialog);
  document.getElementById("openDeratingFromParamsBtn")?.addEventListener("click", openDeratingDialog);

  document.getElementById("saveDeratingBtn")?.addEventListener("click", async () => {
    try {
      const charge = collectDeratingRows("chargeDeratingBody");
      const discharge = collectDeratingRows("dischargeDeratingBody");
      const res = await apiPost("/api/agc/control", {
        action: "save_settings",
        settings: {
          storage_charge_derating_curve: charge,
          storage_discharge_derating_curve: discharge
        },
        preset: "custom"
      });
      AGCState.control = res.control;
      document.getElementById("deratingDialog")?.close();
      showToast("储能 SOC 降额曲线已保存", "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  document.getElementById("resetDeratingBtn")?.addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: "确认恢复默认储能降额曲线？",
      target: "storage_charge/discharge_derating_curve",
      message: "当前自定义的充放电分段降额点将被默认配置覆盖。"
    });
    if (!ok) return;
    const res = await apiPost("/api/agc/control", { action: "apply_preset", preset: "standard" });
    AGCState.control = res.control;
    openDeratingDialog();
    showToast("已恢复默认降额曲线", "ok");
  });
}
