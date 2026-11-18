(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { current: null, filter: "all", deviceSearch: "", selectedDevices: new Set(), curveDevice: null, curveValues: [], curvePage: 0, detailKey: "", detailData: null, detailTab: "overview", monitorTab: "summary", devicePage: 1, commandPage: 1, commandRows: [], logPage: 1, logRows: [], historyPage: 1, historyRows: [], refreshBusy: false, view: "overview", detailPage: 1, drafts: {}, detailBusy: false };
  const PAGE_SIZE = { devices: 8, commands: 12, logs: 12, history: 8 };
  const CURVE_PAGE_SIZE = 24;
  const kindNames = { wind: "风机", pv: "光伏", storage: "储能", diesel: "柴发", load: "负荷" };
  const typeClass = { wind: "wind", pv: "pv", storage: "storage", diesel: "diesel", load: "load" };

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
    return data;
  }

  function toast(message, type = "success") {
    const node = $("#toast");
    node.textContent = message;
    node.className = `toast show ${type}`;
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => { node.className = "toast"; }, 2800);
  }

  function formatNumber(value, digits = 1) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(digits) : "--";
  }

  function renderState(data) {
    const previous = state.current;
    const previousDevices = new Map((previous?.devices || []).map(item => [`${item.kind}:${item.id}`, item]));
    state.current = {
      ...previous,
      ...data,
      history: data.history || previous?.history || [],
      devices: (data.devices || []).map(item => {
        const old = previousDevices.get(`${item.kind}:${item.id}`);
        return old && !item.presetCurve && old.presetCurve ? { ...old, ...item } : item;
      }),
    };
    data = state.current;
    const summary = data.summary || {};
    $("#heroStatus").textContent = data.running ? (data.controlMode === "open_loop" ? "开环预设运行" : "闭环自动运行") : "系统已暂停";
    $("#periodValue").textContent = data.period ?? "--";
    $("#cycleValue").textContent = data.cycle ?? 0;
    $("#loadKpi").textContent = formatNumber(summary.load);
    $("#renewableKpi").textContent = formatNumber(summary.renewable);
    $("#storageKpi").textContent = formatNumber(summary.storage);
    $("#dieselKpi").textContent = formatNumber(summary.diesel);
    $("#balanceKpi").textContent = formatNumber(summary.balance);
    $("#windValue").textContent = formatNumber(data.weather?.wind_speed, 1);
    $("#irradianceValue").textContent = formatNumber(data.weather?.solor_irrad, 0);
    $("#tempValue").textContent = formatNumber(data.weather?.env_temp, 1);
    $("#runBtn").textContent = data.running ? "停止控制" : "启动控制";
    $("#runBtn").classList.toggle("btn-danger", data.running);
    $("#runBtn").classList.toggle("btn-primary", !data.running);
    $("#heroRunBtn").textContent = data.running ? "停止自动控制" : "启动自动控制";
    $("#controlMode").value = data.controlMode || "closed_loop";
    $("#strategyStatus").textContent = data.running ? (data.controlMode === "open_loop" ? "开环预设" : "闭环自动") : "待机";
    $("#healthMessage").textContent = data.lastError ? `最近错误：${data.lastError}` : (data.running ? "控制周期正常执行" : "控制服务等待操作");
    const backend = data.backend || {};
    $("#backendStatus").textContent = backend.source === "sqlite" ? "已同步" : "未连接";
    $("#backendStatus").className = backend.source === "sqlite" ? "" : "warning";
    $("#backendCycle").textContent = backend.lastCycle ?? "--";
    const scada = data.scada || {};
    const sourceText = scada.source === "http" ? "SCADA HTTP" : (scada.source || "").startsWith("module:") ? "FES 模块" : "本地仿真";
    const totalPoints = Number(scada.ycTotal || 0) + Number(scada.yxTotal || 0);
    const okPoints = Number(scada.ycOk || 0) + Number(scada.yxOk || 0);
    $("#healthScore").textContent = totalPoints ? Math.round(okPoints / totalPoints * 100) : "—";
    $("#healthLabel").textContent = !totalPoints ? "等待采集" : okPoints < totalPoints ? "采集异常" : "采集正常";
    $("#telemetryStatus").textContent = totalPoints ? `${sourceText} ${okPoints}/${totalPoints}` : sourceText;
    $("#telemetryStatus").className = scada.source === "fallback" ? "warning" : "";
    $("#monitorTime").textContent = data.timestamp || "--";
    renderResources(data.devices || []);
    if (!$("#deviceTable").contains(document.activeElement)) renderDevices(data.devices || []);
    populateDeviceSelector(data.devices || []);

    drawChart($("#overviewChart"), data.history || [], ["load", "renewable", "balance"]);
    drawChart($("#monitorChart"), data.history || [], ["load", "renewable", "storage", "diesel"]);
    $("#sidebarTime").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  function renderResources(devices) {
    const totals = { wind: 0, pv: 0, storage: 0, diesel: 0, load: 0 };
    devices.forEach(item => { totals[item.kind] = (totals[item.kind] || 0) + Number(item.power || 0); });
    const rows = [
      ["wind", "风电", totals.wind, "green"], ["pv", "光伏", totals.pv, "yellow"],
      ["storage", "储能", totals.storage, "blue"], ["diesel", "柴发", totals.diesel, "red"],
    ];
    $("#resourceMix").innerHTML = rows.map(([kind, label, value, color]) => `<div class="resource-row"><span class="resource-dot ${color}"></span><span>${label}</span><div class="resource-bar"><i class="${color}" style="width:${Math.min(100, Math.max(4, Math.abs(value) * 2))}%"></i></div><b>${formatNumber(value)} <small>MW</small></b></div>`).join("");
  }

  function renderDevices(devices) {
    PAGE_SIZE.devices = pageCapacity("deviceTable", PAGE_SIZE.devices);
    const search = state.deviceSearch.trim().toLowerCase();
    const filtered = devices.filter(item => (state.filter === "all" || item.kind === state.filter) && (!search || `${item.name} ${item.id}`.toLowerCase().includes(search)));
    const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE.devices));
    state.devicePage = Math.min(state.devicePage, pageCount);
    const start = (state.devicePage - 1) * PAGE_SIZE.devices;
    const visible = filtered.slice(start, start + PAGE_SIZE.devices);
    $("#deviceTable").innerHTML = visible.map(item => {
      const current = Number(item.power || 0);
      const preset = Number(item.presetPower || 0);
      const stateLabel = item.controlState === "local" ? "本地" : "远方";
      const status = Number(item.status) === 1 ? `<span class="status-pill on"><i></i>运行</span>` : `<span class="status-pill off"><i></i>停运</span>`;
      const key = `${item.kind}:${item.id}`;
      return `<tr><td class="select-col"><input class="device-select" type="checkbox" data-device-key="${key}" ${state.selectedDevices.has(key) ? "checked" : ""} aria-label="选择 ${item.name}" /></td><td><div class="device-name"><span class="device-symbol ${typeClass[item.kind] || ""}">${item.kind === "storage" ? "▣" : item.kind === "load" ? "⌁" : "⚡"}</span><div><b>${item.name}</b><small>ID ${item.id}</small></div></div></td><td><span class="type-label">${kindNames[item.kind] || item.kind}</span></td><td>${status}</td><td><b>${formatNumber(current)}</b> MW</td><td>${formatNumber(item.maxPower)} MW</td><td>${item.soc == null ? "—" : `<div class="soc"><span>${formatNumber(item.soc * 100, 0)}%</span><i><em style="width:${Math.max(0, Math.min(100, item.soc * 100))}%"></em></i></div>`}</td><td><select class="device-control" data-kind="${item.kind}" data-id="${item.id}" data-field="control_state"><option value="remote" ${item.controlState === "remote" ? "selected" : ""}>远方 · 自动</option><option value="local" ${item.controlState === "local" ? "selected" : ""}>本地 · 曲线</option></select></td><td><div class="curve-actions"><span title="当前曲线点">${formatNumber(preset)}</span><button class="small-btn" data-curve="${item.kind}:${item.id}">96点曲线</button><button class="small-btn" data-detail="${item.kind}:${item.id}">详情</button></div></td></tr>`;
    }).join("") || `<tr><td colspan="9" class="empty-cell">暂无设备</td></tr>`;
    $$(".device-select").forEach(input => input.addEventListener("change", () => { const key = input.dataset.deviceKey; input.checked ? state.selectedDevices.add(key) : state.selectedDevices.delete(key); updateSelectionUi(); }));
    $$(".device-control").forEach(select => select.addEventListener("change", () => sendDeviceCommand(select.dataset.kind, select.dataset.id, "control_state", select.value)));
    $$(".preset-input").forEach(input => input.addEventListener("change", () => sendDeviceCommand(input.dataset.kind, input.dataset.id, "preset_power", Number(input.value))));
    $$("[data-curve]").forEach(button => button.addEventListener("click", () => openCurve(button.dataset.curve)));
    $$(`[data-detail]`).forEach(button => button.addEventListener("click", () => selectDeviceDetail(button.dataset.detail)));
    updateSelectionUi();
    renderPager("#devicePager", state.devicePage, pageCount, page => { state.devicePage = page; renderDevices(devices); });
  }

  function updateSelectionUi() {
    const count = state.selectedDevices.size;
    const counter = $("#selectedDeviceCount"); if (counter) counter.textContent = `已选 ${count} 台`;
    const selectAll = $("#selectAllDevices");
    if (selectAll) {
      const visible = $$(".device-select");
      selectAll.checked = visible.length > 0 && visible.every(input => input.checked);
      selectAll.indeterminate = visible.some(input => input.checked) && !selectAll.checked;
    }
  }

  async function applyBulkState() {
    const devices = [...state.selectedDevices].map(key => { const [kind, id] = key.split(":"); return { kind, id }; });
    if (!devices.length) { toast("请先选择设备", "error"); return; }
    try {
      const value = $("#bulkState").value;
      const result = await api("/api/device-batch", { method: "POST", body: JSON.stringify({ devices, field: "control_state", value }) });
      renderState(result); state.selectedDevices.clear(); renderDevices(state.current?.devices || []); const okCount = result.batchResults?.filter(item => item.ok).length || 0; const failCount = (result.batchResults?.length || 0) - okCount; toast(failCount ? `已更新 ${okCount} 台，${failCount} 台失败` : `已更新 ${okCount} 台设备`, failCount ? "error" : "success");
    } catch (error) { toast(error.message, "error"); }
  }

  function renderPager(selector, page, pageCount, onChange) {
    const node = $(selector);
    if (!node) return;
    if (pageCount <= 1) { node.innerHTML = ""; return; }
    const buttons = [];
    buttons.push(`<button data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>‹</button>`);
    for (let index = 1; index <= pageCount; index += 1) {
      if (pageCount > 7 && index > 2 && index < pageCount - 1 && Math.abs(index - page) > 1) {
        if (index === 3 || index === pageCount - 2) buttons.push("<span>…</span>");
        continue;
      }
      buttons.push(`<button data-page="${index}" class="${index === page ? "active" : ""}">${index}</button>`);
    }
    buttons.push(`<button data-page="${page + 1}" ${page >= pageCount ? "disabled" : ""}>›</button>`);
    node.innerHTML = buttons.join("");
    node.querySelectorAll("button[data-page]").forEach(button => button.addEventListener("click", () => onChange(Number(button.dataset.page))));
  }

  async function sendDeviceCommand(kind, id, field, value) {
    try { renderState(await api("/api/device-command", { method: "POST", body: JSON.stringify({ kind, id, field, value }) })); toast(field === "control_state" ? (value === "local" ? "已切换为本地曲线控制" : "已切换为远方自动控制") : "默认功率已更新"); }
    catch (error) { toast(error.message, "error"); }
  }

  function populateDeviceSelector(devices) {
    const select = $("#detailDeviceSelect");
    if (!select) return;
    if (!state.detailKey && devices.length) state.detailKey = `${devices[0].kind}:${devices[0].id}`;
    const current = state.detailKey;
    select.innerHTML = devices.map(item => `<option value="${item.kind}:${item.id}">${item.name} · ${kindNames[item.kind] || item.kind}</option>`).join("");
    select.value = current;
    select.onchange = () => selectDeviceDetail(select.value);
  }

  function selectDeviceDetail(key) {
    state.detailKey = key; state.detailPage = 1;
    const select = $("#detailDeviceSelect");
    if (select) select.value = key;
    state.monitorTab = "detail";
    setView("monitor");
    $$(".monitor-tab").forEach(item => item.classList.toggle("active", item.dataset.monitorTab === "detail"));
    $$(".monitor-pane").forEach(item => item.classList.toggle("active", item.id === "monitor-pane-detail"));
  }

  async function refreshDeviceDetail() {
    if (!state.detailKey || state.detailBusy) return;
    state.detailBusy = true;
    const key = state.detailKey;
    const [kind, id] = key.split(":");
    try {
      const data = await api(`/api/device-detail?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
      if (key === state.detailKey) { state.detailData = data; renderDeviceDetail(data); }
    } catch (error) {
      toast(error.message, "error");
    } finally { state.detailBusy = false; }
  }

  function renderDeviceDetail(data) {
    const device = data.device;
    if (!device) return;
    $("#deviceDetailEmpty").hidden = true;
    $("#deviceDetailBody").hidden = false;
    $("#detailSummary").innerHTML = `<div><span>设备</span><b>${device.name}</b></div><div><span>受控状态</span><b class="detail-state ${device.controlState}">${device.controlState === "local" ? "本地 · 默认曲线" : "远方 · 自动决策"}</b></div><div><span>实时功率</span><b>${formatNumber(data.history?.at(-1)?.realtimePower)}</b><small>MW</small></div><div><span>最新指令</span><b>${formatNumber(data.history?.at(-1)?.commandPower)}</b><small>MW</small></div><div><span>控制上下限</span><b>${formatNumber(device.pMin)} ~ ${formatNumber(device.pMax)}</b><small>MW</small></div>`;
    const form = $("#deviceParameterForm");
    ["control_state", "run_stat", "control_step", "control_deadband", "p_min", "p_max", "preset_power"].forEach(field => {
      const input = form?.elements[field];
      if (!input) return;
      const values = { control_state: device.controlState, run_stat: String(device.runStatus), control_step: device.controlStep, control_deadband: device.controlDeadband, p_min: device.pMin, p_max: device.pMax, preset_power: device.presetPower };
      input.dataset.saved = String(values[field]);
      input.value = state.drafts[state.detailKey]?.[field] ?? values[field];
      if (["p_min", "p_max"].includes(field)) { input.disabled = device.kind === "storage"; input.title = input.disabled ? "储能边界由模型充放电额定功率确定" : ""; }
      if (field === "preset_power") { input.min = device.pMin; input.max = device.pMax; }
    });
    $("#deviceEditNote").textContent = Object.keys(state.drafts[state.detailKey] || {}).length ? "有未保存修改 · 自动刷新保留编辑" : "已同步";
    drawDeviceChart($("#deviceChart"), data.history || []);
    const commands = [...(data.commands || [])].reverse();
    const pageSize = pageCapacity("deviceCommandTable", 10);
    const pages = Math.max(1, Math.ceil(commands.length / pageSize));
    state.detailPage = Math.min(state.detailPage, pages);
    renderPager("#detailCommandPager", state.detailPage, pages, page => { state.detailPage = page; renderDeviceDetail(state.detailData); });
    $("#deviceCommandTable").innerHTML = commands.slice((state.detailPage - 1) * pageSize, state.detailPage * pageSize).map(item => `<tr><td>${item.timestamp}</td><td>${item.cycle}</td><td><span class="state-badge ${item.controlState}">${item.controlState === "local" ? "本地" : "远方"}</span></td><td>${formatNumber(item.realtimePower)}</td><td><b>${formatNumber(item.commandPower)}</b></td><td>${formatNumber(item.presetPower)}</td><td>${formatNumber(item.controlStep)} / ${formatNumber(item.controlDeadband)}</td></tr>`).join("") || `<tr><td colspan="7" class="empty-cell">暂无控制时刻记录，请执行单步或启动控制</td></tr>`;
  }

  function drawDeviceChart(canvas, points) {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(420, Math.floor(rect.width || canvas.parentElement?.clientWidth || 680));
    const height = Number(canvas.dataset.chartHeight ||= canvas.getAttribute("height")) || 240;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio; canvas.height = height * ratio;
    const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio); ctx.clearRect(0, 0, width, height);
    const pad = { top: 16, right: 18, bottom: 25, left: 43 };
    const values = points.flatMap(item => [Number(item.realtimePower || 0), Number(item.commandPower || 0), Number(item.presetPower || 0)]);
    const max = Math.max(10, ...values); const min = Math.min(0, ...values); const span = Math.max(10, max - min);
    ctx.font = "11px Inter, sans-serif"; ctx.fillStyle = "#8b96a7"; ctx.strokeStyle = "#e6ebf2"; ctx.lineWidth = 1;
    for (let index = 0; index <= 4; index++) { const y = pad.top + index * (height - pad.top - pad.bottom) / 4; ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke(); ctx.fillText(formatNumber(max - index * span / 4, 0), 4, y + 4); }
    if (!points.length) { ctx.fillText("暂无控制时刻数据", pad.left + 20, height / 2); return; }
    [["realtimePower", "#3c8bd9"], ["commandPower", "#7569e8"], ["presetPower", "#e98b4a"]].forEach(([field, color]) => { ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath(); points.forEach((item, index) => { const x = pad.left + (points.length === 1 ? 0 : index * (width - pad.left - pad.right) / (points.length - 1)); const value = Number(item[field] || 0); const y = pad.top + (max - value) * (height - pad.top - pad.bottom) / span; index ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke(); });
    ctx.fillStyle = "#8b96a7"; ctx.fillText(points[0]?.timestamp?.slice(11, 19) || "", pad.left, height - 6); ctx.fillText(points.at(-1)?.timestamp?.slice(11, 19) || "", width - pad.right - 46, height - 6);
  }

  async function saveDeviceParameters(event) {
    event.preventDefault();
    if (!state.detailKey) return;
    const [kind, id] = state.detailKey.split(":");
    const form = event.currentTarget;
    const draft = state.drafts[state.detailKey] || {};
    const parameters = Object.fromEntries(Object.entries(draft).map(([field, value]) => [field, field === "control_state" ? value : Number(value)]));
    if (!Object.keys(parameters).length) { toast("没有需要保存的修改"); return; }
    if (!form.reportValidity()) return;
    try {
      renderState(await api("/api/device-config", { method: "POST", body: JSON.stringify({ kind, id, parameters }) }));
      delete state.drafts[`${kind}:${id}`];
      await refreshDeviceDetail();
      toast("设备受控状态和控制参数已保存");
    } catch (error) { toast(error.message, "error"); }
  }

  function drawChart(canvas, points, series) {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(360, Math.floor(rect.width || canvas.parentElement?.clientWidth || 640));
    const height = Number(canvas.dataset.chartHeight ||= canvas.getAttribute("height")) || 240;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio; canvas.height = height * ratio;
    const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, width, height);
    const pad = { top: 18, right: 16, bottom: 26, left: 42 };
    const values = points.flatMap(point => series.map(key => Number(point.summary?.[key] || 0)));
    const max = Math.max(10, ...values.map(Math.abs)); const min = Math.min(0, ...values); const span = Math.max(10, max - min);
    ctx.strokeStyle = "#e6ebf2"; ctx.lineWidth = 1; ctx.font = "11px Inter, sans-serif"; ctx.fillStyle = "#8b96a7";
    for (let index = 0; index <= 4; index++) { const y = pad.top + index * (height - pad.top - pad.bottom) / 4; ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke(); ctx.fillText(formatNumber(max - index * span / 4, 0), 4, y + 4); }
    if (!points.length) { ctx.fillStyle = "#9aa5b5"; ctx.fillText("启动控制或执行单步以生成趋势数据", pad.left + 20, height / 2); return; }
    const colors = { load: "#f08c46", renewable: "#2db582", balance: "#7569e8", storage: "#3c8bd9", diesel: "#e05c68" };
    series.forEach(key => { ctx.strokeStyle = colors[key] || "#667085"; ctx.lineWidth = 2; ctx.beginPath(); points.forEach((point, index) => { const x = pad.left + (points.length === 1 ? 0 : index * (width - pad.left - pad.right) / (points.length - 1)); const value = Number(point.summary?.[key] || 0); const y = pad.top + (max - value) * (height - pad.top - pad.bottom) / span; index ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke(); });
    ctx.fillStyle = "#8b96a7"; ctx.fillText(points[0]?.timestamp?.slice(11, 16) || "", pad.left, height - 6); ctx.fillText(points.at(-1)?.timestamp?.slice(11, 16) || "", width - pad.right - 34, height - 6);
  }

  async function refreshState() {
    if (state.refreshBusy || document.hidden) return;
    state.refreshBusy = true;
    try {
      const since = state.current?.cycle ?? -1;
      renderState(await api(`/api/state?lite=1&history_since=${since}`));
      $("#connectionText").textContent = "服务已连接";
      if (state.view === "monitor" && state.monitorTab === "detail") await refreshDeviceDetail();
      if (state.view === "monitor" && state.monitorTab === "commands") await loadCommands();
    } catch (error) { $("#connectionText").textContent = "服务未连接"; }
    finally { state.refreshBusy = false; }
  }

  function pageCapacity(tableId, fallback) {
    const body = document.getElementById(tableId);
    const box = body?.closest(".table-scroll");
    return box?.clientHeight ? Math.max(1, Math.floor((box.clientHeight - 38) / 44)) : fallback;
  }

  function setView(name) {
    state.view = name;
    $$(".nav-item").forEach(button => button.classList.toggle("active", button.dataset.view === name));
    $$(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
    const titles = { overview: "系统总览", monitor: "实时监视", config: "参数配置", logs: "日志查询", history: "历史查询" };
    $("#viewTitle").textContent = titles[name] || titles.overview;
    if (name === "config") loadConfig(); if (name === "logs") loadLogs(); if (name === "history") loadHistory(); if (name === "monitor") { if (state.monitorTab === "commands") loadCommands(); if (state.monitorTab === "detail") refreshDeviceDetail(); }
    window.setTimeout(() => $$("canvas").forEach(canvas => { if (canvas.id === "historyChart") return; if (canvas.id === "overviewChart" || canvas.id === "monitorChart") drawChart(canvas, state.current?.history || [], canvas.id === "monitorChart" ? ["load", "renewable", "storage", "diesel"] : ["load", "renewable", "balance"]); }), 30);
  }

  async function control(action) { try { renderState(await api("/api/control", { method: "POST", body: JSON.stringify({ action }) })); toast(action === "step" ? "单步控制周期已执行" : action === "start" ? "自动控制已启动" : "自动控制已停止"); } catch (error) { toast(error.message, "error"); } }

  async function loadConfig() {
    try { const data = await api("/api/config"); $("#modelFile").textContent = data.model?.split(/[\\/]/).pop() || "model_real.e"; $("#footerModel").textContent = $("#modelFile").textContent; $("#parameterForm").innerHTML = (data.parameters || []).map(item => `<label><span>${item.description || item.name}<em>${item.name}</em></span><input name="${item.name}" type="number" step="any" value="${item.value}" /></label>`).join(""); const simulation = data.simulation || {}; Object.entries(simulation).forEach(([key, value]) => { const input = $(`#simulationForm [name="${key}"]`); if (input) input.value = value; }); }
    catch (error) { toast(error.message, "error"); }
  }

  async function loadLogs() { try { const query = new URLSearchParams({ keyword: $("#logKeyword").value, level: $("#logLevel").value, limit: "1000" }); const data = await api(`/api/logs?${query}`); state.logRows = data.items || []; state.logPage = 1; renderLogs(); } catch (error) { toast(error.message, "error"); } }

  function renderLogs() {
    PAGE_SIZE.logs = pageCapacity("logTable", PAGE_SIZE.logs);
    const rows = state.logRows || [];
    const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE.logs));
    state.logPage = Math.min(state.logPage, pageCount);
    const start = (state.logPage - 1) * PAGE_SIZE.logs;
    $("#logTable").innerHTML = rows.slice(start, start + PAGE_SIZE.logs).map(item => `<tr><td class="log-time">${item.timestamp}</td><td><span class="level ${item.level.toLowerCase()}">${item.level}</span></td><td>${escapeHtml(item.message)}</td></tr>`).join("") || `<tr><td colspan="3" class="empty-cell">暂无匹配日志</td></tr>`;
    renderPager("#logPager", state.logPage, pageCount, page => { state.logPage = page; renderLogs(); });
  }

  async function loadHistory() { try { const limit = $("#historyLimit").value; const data = await api(`/api/history?limit=${limit}`); const items = data.items || []; state.historyRows = [...items].reverse(); state.historyPage = 1; $("#historyCount").textContent = `${items.length} 个采样点`; drawChart($("#historyChart"), items, ["load", "renewable", "balance"]); renderHistoryTable(); } catch (error) { toast(error.message, "error"); } }

  function renderHistoryTable() {
    PAGE_SIZE.history = pageCapacity("historyTable", PAGE_SIZE.history);
    const rows = state.historyRows || [];
    const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE.history));
    state.historyPage = Math.min(state.historyPage, pageCount);
    const start = (state.historyPage - 1) * PAGE_SIZE.history;
    $("#historyTable").innerHTML = rows.slice(start, start + PAGE_SIZE.history).map(item => `<tr><td>${item.timestamp}</td><td>${formatNumber(item.summary?.load)}</td><td>${formatNumber(item.summary?.renewable)}</td><td>${formatNumber(item.summary?.storage)}</td><td>${formatNumber(item.summary?.diesel)}</td><td>${formatNumber(item.summary?.balance)}</td></tr>`).join("") || `<tr><td colspan="6" class="empty-cell">暂无历史数据，请先执行控制周期</td></tr>`;
    renderPager("#historyPager", state.historyPage, pageCount, page => { state.historyPage = page; renderHistoryTable(); });
  }

  async function loadCommands() {
    try {
      const data = await api("/api/commands?limit=1000");
      state.commandRows = data.items || [];
      renderCommands();
    } catch (error) { toast(error.message, "error"); }
  }

  function renderCommands() {
    PAGE_SIZE.commands = pageCapacity("allCommandTable", PAGE_SIZE.commands);
    const rows = state.commandRows || [];
    const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE.commands));
    state.commandPage = Math.min(state.commandPage, pageCount);
    const start = (state.commandPage - 1) * PAGE_SIZE.commands;
    const visible = [...rows].reverse().slice(start, start + PAGE_SIZE.commands);
    $("#allCommandTable").innerHTML = visible.map(item => `<tr><td>${item.timestamp}</td><td>${item.cycle}</td><td><b>${item.name}</b><small class="table-sub">${kindNames[item.kind] || item.kind} · ID ${item.id}</small></td><td><span class="state-badge ${item.controlState}">${item.controlState === "local" ? "本地" : "远方"}</span></td><td>${formatNumber(item.realtimePower)}</td><td><b>${formatNumber(item.commandPower)}</b></td><td>${formatNumber(item.presetPower)}</td><td>${formatNumber(item.controlStep)} / ${formatNumber(item.controlDeadband)}</td></tr>`).join("") || `<tr><td colspan="8" class="empty-cell">暂无控制时刻记录，请先执行单步或启动控制</td></tr>`;
    renderPager("#commandPager", state.commandPage, pageCount, page => { state.commandPage = page; renderCommands(); });
  }

  function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character])); }

  function renderCurveEditor() {
    const device = state.curveDevice;
    if (!device) return;
    const pageCount = Math.ceil(state.curveValues.length / CURVE_PAGE_SIZE);
    state.curvePage = Math.max(0, Math.min(state.curvePage, pageCount - 1));
    const start = state.curvePage * CURVE_PAGE_SIZE;
    const visible = state.curveValues.slice(start, start + CURVE_PAGE_SIZE);
    $("#curveInputs").innerHTML = visible.map((value, offset) => {
      const index = start + offset;
      const hour = String(Math.floor(index / 4)).padStart(2, "0");
      const minute = String((index % 4) * 15).padStart(2, "0");
      const minPower = Number(device.pMin);
      return `<label><span>${hour}:${minute}</span><input data-curve-index="${index}" type="number" step="0.1" min="${minPower}" max="${device.pMax}" value="${value}" /></label>`;
    }).join("");
    $("#curvePageNote").textContent = `第 ${state.curvePage + 1} / ${pageCount} 组 · ${start + 1}-${Math.min(start + CURVE_PAGE_SIZE, state.curveValues.length)} 点`;
    $("#curvePrevBtn").disabled = state.curvePage === 0;
    $("#curveNextBtn").disabled = state.curvePage >= pageCount - 1;
  }

  async function openCurve(key) {
    const [kind, id] = key.split(":"); const device = (state.current?.devices || []).find(item => item.kind === kind && String(item.id) === id); if (!device) return;
    let detail;
    try { const payload = await api(`/api/device-detail?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`); detail = { ...device, ...payload.device }; } catch (error) { toast(error.message, "error"); return; }
    state.curveDevice = detail; state.curveValues = Array.from({ length: 96 }, (_, index) => Number(detail.presetCurve?.[index] ?? detail.presetPower ?? 0)); state.curvePage = 0; state.savedCurve = [...state.curveValues];
    $("#curveTitle").textContent = `${device.name} · 96点默认功率曲线`; $("#curveRangeNote").textContent = `功率范围：${formatNumber(detail.pMin)} ~ ${formatNumber(detail.pMax)} MW`;
    renderCurveEditor();
    $("#curveModal").hidden = false;
  }

  async function saveCurve() {
    if (!state.curveDevice) return;
    $$("[data-curve-index]").forEach(input => { state.curveValues[Number(input.dataset.curveIndex)] = input.value.trim() === "" ? NaN : Number(input.value); });
    const values = state.curveValues; const device = state.curveDevice;
    if (values.some(value => !Number.isFinite(value) || value < device.pMin || value > device.pMax)) { toast(`全部 96 点须在 ${device.pMin} ~ ${device.pMax} MW 内`, "error"); return; }
    try { renderState(await api("/api/device-command", { method: "POST", body: JSON.stringify({ kind: device.kind, id: device.id, field: "preset_curve", value: values }) })); $("#curveSaveNote").textContent = "已保存"; toast("96 点默认功率曲线已保存"); window.setTimeout(() => { $("#curveModal").hidden = true; $("#curveSaveNote").textContent = ""; }, 500); } catch (error) { toast(error.message, "error"); }
  }

  function bind() {
    $$(".nav-item").forEach(button => button.addEventListener("click", () => setView(button.dataset.view)));
    $$('[data-view-jump]').forEach(button => button.addEventListener("click", () => setView(button.dataset.viewJump)));
    $("#runBtn").addEventListener("click", () => control(state.current?.running ? "stop" : "start")); $("#heroRunBtn").addEventListener("click", () => control(state.current?.running ? "stop" : "start")); $("#stepBtn").addEventListener("click", () => control("step"));
    $("#controlMode").addEventListener("change", async event => { try { renderState(await api("/api/mode", { method: "POST", body: JSON.stringify({ mode: event.target.value }) })); toast(event.target.value === "closed_loop" ? "已切换为闭环自动模式" : "已切换为开环预设模式"); } catch (error) { toast(error.message, "error"); } });
    $$(".filter-chip").forEach(button => button.addEventListener("click", () => { $$(".filter-chip").forEach(item => item.classList.remove("active")); button.classList.add("active"); state.filter = button.dataset.filter; renderDevices(state.current?.devices || []); }));
    $("#selectAllDevices").addEventListener("change", event => { $$(".device-select").forEach(input => { input.checked = event.target.checked; const key = input.dataset.deviceKey; event.target.checked ? state.selectedDevices.add(key) : state.selectedDevices.delete(key); }); updateSelectionUi(); });
    $("#deviceSearch").addEventListener("input", event => { state.deviceSearch = event.target.value; state.devicePage = 1; renderDevices(state.current?.devices || []); });
    $("#applyBulkState").addEventListener("click", applyBulkState);
    $("#saveConfigBtn").addEventListener("click", async event => { event.preventDefault(); const values = Object.fromEntries([...$("#parameterForm").querySelectorAll("input")].map(input => [input.name, Number(input.value)])); try { await api("/api/config", { method: "POST", body: JSON.stringify({ parameters: values }) }); $("#saveNote").textContent = "已保存"; toast("控制参数已保存"); window.setTimeout(() => $("#saveNote").textContent = "", 2000); } catch (error) { toast(error.message, "error"); } });
    $("#simulationForm").addEventListener("submit", async event => { event.preventDefault(); const values = Object.fromEntries([...event.currentTarget.querySelectorAll("input")].map(input => [input.name, Number(input.value)])); try { renderState(await api("/api/simulation", { method: "POST", body: JSON.stringify(values) })); toast("仿真遥测已应用"); } catch (error) { toast(error.message, "error"); } });
    $("#queryLogsBtn").addEventListener("click", loadLogs); $("#refreshLogsBtn").addEventListener("click", loadLogs); $("#refreshHistoryBtn").addEventListener("click", loadHistory); $("#historyLimit").addEventListener("change", loadHistory); $("#refreshCommandsBtn").addEventListener("click", loadCommands);
    $$(".monitor-tab").forEach(button => button.addEventListener("click", () => { state.monitorTab = button.dataset.monitorTab; $$(".monitor-tab").forEach(item => item.classList.toggle("active", item === button)); $$(".monitor-pane").forEach(item => item.classList.toggle("active", item.id === `monitor-pane-${state.monitorTab}`)); if (state.monitorTab === "devices") renderDevices(state.current?.devices || []); if (state.monitorTab === "commands") loadCommands(); if (state.monitorTab === "detail") refreshDeviceDetail(); window.setTimeout(() => { if (state.monitorTab === "summary") drawChart($("#monitorChart"), state.current?.history || [], ["load", "renewable", "storage", "diesel"]); if (state.monitorTab === "detail" && state.detailData) drawDeviceChart($("#deviceChart"), state.detailData.history || []); }, 30); }));
    $("#deviceParameterForm").addEventListener("submit", saveDeviceParameters);
    $("#deviceParameterForm").addEventListener("input", event => {
      const input = event.target; if (!input.name) return;
      const draft = state.drafts[state.detailKey] ||= {};
      if (input.value === input.dataset.saved) delete draft[input.name]; else draft[input.name] = input.value;
      $("#deviceEditNote").textContent = Object.keys(draft).length ? "有未保存修改 · 自动刷新保留编辑" : "已同步";
    });
    $("#resetDeviceBtn").addEventListener("click", () => { delete state.drafts[state.detailKey]; if (state.detailData) renderDeviceDetail(state.detailData); });
    $("#logKeyword").addEventListener("keydown", event => { if (event.key === "Enter") loadLogs(); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });
    $$('[data-section-tab]').forEach(button => button.addEventListener("click", () => {
      const view = button.closest(".view");
      view.querySelectorAll('[data-section-tab]').forEach(item => item.classList.toggle("active", item === button));
      view.querySelectorAll('.section-pane').forEach(item => item.classList.toggle("active", item.id === button.dataset.sectionTab));
      if (state.view === "history") { renderHistoryTable(); drawChart($("#historyChart"), [...state.historyRows].reverse(), ["load", "renewable", "balance"]); }
      if (state.view === "overview") drawChart($("#overviewChart"), state.current?.history || [], ["load", "renewable", "balance"]);
    }));
    $$(".detail-tab").forEach(button => button.addEventListener("click", () => { state.detailTab = button.dataset.detailTab; $$(".detail-tab").forEach(item => item.classList.toggle("active", item === button)); $$(".detail-pane").forEach(item => item.classList.toggle("active", item.id === `detail-pane-${state.detailTab}`)); if (state.detailData) window.setTimeout(() => renderDeviceDetail(state.detailData), 30); }));
    $("#detailCurveBtn").addEventListener("click", () => { if (state.detailKey) openCurve(state.detailKey); });
    $("#closeCurveBtn").addEventListener("click", () => { $("#curveModal").hidden = true; }); $("#curveModal").addEventListener("click", event => { if (event.target.id === "curveModal") event.currentTarget.hidden = true; }); $("#saveCurveBtn").addEventListener("click", saveCurve); $("#fillCurveBtn").addEventListener("click", () => { const value = Number(state.curveDevice?.presetPower || 0); state.curveValues.fill(value); renderCurveEditor(); });
    $("#restoreCurveBtn").addEventListener("click", () => { state.curveValues = [...state.savedCurve]; renderCurveEditor(); });
    $("#rampCurveBtn").addEventListener("click", () => {
      const first = Number($("#rampStart").value), last = Number($("#rampEnd").value);
      const device = state.curveDevice;
      if (![first, last].every(value => Number.isFinite(value) && value >= device.pMin && value <= device.pMax)) { toast("起止功率超出设备范围", "error"); return; }
      const start = state.curvePage * CURVE_PAGE_SIZE;
      for (let i = 0; i < CURVE_PAGE_SIZE; i++) state.curveValues[start + i] = Number((first + (last - first) * i / (CURVE_PAGE_SIZE - 1)).toFixed(3));
      renderCurveEditor();
    });
    $("#curvePrevBtn").addEventListener("click", () => { $$("[data-curve-index]").forEach(input => { state.curveValues[Number(input.dataset.curveIndex)] = input.value.trim() === "" ? NaN : Number(input.value); }); state.curvePage -= 1; renderCurveEditor(); }); $("#curveNextBtn").addEventListener("click", () => { $$("[data-curve-index]").forEach(input => { state.curveValues[Number(input.dataset.curveIndex)] = input.value.trim() === "" ? NaN : Number(input.value); }); state.curvePage += 1; renderCurveEditor(); });
    window.addEventListener("resize", () => { renderDevices(state.current?.devices || []); renderCommands(); renderHistoryTable(); renderLogs(); if (state.detailData) renderDeviceDetail(state.detailData); if (state.current) { drawChart($("#overviewChart"), state.current.history || [], ["load", "renewable", "balance"]); drawChart($("#monitorChart"), state.current.history || [], ["load", "renewable", "storage", "diesel"]); } if (state.detailData) drawDeviceChart($("#deviceChart"), state.detailData.history || []); });
  }

  bind(); refreshState(); window.setInterval(refreshState, 2000);
})();
