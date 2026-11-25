// AGC 控制决策页核心模块：启停、单次计算、开环/闭环、控制参数、储能降额、统计指标、策略表、趋势曲线、控制日志

const TREND_SERIES_META = [
  { key: "load_kw", label: "总负荷", group: "系统", scope: "system", axis: "left", unit: "kW" },
  { key: "renewable_current_kw", label: "总新能源实时", group: "系统", scope: "system", axis: "left", unit: "kW" },
  { key: "renewable_target_kw", label: "总新能源目标", group: "系统", scope: "system", axis: "left", unit: "kW", dashed: true },
  { key: "renewable_max_available_kw", label: "新能源最大可发", group: "系统", scope: "system", axis: "left", unit: "kW", dashed: true },
  { key: "wind_current_kw", label: "总风电实时", group: "系统", scope: "system", axis: "left", unit: "kW" },
  { key: "wind_target_kw", label: "总风电目标", group: "系统", scope: "system", axis: "left", unit: "kW", dashed: true },
  { key: "pv_current_kw", label: "总光伏实时", group: "系统", scope: "system", axis: "left", unit: "kW" },
  { key: "pv_target_kw", label: "总光伏目标", group: "系统", scope: "system", axis: "left", unit: "kW", dashed: true },
  { key: "storage_current_kw", label: "总储能实时", group: "系统", scope: "system", axis: "left", unit: "kW" },
  { key: "storage_target_kw", label: "总储能目标", group: "系统", scope: "system", axis: "left", unit: "kW", dashed: true },
  { key: "storage_soc", label: "总储能 SOC", group: "系统", scope: "system", axis: "right", unit: "%", isRatio: true },
  { key: "diesel_current_kw", label: "柴发实时", group: "系统", scope: "system", axis: "left", unit: "kW" },
  { key: "diesel_target_kw", label: "柴发目标", group: "系统", scope: "system", axis: "left", unit: "kW", dashed: true },
  { key: "diesel_min_kw", label: "柴发下限", group: "系统", scope: "system", axis: "left", unit: "kW", dashed: true },
  { key: "acdc_current_kw", label: "ACDC 变流实时", group: "系统", scope: "system", axis: "left", unit: "kW" },
  { key: "acdc_target_kw", label: "ACDC 变流目标", group: "系统", scope: "system", axis: "left", unit: "kW", dashed: true },
  { key: "ac_renewable_current_kw", label: "交流新能源实时", group: "交流", scope: "ac", axis: "left", unit: "kW" },
  { key: "ac_renewable_target_kw", label: "交流新能源目标", group: "交流", scope: "ac", axis: "left", unit: "kW", dashed: true },
  { key: "ac_storage_current_kw", label: "交流储能实时", group: "交流", scope: "ac", axis: "left", unit: "kW" },
  { key: "ac_storage_target_kw", label: "交流储能目标", group: "交流", scope: "ac", axis: "left", unit: "kW", dashed: true },
  { key: "ac_storage_soc", label: "交流储能 SOC", group: "交流", scope: "ac", axis: "right", unit: "%", isRatio: true },
  { key: "ac_load_kw", label: "交流负荷", group: "交流", scope: "ac", axis: "left", unit: "kW" },
  { key: "dc_renewable_current_kw", label: "直流新能源实时", group: "直流", scope: "dc", axis: "left", unit: "kW" },
  { key: "dc_renewable_target_kw", label: "直流新能源目标", group: "直流", scope: "dc", axis: "left", unit: "kW", dashed: true },
  { key: "dc_storage_current_kw", label: "直流储能实时", group: "直流", scope: "dc", axis: "left", unit: "kW" },
  { key: "dc_storage_target_kw", label: "直流储能目标", group: "直流", scope: "dc", axis: "left", unit: "kW", dashed: true },
  { key: "dc_storage_soc", label: "直流储能 SOC", group: "直流", scope: "dc", axis: "right", unit: "%", isRatio: true },
  { key: "dc_load_kw", label: "直流负荷", group: "直流", scope: "dc", axis: "left", unit: "kW" },
  { key: "electrolyzer_current_kw", label: "电制氢实时", group: "氢能", scope: "hydrogen", axis: "left", unit: "kW" },
  { key: "electrolyzer_target_kw", label: "电制氢目标", group: "氢能", scope: "hydrogen", axis: "left", unit: "kW", dashed: true },
  { key: "fuel_cell_current_kw", label: "燃料电池实时", group: "氢能", scope: "hydrogen", axis: "left", unit: "kW" },
  { key: "fuel_cell_target_kw", label: "燃料电池目标", group: "氢能", scope: "hydrogen", axis: "left", unit: "kW", dashed: true },
  { key: "hydrogen_pressure_mpa", label: "储氢罐压力", group: "氢能", scope: "hydrogen", axis: "left", unit: "MPa" },
  { key: "hydrogen_soc", label: "储氢罐 SOC", group: "氢能", scope: "hydrogen", axis: "right", unit: "%", isRatio: true }
];

function seriesColorByKey(seriesKey) {
  const pal = chartThemePalette();
  const idx = TREND_SERIES_META.findIndex((s) => s.key === seriesKey);
  return pal.colors[(idx >= 0 ? idx : 0) % pal.colors.length];
}

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

function getMetricRowsForScope(tab, m) {
  if (tab === "ac") {
    return [
      { label: "新能源 (kW)", cur: fmtNum(m.acRenewableCurrentKw, 2), tgt: fmtNum(m.acRenewableTargetKw, 2), curKey: "ac_renewable_current_kw", tgtKey: "ac_renewable_target_kw" },
      { label: "新能源最大可发 (kW)", cur: fmtNum(m.acRenewableMaxAvailableKw, 2), tgt: "--" },
      { label: "风电 (kW)", cur: fmtNum(m.acWindCurrentKw, 2), tgt: fmtNum(m.acWindTargetKw, 2) },
      { label: "光伏 (kW)", cur: fmtNum(m.acPvCurrentKw, 2), tgt: fmtNum(m.acPvTargetKw, 2) },
      { label: "储能 (kW)", cur: fmtNum(m.acStorageCurrentKw, 2), tgt: fmtNum(m.acStorageTargetKw, 2), curKey: "ac_storage_current_kw", tgtKey: "ac_storage_target_kw" },
      { label: "储能 SOC (%)", cur: fmtPct(m.acStorageSoc), tgt: "--", curKey: "ac_storage_soc" },
      { label: "跟网储能 (kW)", cur: fmtNum(m.acGridFollowingStorageCurrentKw, 2), tgt: fmtNum(m.acGridFollowingStorageTargetKw, 2) },
      { label: "构网储能 (kW)", cur: fmtNum(m.acGridFormingStorageCurrentKw, 2), tgt: fmtNum(m.acGridFormingStorageTargetKw, 2) },
      { label: "柴发 (kW)", cur: fmtNum(m.acDieselCurrentKw ?? m.dieselCurrentKw, 2), tgt: fmtNum(m.acDieselTargetKw ?? m.dieselTargetKw, 2), curKey: "diesel_current_kw", tgtKey: "diesel_target_kw" },
      { label: "柴发下限 (kW)", cur: fmtNum(m.acDieselMinKw ?? m.dieselMinKw, 2), tgt: "--", curKey: "diesel_min_kw" },
      { label: "负荷 (kW)", cur: fmtNum(m.acLoadKw, 2), tgt: "--", curKey: "ac_load_kw" }
    ];
  }
  if (tab === "dc") {
    return [
      { label: "新能源 (kW)", cur: fmtNum(m.dcRenewableCurrentKw, 2), tgt: fmtNum(m.dcRenewableTargetKw, 2), curKey: "dc_renewable_current_kw", tgtKey: "dc_renewable_target_kw" },
      { label: "新能源最大可发 (kW)", cur: fmtNum(m.dcRenewableMaxAvailableKw, 2), tgt: "--" },
      { label: "风电 (kW)", cur: fmtNum(m.dcWindCurrentKw, 2), tgt: fmtNum(m.dcWindTargetKw, 2) },
      { label: "光伏 (kW)", cur: fmtNum(m.dcPvCurrentKw, 2), tgt: fmtNum(m.dcPvTargetKw, 2) },
      { label: "储能 (kW)", cur: fmtNum(m.dcStorageCurrentKw, 2), tgt: fmtNum(m.dcStorageTargetKw, 2), curKey: "dc_storage_current_kw", tgtKey: "dc_storage_target_kw" },
      { label: "储能 SOC (%)", cur: fmtPct(m.dcStorageSoc), tgt: "--", curKey: "dc_storage_soc" },
      { label: "跟网储能 (kW)", cur: fmtNum(m.dcGridFollowingStorageCurrentKw, 2), tgt: fmtNum(m.dcGridFollowingStorageTargetKw, 2) },
      { label: "构网储能 (kW)", cur: fmtNum(m.dcGridFormingStorageCurrentKw, 2), tgt: fmtNum(m.dcGridFormingStorageTargetKw, 2) },
      { label: "负荷 (kW)", cur: fmtNum(m.dcLoadKw, 2), tgt: "--", curKey: "dc_load_kw" }
    ];
  }
  if (tab === "hydrogen") {
    return [
      { label: "电制氢功率 (kW)", cur: fmtNum(m.electrolyzerCurrentKw, 2), tgt: fmtNum(m.electrolyzerTargetKw, 2), curKey: "electrolyzer_current_kw", tgtKey: "electrolyzer_target_kw" },
      { label: "产氢流量 (Nm³/h)", cur: fmtNum(m.electrolyzerFlowCurrentNm3h, 2), tgt: fmtNum(m.electrolyzerFlowTargetNm3h, 2) },
      { label: "燃料电池功率 (kW)", cur: fmtNum(m.fuelCellCurrentKw, 2), tgt: fmtNum(m.fuelCellTargetKw, 2), curKey: "fuel_cell_current_kw", tgtKey: "fuel_cell_target_kw" },
      { label: "耗氢流量 (Nm³/h)", cur: fmtNum(m.fuelCellFlowCurrentNm3h, 2), tgt: fmtNum(m.fuelCellFlowTargetNm3h, 2) },
      { label: "储氢罐压力 (MPa)", cur: fmtNum(m.hydrogenStoragePressureMpa, 2), tgt: "--", curKey: "hydrogen_pressure_mpa" },
      { label: "储氢罐 SOC (%)", cur: fmtPct(m.hydrogenStorageSoc), tgt: "--", curKey: "hydrogen_soc" },
      { label: "储氢净流量 (Nm³/h)", cur: fmtNum(m.hydrogenStorageFlowNm3h, 2), tgt: "--" }
    ];
  }
  const renCur = Number(m.acRenewableCurrentKw || 0) + Number(m.dcRenewableCurrentKw || 0);
  const renTgt = Number(m.acRenewableTargetKw || 0) + Number(m.dcRenewableTargetKw || 0);
  const renMax = Number(m.acRenewableMaxAvailableKw || 0) + Number(m.dcRenewableMaxAvailableKw || 0);
  const windCur = Number(m.acWindCurrentKw || 0) + Number(m.dcWindCurrentKw || 0);
  const windTgt = Number(m.acWindTargetKw || 0) + Number(m.dcWindTargetKw || 0);
  const pvCur = Number(m.acPvCurrentKw || 0) + Number(m.dcPvCurrentKw || 0);
  const pvTgt = Number(m.acPvTargetKw || 0) + Number(m.dcPvTargetKw || 0);
  const stCur = Number(m.acStorageCurrentKw || 0) + Number(m.dcStorageCurrentKw || 0);
  const stTgt = Number(m.acStorageTargetKw || 0) + Number(m.dcStorageTargetKw || 0);
  return [
    { label: "总负荷 (kW)", cur: fmtNum(m.loadKw, 2), tgt: "--", curKey: "load_kw" },
    { label: "总新能源 (kW)", cur: fmtNum(renCur, 2), tgt: fmtNum(renTgt, 2), curKey: "renewable_current_kw", tgtKey: "renewable_target_kw" },
    { label: "最大可发 (kW)", cur: fmtNum(renMax, 2), tgt: "--", curKey: "renewable_max_available_kw" },
    { label: "总风电 (kW)", cur: fmtNum(windCur, 2), tgt: fmtNum(windTgt, 2), curKey: "wind_current_kw", tgtKey: "wind_target_kw" },
    { label: "总光伏 (kW)", cur: fmtNum(pvCur, 2), tgt: fmtNum(pvTgt, 2), curKey: "pv_current_kw", tgtKey: "pv_target_kw" },
    { label: "总储能 (kW)", cur: fmtNum(stCur, 2), tgt: fmtNum(stTgt, 2), curKey: "storage_current_kw", tgtKey: "storage_target_kw" },
    { label: "总储能 SOC (%)", cur: fmtPct(m.totalStorageSoc ?? m.storageSoc), tgt: "--", curKey: "storage_soc" },
    { label: "总柴发 (kW)", cur: fmtNum(m.totalDieselCurrentKw ?? m.dieselCurrentKw, 2), tgt: fmtNum(m.totalDieselTargetKw ?? m.dieselTargetKw, 2), curKey: "diesel_current_kw", tgtKey: "diesel_target_kw" },
    { label: "柴发下限 (kW)", cur: fmtNum(m.totalDieselMinKw ?? m.dieselMinKw, 2), tgt: "--", curKey: "diesel_min_kw" },
    { label: "ACDC 变流 (kW)", cur: fmtNum(m.acdcCurrentKw, 2), tgt: fmtNum(m.acdcTargetKw, 2), curKey: "acdc_current_kw", tgtKey: "acdc_target_kw" },
    { label: "弃电功率 (kW)", cur: fmtNum(m.curtailKw, 2), tgt: "--" }
  ];
}

function updateMetricTabBadges() {
  const totalBadge = document.getElementById("agcSelectedSeriesBadge");
  if (totalBadge) totalBadge.textContent = String(AGCState.selectedSeries.size);
  ["system", "ac", "dc", "hydrogen"].forEach((sc) => {
    const el = document.querySelector(`[data-tab-count="${sc}"]`);
    if (!el) return;
    const rows = getMetricRowsForScope(sc, {});
    const keys = new Set();
    rows.forEach((r) => {
      if (r.curKey && AGCState.selectedSeries.has(r.curKey)) keys.add(r.curKey);
      if (r.tgtKey && AGCState.selectedSeries.has(r.tgtKey)) keys.add(r.tgtKey);
    });
    el.textContent = keys.size ? `(${keys.size})` : "";
  });
}

function renderAgcMetricsTable() {
  const wrap = document.getElementById("agcMetricTableWrap");
  if (!wrap) return;
  const m = (AGCState.control && AGCState.control.latest && AGCState.control.latest.metrics) || {};
  const rows = getMetricRowsForScope(AGCState.metricTab, m);
  updateMetricTabBadges();

  const renderMetricValCell = (valText, seriesKey, isDashed) => {
    if (!seriesKey) return `<span class="mono">${escapeHtml(valText)}</span>`;
    const isSelected = AGCState.selectedSeries.has(seriesKey);
    const isHidden = isSelected && AGCState.hiddenSeries.has(seriesKey);
    const color = seriesColorByKey(seriesKey);
    const stateCls = !isSelected ? "is-unselected" : (isHidden ? "is-hidden" : "is-visible");
    const tip = !isSelected
      ? "点击加入并显示该曲线"
      : (isHidden ? "已隐藏，点击重新显示该曲线" : "点击隐藏该曲线（不删除）");
    return `
      <span class="metric-val-cell">
        <span class="mono">${escapeHtml(valText)}</span>
        <button type="button" class="metric-swatch-btn ${stateCls}" data-toggle-series="${seriesKey}" title="${tip}" aria-label="${tip}">
          <span class="metric-line-swatch ${isDashed ? "is-dashed" : ""}" style="color:${color}"></span>
        </button>
      </span>`;
  };

  wrap.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th class="col-check"></th>
          <th>指标</th>
          <th class="num">${t("lbl.current")}</th>
          <th class="num">${t("lbl.target")}</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((r) => {
          const rowKeys = [r.curKey, r.tgtKey].filter(Boolean);
          const anySelected = rowKeys.some((k) => AGCState.selectedSeries.has(k));
          const allSelected = rowKeys.length > 0 && rowKeys.every((k) => AGCState.selectedSeries.has(k));
          const checkCell = rowKeys.length
            ? `<input type="checkbox" data-row-keys="${rowKeys.join(",")}" ${allSelected ? "checked" : ""} title="${allSelected ? "取消勾选以移除本行全部曲线" : "勾选以显示本行全部曲线"}" />`
            : "";
          return `
            <tr class="${anySelected ? "is-selected" : ""}">
              <td class="col-check">${checkCell}</td>
              <td>${escapeHtml(r.label)}</td>
              <td class="num">${renderMetricValCell(r.cur, r.curKey, false)}</td>
              <td class="num">${renderMetricValCell(r.tgt, r.tgtKey, true)}</td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;

  wrap.querySelectorAll("input[data-row-keys]").forEach((cb) => {
    const keys = (cb.dataset.rowKeys || "").split(",").filter(Boolean);
    const selectedCount = keys.filter((k) => AGCState.selectedSeries.has(k)).length;
    if (selectedCount > 0 && selectedCount < keys.length) {
      cb.indeterminate = true;
    }
    cb.addEventListener("change", () => {
      if (cb.checked) {
        keys.forEach((k) => {
          AGCState.selectedSeries.add(k);
          AGCState.hiddenSeries.delete(k);
        });
      } else {
        keys.forEach((k) => {
          AGCState.selectedSeries.delete(k);
          AGCState.hiddenSeries.delete(k);
        });
      }
      renderAgcMetricsTable();
      renderAgcTrendChart();
    });
  });

  wrap.querySelectorAll("button[data-toggle-series]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const k = btn.dataset.toggleSeries;
      if (!k) return;
      if (!AGCState.selectedSeries.has(k)) {
        AGCState.selectedSeries.add(k);
        AGCState.hiddenSeries.delete(k);
      } else if (AGCState.hiddenSeries.has(k)) {
        AGCState.hiddenSeries.delete(k);
      } else {
        AGCState.hiddenSeries.add(k);
      }
      renderAgcMetricsTable();
      renderAgcTrendChart();
    });
  });
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

function renderAgcTrendChart() {
  const canvas = document.getElementById("agcTrendCanvas");
  const legendEl = document.getElementById("agcTrendLegend");
  if (!canvas) return;
  const pts = (AGCState.trendData && AGCState.trendData.points) || [];
  const labels = pts.map((p) => p.time_label || (p.recorded_at || "").slice(11, 19));

  const activeSeries = [];
  TREND_SERIES_META.forEach((meta) => {
    if (!AGCState.selectedSeries.has(meta.key)) return;
    const color = seriesColorByKey(meta.key);
    const values = pts.map((p) => {
      const v = p[meta.key];
      if (v === null || v === undefined) return null;
      return meta.isRatio && Number(v) <= 1.5 ? Number(v) * 100 : Number(v);
    });
    activeSeries.push({
      key: meta.key,
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
      .map((s) => {
        const isHidden = AGCState.hiddenSeries.has(s.key);
        const tip = isHidden ? "已隐藏，点击重新显示该曲线" : "点击隐藏该曲线（不删除）";
        return `<button type="button" class="legend-pill is-clickable ${isHidden ? "is-hidden" : ""}" data-legend-toggle="${s.key}" title="${tip}"><span class="metric-line-swatch ${s.dashed ? "is-dashed" : ""}" style="color:${s.color}"></span>${escapeHtml(s.label)}</button>`;
      })
      .join("");
    legendEl.querySelectorAll("[data-legend-toggle]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const k = btn.dataset.legendToggle;
        if (!k) return;
        if (AGCState.hiddenSeries.has(k)) AGCState.hiddenSeries.delete(k);
        else AGCState.hiddenSeries.add(k);
        renderAgcMetricsTable();
        renderAgcTrendChart();
      });
    });
  }

  const visibleSeries = activeSeries.filter((s) => !AGCState.hiddenSeries.has(s.key));
  const emptyText = !activeSeries.length
    ? "请在左侧统计指标表中勾选要展示的曲线"
    : !visibleSeries.length
      ? "当前已选曲线均已隐藏，点击曲线图标可恢复显示"
      : "点击单次计算或启动自动控制后记录趋势曲线";

  drawLineChart(canvas, {
    labels,
    series: visibleSeries,
    leftUnit: "kW",
    rightUnit: "%",
    emptyText
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

  // 统计指标表顶部曲线联动快捷按钮（默认 / 全选当前分组 / 清空）
  document.getElementById("agcSeriesSelectDefault")?.addEventListener("click", () => {
    AGCState.selectedSeries = new Set([
      "load_kw",
      "renewable_current_kw",
      "renewable_target_kw",
      "storage_current_kw",
      "storage_target_kw",
      "storage_soc",
      "diesel_current_kw",
      "diesel_target_kw"
    ]);
    AGCState.hiddenSeries.clear();
    renderAgcMetricsTable();
    renderAgcTrendChart();
  });
  document.getElementById("agcSeriesSelectAll")?.addEventListener("click", () => {
    const rows = getMetricRowsForScope(AGCState.metricTab, {});
    rows.forEach((r) => {
      if (r.curKey) {
        AGCState.selectedSeries.add(r.curKey);
        AGCState.hiddenSeries.delete(r.curKey);
      }
      if (r.tgtKey) {
        AGCState.selectedSeries.add(r.tgtKey);
        AGCState.hiddenSeries.delete(r.tgtKey);
      }
    });
    renderAgcMetricsTable();
    renderAgcTrendChart();
  });
  document.getElementById("agcSeriesClearAll")?.addEventListener("click", () => {
    AGCState.selectedSeries.clear();
    AGCState.hiddenSeries.clear();
    renderAgcMetricsTable();
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
