// 电网模型、实时量测、控制指令（含人工遥控/遥调）、曲线查询、运行参数与运行日志页面

function buildBlockTree(containerId, summaryId, items, activeBlock, filterText, onSelect) {
  const el = document.getElementById(containerId);
  const sumEl = document.getElementById(summaryId);
  if (!el) return;
  const kw = (filterText || "").trim().toLowerCase();
  const counts = new Map();
  let total = 0;
  items.forEach((it) => {
    const blk = String(it.dev_type || it.model_block || "Other");
    const name = String(it.dev_name || it.name || "");
    if (kw && !blk.toLowerCase().includes(kw) && !name.toLowerCase().includes(kw)) return;
    counts.set(blk, (counts.get(blk) || 0) + 1);
    total += 1;
  });
  if (sumEl) sumEl.textContent = `${counts.size} 类 · ${total}`;
  let html = `<button type="button" class="tree-node ${activeBlock === "ALL" ? "is-selected" : ""}" data-block="ALL"><span>${t("cat.all")}</span><small>${total}</small></button>`;
  [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0])).forEach(([blk, cnt]) => {
    html += `<button type="button" class="tree-node ${activeBlock === blk ? "is-selected" : ""}" data-block="${escapeHtml(blk)}"><span>${escapeHtml(blk)}</span><small>${cnt}</small></button>`;
  });
  el.innerHTML = html;
  el.querySelectorAll(".tree-node").forEach((btn) => {
    btn.addEventListener("click", () => onSelect(btn.dataset.block));
  });
}

// ==================== 4. 电网模型 ====================
async function renderModelPage() {
  if (!AGCState.modelData) {
    AGCState.modelData = await apiGet("/api/model");
  }
  const devs = AGCState.modelData.devices || [];
  const filter = document.getElementById("modelTreeFilter")?.value || "";
  buildBlockTree("modelTreeList", "modelTreeSummary", devs, AGCState.modelFilterBlock, filter, (blk) => {
    AGCState.modelFilterBlock = blk;
    renderModelPage();
  });

  const rows = AGCState.modelFilterBlock === "ALL"
    ? devs
    : devs.filter((d) => d.dev_type === AGCState.modelFilterBlock);
  document.getElementById("modelTableSummary").textContent = `${rows.length} 台`;

  const wrap = document.getElementById("modelParamTable");
  if (!rows.length) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">暂无设备模型数据</td></tr></tbody></table>`;
    return;
  }
  wrap.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>模型表</th>
          <th class="num">IDX</th>
          <th>${t("col.device")}</th>
          <th>设备类别</th>
          <th>控制模式</th>
          <th class="num">额定容量 (kW)</th>
          <th class="num">运行态</th>
          <th>接入域</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((d) => `
          <tr>
            <td class="mono">${escapeHtml(d.dev_type)}</td>
            <td class="num">${escapeHtml(d.idx)}</td>
            <td><strong>${escapeHtml(d.dev_name)}</strong></td>
            <td class="mono">${escapeHtml(d.device_kind || "--")}</td>
            <td class="mono">${escapeHtml(d.mode || "--")}</td>
            <td class="num">${fmtNum(d.rated_capacity, 1)}</td>
            <td class="num"><span class="badge ${d.run_stat ? "badge-ok" : "badge-neutral"}">${d.run_stat ? "1" : "0"}</span></td>
            <td class="mono">${escapeHtml((d.terminal_domains || []).join("/"))}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

// ==================== 5. 实时量测 ====================
function renderMeasurementsPage() {
  const snap = AGCState.snapshot;
  if (!snap) return;
  const scada = (snap.measurements && snap.measurements.scada) || [];
  const treeFilter = document.getElementById("measTreeFilter")?.value || "";
  buildBlockTree("measTreeList", "measTreeSummary", scada, AGCState.measFilterBlock, treeFilter, (blk) => {
    AGCState.measFilterBlock = blk;
    renderMeasurementsPage();
  });

  // 动态填充量测类型下拉框
  const typeSel = document.getElementById("measTypeSelect");
  if (typeSel && typeSel.options.length <= 1) {
    const types = [...new Set(scada.map((m) => m.meas_type))].sort();
    types.forEach((mt) => {
      const opt = document.createElement("option");
      opt.value = mt;
      opt.textContent = mt;
      typeSel.appendChild(opt);
    });
  }

  const kw = (document.getElementById("measKeyword")?.value || "").trim().toLowerCase();
  const typeVal = typeSel ? typeSel.value : "all";
  const abnormalOnly = document.getElementById("measAbnormalOnly")?.checked;

  const filtered = scada.filter((m) => {
    if (AGCState.measFilterBlock !== "ALL" && m.dev_type !== AGCState.measFilterBlock) return false;
    if (typeVal !== "all" && m.meas_type !== typeVal) return false;
    if (abnormalOnly && Number(m.valid) === 1 && Number(m.quality || 0) === 0) return false;
    if (kw && !String(m.dev_name).toLowerCase().includes(kw) && !String(m.meas_type).toLowerCase().includes(kw)) return false;
    return true;
  });

  const wrap = document.getElementById("measTableWrap");
  if (!filtered.length) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">暂无匹配的实时量测</td></tr></tbody></table>`;
    return;
  }

  const sel = AGCState.selectedMeas;
  wrap.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>设备表</th>
          <th>${t("col.device")}</th>
          <th>量测类型</th>
          <th class="num">实测值</th>
          <th>${t("col.unit")}</th>
          <th>状态</th>
          <th class="num">RTU / 点号</th>
          <th>采样时刻</th>
        </tr>
      </thead>
      <tbody>
        ${filtered.slice(0, 300).map((m, idx) => {
          const isSel = sel && sel.dev_type === m.dev_type && sel.dev_name === m.dev_name && sel.meas_type === m.meas_type;
          const valDisp = m.meas_type === "SOC" ? fmtPct(m.value) : fmtNum(m.value, 3);
          return `
            <tr data-meas-idx="${idx}" class="${isSel ? "is-selected" : ""}" style="cursor:pointer;">
              <td class="mono">${escapeHtml(m.dev_type)}</td>
              <td><strong>${escapeHtml(m.dev_name)}</strong></td>
              <td class="mono">${escapeHtml(m.meas_type)}</td>
              <td class="num">${valDisp}</td>
              <td>${escapeHtml(m.unit || "")}</td>
              <td><span class="badge ${m.valid ? "badge-ok" : "badge-danger"}">${m.valid ? "有效" : "无效"}</span></td>
              <td class="num">${m.rtu_id != null ? `${m.rtu_id}:${m.pnt_no}` : "--"}</td>
              <td class="mono">${escapeHtml((m.timestamp || "").replace("T", " ").slice(11, 19))}</td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;

  wrap._filteredRows = filtered.slice(0, 300);
  wrap.querySelectorAll("tr[data-meas-idx]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const row = wrap._filteredRows[Number(tr.dataset.measIdx)];
      if (!row) return;
      AGCState.selectedMeas = row;
      renderMeasurementsPage();
      refreshMeasurementTrace();
    });
  });

  if (!AGCState.selectedMeas && filtered.length) {
    AGCState.selectedMeas = filtered[0];
    refreshMeasurementTrace();
  }
}

async function refreshMeasurementTrace() {
  const sel = AGCState.selectedMeas;
  const canvas = document.getElementById("measTraceCanvas");
  const titleEl = document.getElementById("measTraceTitle");
  if (!sel || !canvas) return;
  const win = Number(document.getElementById("measTraceWindow")?.value || 60);
  if (titleEl) titleEl.textContent = `${sel.dev_name} · ${sel.meas_type}`;
  const qs = new URLSearchParams({
    dev_type: sel.dev_type,
    dev_name: sel.dev_name,
    meas_type: sel.meas_type,
    window: String(win)
  });
  const data = await apiGet(`/api/measurements/trace?${qs.toString()}`);
  const pts = data.points || [];
  drawLineChart(canvas, {
    labels: pts.map((p) => p.label || ""),
    series: [{
      label: sel.meas_type,
      values: pts.map((p) => (sel.meas_type === "SOC" && p.v <= 1.5 ? p.v * 100 : p.v)),
      unit: sel.meas_type === "SOC" ? "%" : (sel.unit || "")
    }],
    leftUnit: sel.meas_type === "SOC" ? "%" : (sel.unit || ""),
    emptyText: "等待更多采样点..."
  });
}

// ==================== 6. 控制指令（YT / YK & 人工操作） ====================
let currentManualTarget = null;

function openManualCmdModal(devType, devName, setType, kind) {
  currentManualTarget = { devType, devName, setType, kind };
  const isYk = kind === "yk" || setType === "run_stat" || setType === "closed_status";
  document.getElementById("manualCmdTitle").textContent = isYk ? `${t("cmd.yk")} · ${t("cmd.manualOp")}` : `${t("cmd.yt")} · ${t("cmd.manualOp")}`;
  document.getElementById("mcDevice").textContent = `${devName} (${devType})`;
  document.getElementById("mcSetType").textContent = setType;

  // 查找当前量测值
  const scada = (AGCState.snapshot?.measurements?.scada) || [];
  const found = scada.find((m) => m.dev_type === devType && m.dev_name === devName && (
    isYk ? ["RUN_STAT", "CLOSED_STATUS"].includes(m.meas_type) : ["P_GEN", "P_LOAD", "P_AC", "P_DC"].includes(m.meas_type)
  ));
  const curVal = found ? Number(found.value) : 0;
  currentManualTarget.currentValue = curVal;
  document.getElementById("mcCurrent").textContent = found ? fmtNum(curVal, 2) : "--";

  document.getElementById("mcYtBox").hidden = isYk;
  document.getElementById("mcYkBox").hidden = !isYk;
  if (isYk) {
    document.getElementById("mcYkSelect").value = curVal ? "0" : "1";
  } else {
    document.getElementById("mcYtInput").value = fmtNum(curVal, 2, "0");
  }
  document.getElementById("manualCmdDialog")?.showModal();
}

async function refreshCommandsPage() {
  const onlyActive = document.getElementById("cmdActiveOnly")?.checked ? "1" : "0";
  const origin = document.getElementById("cmdOriginFilter")?.value || "all";
  AGCState.commands = await apiGet(`/api/commands?only_active=${onlyActive}&origin=${origin}`);
  renderCommandsPage();
}

function renderCommandsPage() {
  const data = AGCState.commands;
  if (!data) return;
  const rows = AGCState.cmdKind === "yk" ? (data.yk || []) : (data.yt || []);
  const treeFilter = document.getElementById("cmdTreeFilter")?.value || "";
  buildBlockTree("cmdTreeList", "cmdTreeSummary", rows, AGCState.cmdFilterBlock, treeFilter, (blk) => {
    AGCState.cmdFilterBlock = blk;
    renderCommandsPage();
  });

  const kw = (document.getElementById("cmdKeyword")?.value || "").trim().toLowerCase();
  const filtered = rows.filter((r) => {
    if (AGCState.cmdFilterBlock !== "ALL" && r.dev_type !== AGCState.cmdFilterBlock) return false;
    if (kw && !String(r.dev_name).toLowerCase().includes(kw) && !String(r.set_type).toLowerCase().includes(kw)) return false;
    return true;
  });

  const wrap = document.getElementById("cmdTableWrap");
  if (!filtered.length) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">暂无控制指令</td></tr></tbody></table>`;
    return;
  }

  const sel = AGCState.selectedCmd;
  wrap.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>${t("col.device")}</th>
          <th>${t("col.point")}</th>
          <th class="num">RTU:点号</th>
          <th class="num">${t("lbl.current")}</th>
          <th class="num">指令值</th>
          <th>来源</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        ${filtered.map((r, idx) => {
          const isSel = sel && sel.dev_type === r.dev_type && sel.dev_name === r.dev_name && sel.set_type === r.set_type;
          const stCls = r.status === "dispatched" ? "badge-ok" : r.status === "failed" ? "badge-danger" : "badge-neutral";
          return `
            <tr data-cmd-idx="${idx}" class="${isSel ? "is-selected" : ""}" style="cursor:pointer;">
              <td><strong>${escapeHtml(r.dev_name)}</strong></td>
              <td class="mono">${escapeHtml(r.set_type)}</td>
              <td class="num">${r.rtu_id != null ? `${r.rtu_id}:${r.pnt_no}` : "--"}</td>
              <td class="num">${fmtNum(r.measured_value, 2)}</td>
              <td class="num">${fmtNum(r.command_value, 2)}</td>
              <td>${escapeHtml(r.origin)}</td>
              <td><span class="badge ${stCls}">${escapeHtml(r.status)}</span></td>
              <td><button type="button" class="btn btn-xs" data-manual-idx="${idx}">${t("cmd.manualOp")}</button></td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;

  wrap._rows = filtered;
  wrap.querySelectorAll("tr[data-cmd-idx]").forEach((tr) => {
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("button")) return;
      AGCState.selectedCmd = wrap._rows[Number(tr.dataset.cmdIdx)];
      renderCommandsPage();
      refreshCommandTrace();
    });
  });
  wrap.querySelectorAll("button[data-manual-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const r = wrap._rows[Number(btn.dataset.manualIdx)];
      if (r) openManualCmdModal(r.dev_type, r.dev_name, r.set_type, r.kind);
    });
  });
}

async function refreshCommandTrace() {
  const sel = AGCState.selectedCmd;
  const canvas = document.getElementById("cmdTraceCanvas");
  const titleEl = document.getElementById("cmdTraceTitle");
  if (!sel || !canvas) return;
  const win = Number(document.getElementById("cmdTraceWindow")?.value || 60);
  if (titleEl) titleEl.textContent = `${sel.dev_name} · ${sel.set_type}`;
  const qs = new URLSearchParams({
    dev_type: sel.dev_type,
    dev_name: sel.dev_name,
    set_type: sel.set_type,
    window: String(win)
  });
  const data = await apiGet(`/api/commands/trace?${qs.toString()}`);
  const pts = data.points || [];
  drawLineChart(canvas, {
    labels: pts.map((p) => (p.issued_at || "").slice(11, 19)),
    series: [
      { label: "控制值", values: pts.map((p) => p.command_value), unit: sel.unit || "" },
      { label: "实时值", values: pts.map((p) => p.measured_value), unit: sel.unit || "", dashed: true }
    ],
    leftUnit: sel.unit || "",
    emptyText: "暂无该指令历史跟踪记录"
  });
}

// ==================== 7. 曲线查询（agc.sqlite3 持久化历史） ====================
async function refreshCurvesPage() {
  const qs = new URLSearchParams({ window: String(AGCState.curveWindow), limit: "600" });
  if (AGCState.curveEndMs) qs.set("end_epoch_ms", String(AGCState.curveEndMs));
  const data = await apiGet(`/api/agc/trend?${qs.toString()}`);
  AGCState.curveQueryData = data;
  renderCurvesPage();
}

function renderCurvesPage() {
  const listEl = document.getElementById("curveMetricList");
  const filter = (document.getElementById("curveListFilter")?.value || "").trim().toLowerCase();
  const visible = TREND_SERIES_META.filter((s) => !filter || s.label.toLowerCase().includes(filter));
  document.getElementById("curveListCount").textContent = String(visible.length);
  if (listEl) {
    listEl.innerHTML = visible.map((s) => `
      <button type="button" class="tree-node ${AGCState.curveField === s.key ? "is-selected" : ""}" data-curve-key="${s.key}">
        <span>${escapeHtml(s.label)}</span>
        <small>${escapeHtml(s.unit)}</small>
      </button>`).join("");
    listEl.querySelectorAll("button[data-curve-key]").forEach((btn) => {
      btn.addEventListener("click", () => {
        AGCState.curveField = btn.dataset.curveKey;
        renderCurvesPage();
      });
    });
  }

  const meta = TREND_SERIES_META.find((s) => s.key === AGCState.curveField) || TREND_SERIES_META[0];
  const pts = (AGCState.curveQueryData && AGCState.curveQueryData.points) || [];
  document.getElementById("curveChartTitle").textContent = `${meta.label} (${meta.unit})`;
  document.getElementById("curvePointCount").textContent = `${pts.length} 点`;

  const values = pts.map((p) => {
    const v = p[meta.key];
    if (v === null || v === undefined) return null;
    return meta.isRatio && Number(v) <= 1.5 ? Number(v) * 100 : Number(v);
  });
  drawLineChart(document.getElementById("curveQueryCanvas"), {
    labels: pts.map((p) => p.time_label || (p.recorded_at || "").slice(11, 19)),
    series: [{ label: meta.label, values, unit: meta.unit }],
    leftUnit: meta.unit,
    emptyText: "暂无历史趋势曲线数据"
  });

  const wrap = document.getElementById("curveDataTableWrap");
  if (!pts.length) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">暂无持久化曲线数据</td></tr></tbody></table>`;
  } else {
    const recent = [...pts].reverse().slice(0, 200);
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr><th class="num">周期</th><th>记录时刻</th><th class="num">${escapeHtml(meta.label)} (${escapeHtml(meta.unit)})</th><th class="num">总负荷 (kW)</th><th class="num">新能源实时 (kW)</th><th class="num">柴发实时 (kW)</th></tr></thead>
        <tbody>
          ${recent.map((p) => {
            const rawV = p[meta.key];
            const dispV = rawV == null ? "--" : fmtNum(meta.isRatio && Number(rawV) <= 1.5 ? Number(rawV) * 100 : Number(rawV), 2);
            return `<tr>
              <td class="num">#${p.cycle}</td>
              <td class="mono">${escapeHtml((p.recorded_at || "").replace("T", " "))}</td>
              <td class="num">${dispV}</td>
              <td class="num">${fmtNum(p.load_kw, 2)}</td>
              <td class="num">${fmtNum(p.renewable_current_kw, 2)}</td>
              <td class="num">${fmtNum(p.diesel_current_kw, 2)}</td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>`;
  }
}

// ==================== 8. 参数配置 ====================
async function refreshRuntimeSettingsPage() {
  const data = await apiGet("/api/runtime-settings");
  AGCState.runtimeSettings = data;
  const b = data.backend || {};
  const w = data.web || {};
  const bind = (curId, inpId, val) => {
    const c = document.getElementById(curId);
    const i = document.getElementById(inpId);
    if (c) c.textContent = String(val ?? "--");
    if (i) i.value = String(val ?? "");
  };
  bind("curBackendRefresh", "inpBackendRefresh", b.backend_refresh_seconds);
  bind("curFrameAgeLimit", "inpFrameAgeLimit", b.frame_age_limit_seconds);
  bind("curCmdTimeout", "inpCmdTimeout", b.scada_command_timeout_seconds);
  bind("curTrendRetention", "inpTrendRetention", b.trend_retention_points);
  bind("curWebRefresh", "inpWebRefresh", w.frontend_refresh_seconds);
  bind("curLogPageSize", "inpLogPageSize", w.runtime_log_page_size);
  bind("curDiagElectric", "inpDiagElectric", w.diagram_flow_electric_threshold_kw);
  bind("curDiagHydro", "inpDiagHydro", w.diagram_flow_hydrogen_threshold_nm3_h);
}

// ==================== 9. 运行日志 ====================
async function refreshRuntimeLogsPage() {
  const kw = document.getElementById("runtimeLogKeyword")?.value || "";
  const logType = document.getElementById("runtimeLogType")?.value || "all";
  const onlyAlert = document.getElementById("runtimeLogAlertOnly")?.checked ? "1" : "0";
  const pageSize = AGCState.runtimeSettings?.web?.runtime_log_page_size || 20;
  const qs = new URLSearchParams({
    page: String(AGCState.runtimeLogPage),
    page_size: String(pageSize),
    log_type: logType,
    only_alert: onlyAlert,
    keyword: kw
  });
  const data = await apiGet(`/api/runtime-logs?${qs.toString()}`);
  document.getElementById("runtimeLogTotal").textContent = `${data.total}`;

  const typeSel = document.getElementById("runtimeLogType");
  if (typeSel && (data.types || []).length) {
    const existing = new Set([...typeSel.options].map((o) => o.value));
    data.types.forEach((tp) => {
      if (!existing.has(tp)) {
        const opt = document.createElement("option");
        opt.value = tp;
        opt.textContent = tp;
        typeSel.appendChild(opt);
      }
    });
  }

  const wrap = document.getElementById("runtimeLogTableWrap");
  const items = data.items || [];
  if (!items.length) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">暂无运行日志</td></tr></tbody></table>`;
  } else {
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr><th>时间</th><th>级别</th><th>类型</th><th>对象</th><th>结果</th><th>概要</th></tr></thead>
        <tbody>
          ${items.map((it) => {
            const cls = it.level === "error" ? "badge-danger" : it.level === "warn" ? "badge-warn" : "badge-info";
            return `
              <tr>
                <td class="mono">${escapeHtml((it.logged_at || "").replace("T", " "))}</td>
                <td><span class="badge ${cls}">${escapeHtml(String(it.level || "info").toUpperCase())}</span></td>
                <td class="mono">${escapeHtml(it.log_type)}</td>
                <td>${escapeHtml(it.target)}</td>
                <td>${escapeHtml(it.result)}</td>
                <td>${escapeHtml(it.summary)}</td>
              </tr>`;
          }).join("")}
        </tbody>
      </table>`;
  }

  const pager = document.getElementById("runtimeLogPager");
  if (pager) {
    pager.innerHTML = `
      <span>共 ${data.total} 条 · 第 ${data.page}/${data.pages} 页</span>
      <div class="inline-btn-group">
        <button type="button" class="btn btn-xs" id="rtLogPrev" ${data.page <= 1 ? "disabled" : ""}>上一页</button>
        <button type="button" class="btn btn-xs" id="rtLogNext" ${data.page >= data.pages ? "disabled" : ""}>下一页</button>
      </div>`;
    document.getElementById("rtLogPrev")?.addEventListener("click", () => {
      AGCState.runtimeLogPage = Math.max(1, AGCState.runtimeLogPage - 1);
      refreshRuntimeLogsPage();
    });
    document.getElementById("rtLogNext")?.addEventListener("click", () => {
      AGCState.runtimeLogPage = Math.min(data.pages, AGCState.runtimeLogPage + 1);
      refreshRuntimeLogsPage();
    });
  }
}

function bindMonitorPageEvents() {
  document.getElementById("modelTreeFilter")?.addEventListener("input", renderModelPage);
  document.getElementById("measTreeFilter")?.addEventListener("input", renderMeasurementsPage);
  document.getElementById("measKeyword")?.addEventListener("input", renderMeasurementsPage);
  document.getElementById("measTypeSelect")?.addEventListener("change", renderMeasurementsPage);
  document.getElementById("measAbnormalOnly")?.addEventListener("change", renderMeasurementsPage);
  document.getElementById("measTraceWindow")?.addEventListener("change", refreshMeasurementTrace);

  document.getElementById("exportMeasCsvBtn")?.addEventListener("click", () => {
    const scada = (AGCState.snapshot?.measurements?.scada) || [];
    downloadCsv(
      "qinling_measurements.csv",
      ["dev_type", "dev_name", "meas_type", "value", "unit", "valid", "rtu_id", "pnt_no", "timestamp"],
      scada.map((m) => [m.dev_type, m.dev_name, m.meas_type, m.value, m.unit, m.valid, m.rtu_id, m.pnt_no, m.timestamp])
    );
  });

  // 控制指令事件
  document.querySelectorAll("#cmdKindTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#cmdKindTabs button").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      AGCState.cmdKind = btn.dataset.cmdKind;
      renderCommandsPage();
    });
  });
  document.getElementById("cmdTreeFilter")?.addEventListener("input", renderCommandsPage);
  document.getElementById("cmdKeyword")?.addEventListener("input", renderCommandsPage);
  document.getElementById("cmdOriginFilter")?.addEventListener("change", refreshCommandsPage);
  document.getElementById("cmdActiveOnly")?.addEventListener("change", refreshCommandsPage);
  document.getElementById("cmdTraceWindow")?.addEventListener("change", refreshCommandTrace);

  // 人工遥控/遥调弹窗快捷与确认
  document.querySelectorAll("[data-yt-quick]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const inp = document.getElementById("mcYtInput");
      const cur = Number(currentManualTarget?.currentValue || 0);
      const q = btn.dataset.ytQuick;
      if (q === "zero") inp.value = "0";
      else if (q === "cur") inp.value = fmtNum(cur, 2, "0");
      else if (q === "minus10") inp.value = fmtNum(cur * 0.9, 2, "0");
      else if (q === "plus10") inp.value = fmtNum(cur * 1.1, 2, "0");
    });
  });

  document.getElementById("mcConfirmBtn")?.addEventListener("click", async () => {
    if (!currentManualTarget) return;
    const { devType, devName, setType, kind } = currentManualTarget;
    const isYk = kind === "yk" || setType === "run_stat" || setType === "closed_status";
    const val = isYk ? Number(document.getElementById("mcYkSelect").value) : Number(document.getElementById("mcYtInput").value);
    const ok = await confirmDanger({
      title: isYk ? "确认下发人工遥控 (YK)？" : "确认下发人工遥调 (YT)？",
      target: `${devName} · ${setType} = ${val}`,
      message: "该指令将直接通过 SCADA 四遥通道发送并写入操作审计日志。",
      confirmText: "确认下发"
    });
    if (!ok) return;
    try {
      const res = await apiPost("/api/commands/manual", { dev_type: devType, dev_name: devName, set_type: setType, value: val });
      document.getElementById("manualCmdDialog")?.close();
      showToast(res.message || "指令已下发", res.ok ? "ok" : "warn");
      await refreshCommandsPage();
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  // 曲线查询事件
  document.getElementById("curveListFilter")?.addEventListener("input", renderCurvesPage);
  document.getElementById("curveWindowSelect")?.addEventListener("change", (ev) => {
    AGCState.curveWindow = Number(ev.target.value) || 60;
    AGCState.curveEndMs = null;
    refreshCurvesPage();
  });
  document.getElementById("curvePrevBtn")?.addEventListener("click", () => {
    const d = AGCState.curveQueryData;
    if (!d || !d.end_epoch_ms) return;
    AGCState.curveEndMs = d.end_epoch_ms - AGCState.curveWindow * 60 * 1000;
    refreshCurvesPage();
  });
  document.getElementById("curveNowBtn")?.addEventListener("click", () => {
    AGCState.curveEndMs = null;
    refreshCurvesPage();
  });
  document.getElementById("curveNextBtn")?.addEventListener("click", () => {
    const d = AGCState.curveQueryData;
    if (!d || !d.end_epoch_ms) return;
    const next = d.end_epoch_ms + AGCState.curveWindow * 60 * 1000;
    AGCState.curveEndMs = d.max_epoch_ms && next >= d.max_epoch_ms ? null : next;
    refreshCurvesPage();
  });
  document.getElementById("exportCurveCsvBtn")?.addEventListener("click", () => {
    const pts = (AGCState.curveQueryData && AGCState.curveQueryData.points) || [];
    downloadCsv(
      "agc_trend_export.csv",
      ["cycle", "recorded_at", "load_kw", "renewable_current_kw", "renewable_target_kw", "storage_current_kw", "storage_soc", "diesel_current_kw", "diesel_target_kw", "hydrogen_soc"],
      pts.map((p) => [p.cycle, p.recorded_at, p.load_kw, p.renewable_current_kw, p.renewable_target_kw, p.storage_current_kw, p.storage_soc, p.diesel_current_kw, p.diesel_target_kw, p.hydrogen_soc])
    );
  });

  // 参数配置事件
  document.getElementById("saveBackendParamsBtn")?.addEventListener("click", async () => {
    await apiPost("/api/runtime-settings", {
      backend: {
        backend_refresh_seconds: Number(document.getElementById("inpBackendRefresh").value),
        frame_age_limit_seconds: Number(document.getElementById("inpFrameAgeLimit").value),
        scada_command_timeout_seconds: Number(document.getElementById("inpCmdTimeout").value),
        trend_retention_points: Number(document.getElementById("inpTrendRetention").value)
      }
    });
    await refreshRuntimeSettingsPage();
    showToast("后台运行参数已保存", "ok");
  });

  document.getElementById("resetBackendParamsBtn")?.addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: "确认恢复默认后台运行参数？",
      target: "backend_runtime_settings",
      message: "后台采集刷新周期与超时保护阈值将恢复为默认值。"
    });
    if (!ok) return;
    await apiPost("/api/runtime-settings", { reset_scope: "backend" });
    await refreshRuntimeSettingsPage();
    showToast("后台运行参数已恢复默认", "ok");
  });

  document.getElementById("saveWebParamsBtn")?.addEventListener("click", async () => {
    await apiPost("/api/runtime-settings", {
      web: {
        frontend_refresh_seconds: Number(document.getElementById("inpWebRefresh").value),
        runtime_log_page_size: Number(document.getElementById("inpLogPageSize").value),
        diagram_flow_electric_threshold_kw: Number(document.getElementById("inpDiagElectric").value),
        diagram_flow_hydrogen_threshold_nm3_h: Number(document.getElementById("inpDiagHydro").value)
      }
    });
    await refreshRuntimeSettingsPage();
    showToast("WEB 运行参数已保存", "ok");
  });

  document.getElementById("resetWebParamsBtn")?.addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: "确认恢复默认 WEB 运行参数？",
      target: "web_runtime_settings",
      message: "前端刷新周期与日志分页将恢复为默认值。"
    });
    if (!ok) return;
    await apiPost("/api/runtime-settings", { reset_scope: "web" });
    await refreshRuntimeSettingsPage();
    showToast("WEB 运行参数已恢复默认", "ok");
  });

  // 运行日志事件
  document.getElementById("runtimeLogKeyword")?.addEventListener("input", () => {
    AGCState.runtimeLogPage = 1;
    refreshRuntimeLogsPage();
  });
  document.getElementById("runtimeLogType")?.addEventListener("change", () => {
    AGCState.runtimeLogPage = 1;
    refreshRuntimeLogsPage();
  });
  document.getElementById("runtimeLogAlertOnly")?.addEventListener("change", () => {
    AGCState.runtimeLogPage = 1;
    refreshRuntimeLogsPage();
  });
  document.getElementById("clearRuntimeLogsBtn")?.addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: "确认清空系统运行日志？",
      target: "agc_runtime_logs",
      message: "此操作将清空数据库中已保存的全部运行日志记录。",
      confirmText: "确认清空"
    });
    if (!ok) return;
    await apiPost("/api/runtime-logs/clear");
    AGCState.runtimeLogPage = 1;
    await refreshRuntimeLogsPage();
    showToast("系统运行日志已清空", "ok");
  });
}
