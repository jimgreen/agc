// 受控设备控制参数配置与 96 点预设曲线交互编辑模块

const DevCfgState = {
  items: [],
  counts: { total: 0, closed: 0, track: 0, open: 0 },
  currentSlotIndex: 0,
  currentSlotLabel: "00:00",
  drafts: {},       // key -> { control_state, p_min, p_max, control_step, control_deadband, curve_interp }
  dirtyKeys: new Set(),
  invalidCells: new Set(),
  groupFilter: "all",
  stateFilter: "all",
  keyword: "",
  selectedKey: null,
  curve: null,      // { dev_type, dev_name, set_type, points: number[96], labels: string[96], curve_interp, effective_min, effective_max, current_kw, current_slot_index }
  curveDirty: false,
  curveHoverIdx: -1,
  curveDragging: false,
  lastDragIdx: -1,
  lastDragVal: null,
  dragLockedRange: null, // 拖拽期间锁定 Y 轴视窗，避免边拖边缩放导致指针漂移
  curveBounds: null // 供画布指针反算 Y 值使用 { minY, maxY, padL, padR, padT, padB, w, h }
};

function devCfgKey(item) {
  return `${item.dev_type}::${item.dev_name}::${item.set_type}`;
}

function devCfgControlStateMeta(state) {
  const s = String(state || "closed").trim().toLowerCase();
  if (s === "track") return { cls: "badge-warn", label: t("devcfg.stateTrack") };
  if (s === "open") return { cls: "badge-neutral", label: t("devcfg.stateOpen") };
  return { cls: "badge-ok", label: t("devcfg.stateClosed") };
}

function _parseOptNum(raw) {
  if (raw === null || raw === undefined) return { empty: true, ok: true, value: null };
  const s = String(raw).trim();
  if (s === "") return { empty: true, ok: true, value: null };
  const n = Number(s);
  if (!Number.isFinite(n)) return { empty: false, ok: false, value: null };
  return { empty: false, ok: true, value: n };
}

function _getRowDraft(item) {
  const key = devCfgKey(item);
  if (DevCfgState.drafts[key]) return DevCfgState.drafts[key];
  return {
    control_state: item.control_state || "closed",
    p_min: item.p_min === null || item.p_min === undefined ? "" : String(item.p_min),
    p_max: item.p_max === null || item.p_max === undefined ? "" : String(item.p_max),
    control_step: item.control_step === null || item.control_step === undefined ? "" : String(item.control_step),
    control_deadband: item.control_deadband === null || item.control_deadband === undefined ? "0" : String(item.control_deadband),
    curve_interp: item.curve_interp || "step"
  };
}

function _validateRowDraft(item, draft) {
  const errs = {};
  const minP = _parseOptNum(draft.p_min);
  const maxP = _parseOptNum(draft.p_max);
  const stepP = _parseOptNum(draft.control_step);
  const deadP = _parseOptNum(draft.control_deadband);

  if (!minP.ok) errs.p_min = true;
  if (!maxP.ok) errs.p_max = true;
  if (!stepP.ok || (!stepP.empty && stepP.value < 0)) errs.control_step = true;
  if (!deadP.ok || (!deadP.empty && deadP.value < 0)) errs.control_deadband = true;

  if (!item.allow_negative) {
    if (!minP.empty && minP.ok && minP.value < 0) errs.p_min = true;
    if (!maxP.empty && maxP.ok && maxP.value < 0) errs.p_max = true;
  }
  const effMin = !minP.empty && minP.ok ? minP.value : item.default_min;
  const effMax = !maxP.empty && maxP.ok ? maxP.value : item.default_max;
  if (
    effMin !== null && effMin !== undefined &&
    effMax !== null && effMax !== undefined &&
    Number(effMin) > Number(effMax) + 1e-9
  ) {
    if (!minP.empty) errs.p_min = true;
    if (!maxP.empty) errs.p_max = true;
  }
  return errs;
}

function _recomputeValidation() {
  DevCfgState.invalidCells.clear();
  DevCfgState.items.forEach((item) => {
    const key = devCfgKey(item);
    const draft = _getRowDraft(item);
    const errs = _validateRowDraft(item, draft);
    Object.keys(errs).forEach((field) => {
      DevCfgState.invalidCells.add(`${key}::${field}`);
    });
  });
  const saveBtn = document.getElementById("devCfgSaveBtn");
  if (saveBtn) {
    const dirtyCnt = DevCfgState.dirtyKeys.size;
    saveBtn.disabled = DevCfgState.invalidCells.size > 0 || dirtyCnt === 0;
    saveBtn.textContent = dirtyCnt > 0 ? `${t("action.save")} (${dirtyCnt})` : t("action.save");
  }
}

async function refreshDeviceConfigsPage(preserveDrafts = true) {
  const data = await apiGet("/api/device-configs");
  DevCfgState.items = data.items || [];
  DevCfgState.counts = data.counts || { total: 0, closed: 0, track: 0, open: 0 };
  DevCfgState.currentSlotIndex = Number(data.current_slot_index || 0);
  DevCfgState.currentSlotLabel = data.current_slot_label || "00:00";

  if (!preserveDrafts) {
    DevCfgState.drafts = {};
    DevCfgState.dirtyKeys.clear();
  }
  if (!DevCfgState.selectedKey && DevCfgState.items.length > 0) {
    DevCfgState.selectedKey = devCfgKey(DevCfgState.items[0]);
  }
  renderDeviceConfigsToolbar();
  renderDeviceConfigsTable();
  if (DevCfgState.selectedKey && !DevCfgState.curveDirty) {
    const selItem = DevCfgState.items.find((it) => devCfgKey(it) === DevCfgState.selectedKey);
    if (selItem) {
      await loadDeviceCurveEditor(selItem, false);
    }
  }
}

async function refreshDeviceConfigsLiveOnly() {
  // 后台轮询时仅更新只读遥测与当前时段，绝不打断用户正在输入的单元格
  try {
    const data = await apiGet("/api/device-configs");
    const byKey = new Map((data.items || []).map((it) => [devCfgKey(it), it]));
    DevCfgState.currentSlotIndex = Number(data.current_slot_index || 0);
    DevCfgState.currentSlotLabel = data.current_slot_label || "00:00";
    DevCfgState.items.forEach((it) => {
      const fresh = byKey.get(devCfgKey(it));
      if (!fresh) return;
      it.current_kw = fresh.current_kw;
      it.online = fresh.online;
      it.curve_now_kw = fresh.curve_now_kw;
      it.has_curve = fresh.has_curve;
      const key = devCfgKey(it);
      const curEl = document.querySelector(`[data-live-cur="${CSS.escape(key)}"]`);
      if (curEl) curEl.textContent = fmtNum(it.current_kw, 2);
      const cNowEl = document.querySelector(`[data-live-cnow="${CSS.escape(key)}"]`);
      if (cNowEl) cNowEl.textContent = it.curve_now_kw !== null && it.curve_now_kw !== undefined ? fmtNum(it.curve_now_kw, 1) : "—";
    });
    if (DevCfgState.curve) {
      const selItem = DevCfgState.items.find((it) => devCfgKey(it) === DevCfgState.selectedKey);
      if (selItem) DevCfgState.curve.current_kw = selItem.current_kw;
      DevCfgState.curve.current_slot_index = DevCfgState.currentSlotIndex;
      drawDeviceCurveCanvas();
    }
  } catch (_e) {}
}

function getFilteredDeviceConfigItems() {
  const kw = (DevCfgState.keyword || "").trim().toLowerCase();
  return DevCfgState.items.filter((item) => {
    if (DevCfgState.groupFilter !== "all" && item.group !== DevCfgState.groupFilter) return false;
    const draft = _getRowDraft(item);
    if (DevCfgState.stateFilter !== "all" && draft.control_state !== DevCfgState.stateFilter) return false;
    if (kw) {
      const hay = `${item.category} ${item.dev_name} ${item.dev_type} ${item.set_type}`.toLowerCase();
      if (!hay.includes(kw)) return false;
    }
    return true;
  });
}

function renderDeviceConfigsToolbar() {
  const counts = { total: DevCfgState.items.length, closed: 0, track: 0, open: 0 };
  DevCfgState.items.forEach((item) => {
    const st = _getRowDraft(item).control_state || "closed";
    counts[st] = (counts[st] || 0) + 1;
  });
  const setTxt = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(val);
  };
  setTxt("dcStatTotal", counts.total);
  setTxt("dcStatClosed", counts.closed);
  setTxt("dcStatTrack", counts.track);
  setTxt("dcStatOpen", counts.open);
  setTxt("dcNowSlotBadge", DevCfgState.currentSlotLabel || "00:00");
  _recomputeValidation();
}

function renderDeviceConfigsTable() {
  const wrap = document.getElementById("devCfgTableWrap");
  if (!wrap) return;
  const rows = getFilteredDeviceConfigItems();
  if (!rows.length) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">—</td></tr></tbody></table>`;
    return;
  }

  wrap.innerHTML = `
    <table class="data-table devcfg-table">
      <thead>
        <tr>
          <th>${t("col.category")}</th>
          <th>${t("col.device")}</th>
          <th>${t("devcfg.ctrlVar")}</th>
          <th style="width:94px;">${t("devcfg.ctrlState")}</th>
          <th class="num">${t("col.currentVal")} (kW)</th>
          <th class="num" style="width:86px;">${t("devcfg.pMin")} (kW)</th>
          <th class="num" style="width:86px;">${t("devcfg.pMax")} (kW)</th>
          <th class="num" style="width:82px;">${t("devcfg.step")} (kW)</th>
          <th class="num" style="width:78px;">${t("devcfg.deadband")} (kW)</th>
          <th class="num">${t("devcfg.curveNow")}</th>
          <th style="width:58px;">${t("devcfg.curve")}</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((item) => {
          const key = devCfgKey(item);
          const draft = _getRowDraft(item);
          const errs = _validateRowDraft(item, draft);
          const isSel = key === DevCfgState.selectedKey;
          const isDirty = DevCfgState.dirtyKeys.has(key);
          const stCls = draft.control_state === "track" ? "state-track" : (draft.control_state === "open" ? "state-open" : "state-closed");
          const minPh = item.default_min !== null && item.default_min !== undefined ? String(item.default_min) : "";
          const maxPh = item.default_max !== null && item.default_max !== undefined ? String(item.default_max) : "";
          const stepPh = item.default_step !== null && item.default_step !== undefined ? String(item.default_step) : "auto";
          const cNowTxt = item.curve_now_kw !== null && item.curve_now_kw !== undefined ? fmtNum(item.curve_now_kw, 1) : "—";
          return `
            <tr data-devcfg-row="${escapeHtml(key)}" class="${isSel ? "is-selected" : ""} ${isDirty ? "is-dirty-row" : ""}">
              <td><span class="muted">${escapeHtml(item.category)}</span></td>
              <td class="devcfg-name-cell">
                <span class="online-dot ${item.online ? "on" : "off"}"></span>
                <strong>${escapeHtml(item.dev_name)}</strong>
                ${isDirty ? `<span class="dirty-dot" title="未保存"></span>` : ""}
              </td>
              <td class="mono muted">${escapeHtml(item.set_type)}</td>
              <td class="cell-edit">
                <select class="grid-control ${stCls}" data-cfg-key="${escapeHtml(key)}" data-cfg-field="control_state">
                  <option value="closed" ${draft.control_state === "closed" ? "selected" : ""}>${t("devcfg.stateClosed")}</option>
                  <option value="track" ${draft.control_state === "track" ? "selected" : ""}>${t("devcfg.stateTrack")}</option>
                  <option value="open" ${draft.control_state === "open" ? "selected" : ""}>${t("devcfg.stateOpen")}</option>
                </select>
              </td>
              <td class="num" data-live-cur="${escapeHtml(key)}">${fmtNum(item.current_kw, 2)}</td>
              <td class="cell-edit num ${errs.p_min ? "is-invalid" : ""}">
                <input type="number" step="0.1" class="grid-control num" data-cfg-key="${escapeHtml(key)}" data-cfg-field="p_min" value="${escapeHtml(draft.p_min)}" placeholder="${escapeHtml(minPh)}" />
              </td>
              <td class="cell-edit num ${errs.p_max ? "is-invalid" : ""}">
                <input type="number" step="0.1" class="grid-control num" data-cfg-key="${escapeHtml(key)}" data-cfg-field="p_max" value="${escapeHtml(draft.p_max)}" placeholder="${escapeHtml(maxPh)}" />
              </td>
              <td class="cell-edit num ${errs.control_step ? "is-invalid" : ""}">
                <input type="number" min="0" step="0.1" class="grid-control num" data-cfg-key="${escapeHtml(key)}" data-cfg-field="control_step" value="${escapeHtml(draft.control_step)}" placeholder="${escapeHtml(stepPh)}" />
              </td>
              <td class="cell-edit num ${errs.control_deadband ? "is-invalid" : ""}">
                <input type="number" min="0" step="0.1" class="grid-control num" data-cfg-key="${escapeHtml(key)}" data-cfg-field="control_deadband" value="${escapeHtml(draft.control_deadband)}" placeholder="0" />
              </td>
              <td class="num ${draft.control_state === "track" ? "val-warn" : "muted"}" data-live-cnow="${escapeHtml(key)}">${cNowTxt}</td>
              <td>
                <button type="button" class="btn btn-xs ${item.has_curve ? "btn-primary" : ""}" data-open-curve="${escapeHtml(key)}">${t("devcfg.curve")}</button>
              </td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;

  wrap.querySelectorAll("tr[data-devcfg-row]").forEach((tr) => {
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("input, select, button")) return;
      const key = tr.dataset.devcfgRow;
      selectDeviceForCurve(key);
    });
  });

  wrap.querySelectorAll("button[data-open-curve]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      selectDeviceForCurve(btn.dataset.openCurve);
    });
  });

  wrap.querySelectorAll(".grid-control[data-cfg-key]").forEach((ctrl) => {
    const evtName = ctrl.tagName === "SELECT" ? "change" : "input";
    ctrl.addEventListener(evtName, () => {
      const key = ctrl.dataset.cfgKey;
      const field = ctrl.dataset.cfgField;
      const item = DevCfgState.items.find((it) => devCfgKey(it) === key);
      if (!item) return;
      const draft = { ..._getRowDraft(item) };
      draft[field] = ctrl.value;
      DevCfgState.drafts[key] = draft;
      DevCfgState.dirtyKeys.add(key);

      if (field === "control_state") {
        ctrl.classList.remove("state-closed", "state-track", "state-open");
        ctrl.classList.add(
          draft.control_state === "track" ? "state-track" : (draft.control_state === "open" ? "state-open" : "state-closed")
        );
      }
      const tr = ctrl.closest("tr");
      if (tr) {
        tr.classList.add("is-dirty-row");
        const errs = _validateRowDraft(item, draft);
        ["p_min", "p_max", "control_step", "control_deadband"].forEach((f) => {
          const cell = tr.querySelector(`[data-cfg-field="${f}"]`)?.closest("td");
          if (cell) cell.classList.toggle("is-invalid", Boolean(errs[f]));
        });
      }
      renderDeviceConfigsToolbar();
      // 如果编辑的是当前右侧曲线对应设备的上下限，即时同步画布安全带
      if (key === DevCfgState.selectedKey && DevCfgState.curve && (field === "p_min" || field === "p_max")) {
        const minP = _parseOptNum(draft.p_min);
        const maxP = _parseOptNum(draft.p_max);
        DevCfgState.curve.effective_min = !minP.empty && minP.ok ? minP.value : item.default_min;
        DevCfgState.curve.effective_max = !maxP.empty && maxP.ok ? maxP.value : item.default_max;
        drawDeviceCurveCanvas();
      }
    });
  });
}

async function selectDeviceForCurve(key) {
  if (!key) return;
  const item = DevCfgState.items.find((it) => devCfgKey(it) === key);
  if (!item) return;
  DevCfgState.selectedKey = key;
  document.querySelectorAll("#devCfgTableWrap tr[data-devcfg-row]").forEach((tr) => {
    tr.classList.toggle("is-selected", tr.dataset.devcfgRow === key);
  });
  await loadDeviceCurveEditor(item, true);
}

async function loadDeviceCurveEditor(item, forceReload = true) {
  if (!forceReload && DevCfgState.curve && devCfgKey(DevCfgState.curve) === devCfgKey(item)) {
    drawDeviceCurveCanvas();
    return;
  }
  const qs = new URLSearchParams({
    dev_type: item.dev_type,
    dev_name: item.dev_name,
    set_type: item.set_type
  });
  const data = await apiGet(`/api/device-configs/curve?${qs.toString()}`);
  const draft = _getRowDraft(item);
  const minP = _parseOptNum(draft.p_min);
  const maxP = _parseOptNum(draft.p_max);
  data.effective_min = !minP.empty && minP.ok ? minP.value : item.default_min;
  data.effective_max = !maxP.empty && maxP.ok ? maxP.value : item.default_max;
  data.allow_negative = Boolean(item.allow_negative);
  data.curve_interp = draft.curve_interp || data.curve_interp || "step";

  DevCfgState.curve = data;
  DevCfgState.curveDirty = false;
  DevCfgState.dragLockedRange = null;
  renderDeviceCurveHeader();
  drawDeviceCurveCanvas();
  renderDeviceCurveGrid();
}

function renderDeviceCurveHeader() {
  const c = DevCfgState.curve;
  const titleEl = document.getElementById("dcCurveTitle");
  const metaEl = document.getElementById("dcCurveMeta");
  const interpSel = document.getElementById("dcCurveInterpSelect");
  const saveBtn = document.getElementById("dcSaveCurveBtn");
  if (!c) {
    if (titleEl) titleEl.textContent = t("devcfg.curveEditor");
    if (metaEl) metaEl.textContent = "—";
    if (saveBtn) saveBtn.disabled = true;
    return;
  }
  if (titleEl) titleEl.textContent = c.dev_name;
  if (metaEl) {
    const lo = c.effective_min !== null && c.effective_min !== undefined ? fmtNum(c.effective_min, 1) : "--";
    const hi = c.effective_max !== null && c.effective_max !== undefined ? fmtNum(c.effective_max, 1) : "--";
    metaEl.textContent = `${c.set_type} · [${lo}, ${hi}] ${c.unit || "kW"}`;
  }
  if (interpSel) interpSel.value = c.curve_interp || "step";
  if (saveBtn) {
    saveBtn.disabled = false;
    saveBtn.textContent = DevCfgState.curveDirty ? `${t("devcfg.saveCurve")} *` : t("devcfg.saveCurve");
  }
}

function _computeCurveYRange(c) {
  const vals = (c.points || []).map((v) => Number(v) || 0);
  const hasMin = c.effective_min !== null && c.effective_min !== undefined && Number.isFinite(Number(c.effective_min));
  const hasMax = c.effective_max !== null && c.effective_max !== undefined && Number.isFinite(Number(c.effective_max));
  const validBounds = hasMin && hasMax && Number(c.effective_min) < Number(c.effective_max);

  let minY = vals.length ? Math.min(...vals) : 0;
  let maxY = vals.length ? Math.max(...vals) : 10;
  if (validBounds) {
    minY = Math.min(minY, Number(c.effective_min));
    maxY = Math.max(maxY, Number(c.effective_max));
  } else {
    if (hasMin) minY = Math.min(minY, Number(c.effective_min));
    if (hasMax && Number(c.effective_max) > minY) maxY = Math.max(maxY, Number(c.effective_max));
  }
  if (c.current_kw !== null && c.current_kw !== undefined && Number.isFinite(Number(c.current_kw))) {
    minY = Math.min(minY, Number(c.current_kw));
    maxY = Math.max(maxY, Number(c.current_kw));
  }
  if (Math.abs(maxY - minY) < 1e-3) {
    if (c.allow_negative) {
      minY -= 10;
      maxY += 10;
    } else {
      minY = Math.max(0, minY - 2);
      maxY = minY + 20;
    }
  } else {
    const pad = (maxY - minY) * 0.12;
    minY = c.allow_negative ? minY - pad : Math.max(0, minY - pad);
    maxY += pad;
  }
  return { minY, maxY, validBounds };
}

function drawDeviceCurveCanvas() {
  const canvas = document.getElementById("dcCurveCanvas");
  const c = DevCfgState.curve;
  if (!canvas || !c || !Array.isArray(c.points)) return;

  const rect = canvas.parentElement.getBoundingClientRect();
  const w = Math.max(240, Math.floor(rect.width));
  const h = Math.max(170, Math.floor(rect.height));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  canvas.style.width = `${w}px`;
  canvas.style.height = `${h}px`;

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const pal = chartThemePalette();
  ctx.clearRect(0, 0, w, h);

  const padL = 46;
  const padR = 14;
  const padT = 16;
  const padB = 26;
  const plotW = Math.max(40, w - padL - padR);
  const plotH = Math.max(40, h - padT - padB);

  const vals = c.points.map((v) => Number(v) || 0);
  const range = DevCfgState.dragLockedRange || _computeCurveYRange(c);
  const minY = range.minY;
  const maxY = range.maxY;
  DevCfgState.curveBounds = { minY, maxY, padL, padR, padT, padB, w, h, plotW, plotH };

  const xOf = (idx) => padL + (idx / 95) * plotW;
  const yOf = (val) => padT + (1 - (val - minY) / (maxY - minY)) * plotH;

  // 水平网格与 Y 轴刻度
  ctx.font = "10.5px var(--font-mono), Consolas, monospace";
  ctx.strokeStyle = pal.grid;
  ctx.lineWidth = 1;
  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const v = minY + ((maxY - minY) * i) / yTicks;
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.fillStyle = pal.text;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(fmtNum(v, Math.abs(v) >= 100 ? 0 : 1), padL - 6, y);
  }

  // 垂直时间刻度（每 4 小时）
  [0, 16, 32, 48, 64, 80, 95].forEach((idx) => {
    const x = xOf(idx);
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, h - padB);
    ctx.stroke();
    ctx.fillStyle = pal.text;
    ctx.textAlign = idx === 95 ? "right" : (idx === 0 ? "left" : "center");
    ctx.textBaseline = "top";
    ctx.fillText(idx === 95 ? "23:45" : `${String((idx * 15) / 60).padStart(2, "0")}:00`, x, h - padB + 6);
  });

  // 上下限与当前实时值参考线（错开标签水平位置，彻底避免重叠）
  const drawLimitLine = (val, color, label, alignMode) => {
    if (val === null || val === undefined || !Number.isFinite(Number(val))) return;
    const numV = Number(val);
    if (numV < minY - (maxY - minY) * 0.05 || numV > maxY + (maxY - minY) * 0.05) return;
    const y = Math.max(padT, Math.min(h - padB, yOf(numV)));
    ctx.save();
    ctx.strokeStyle = color;
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = "10px var(--font-mono), Consolas, monospace";
    const tw = ctx.measureText(label).width + 8;
    let bx = w - padR - tw - 2;
    if (alignMode === "left") bx = padL + 4;
    else if (alignMode === "center") bx = padL + (plotW - tw) / 2;
    const by = y - 14 < padT ? y + 2 : y - 14;
    ctx.fillStyle = pal.bg || "rgba(15, 23, 42, 0.82)";
    ctx.globalAlpha = 0.85;
    ctx.fillRect(bx, by, tw, 13);
    ctx.globalAlpha = 1;
    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, bx + 4, by + 6.5);
    ctx.restore();
  };
  if (range.validBounds) {
    drawLimitLine(c.effective_max, "#D97706", `MAX ${fmtNum(c.effective_max, 1)}`, "right");
    drawLimitLine(c.effective_min, "#D97706", `MIN ${fmtNum(c.effective_min, 1)}`, "left");
  }
  if (c.current_kw !== null && c.current_kw !== undefined) {
    drawLimitLine(c.current_kw, "#0D9488", `NOW ${fmtNum(c.current_kw, 1)}`, "center");
  }

  // 96 点预设曲线折线/阶梯 + 柔和面积填充
  const isStep = (c.curve_interp || "step") === "step";
  const accent = pal.colors[0] || "#2563EB";
  ctx.save();
  ctx.beginPath();
  for (let i = 0; i < 96; i++) {
    const x = xOf(i);
    const y = yOf(vals[i]);
    if (i === 0) {
      ctx.moveTo(x, y);
    } else if (isStep) {
      ctx.lineTo(x, yOf(vals[i - 1]));
      ctx.lineTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.strokeStyle = accent;
  ctx.lineWidth = DevCfgState.curveDragging ? 2.5 : 2;
  ctx.stroke();

  // 当前时刻竖向指示线与圆点
  const nowIdx = Math.max(0, Math.min(95, Number(c.current_slot_index || 0)));
  const nowX = xOf(nowIdx);
  const nowY = yOf(vals[nowIdx]);
  ctx.strokeStyle = "#EF4444";
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(nowX, padT);
  ctx.lineTo(nowX, h - padB);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#EF4444";
  ctx.beginPath();
  ctx.arc(nowX, nowY, 3.8, 0, Math.PI * 2);
  ctx.fill();

  // 悬停/拖拽高亮十字与浮标
  const hIdx = DevCfgState.curveHoverIdx;
  if (hIdx >= 0 && hIdx < 96) {
    const hx = xOf(hIdx);
    const hy = yOf(vals[hIdx]);
    ctx.strokeStyle = DevCfgState.curveDragging ? accent : pal.text;
    ctx.globalAlpha = DevCfgState.curveDragging ? 0.7 : 0.4;
    ctx.beginPath();
    ctx.moveTo(hx, padT);
    ctx.lineTo(hx, h - padB);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = accent;
    ctx.beginPath();
    ctx.arc(hx, hy, DevCfgState.curveDragging ? 5.2 : 4.2, 0, Math.PI * 2);
    ctx.fill();
    if (DevCfgState.curveDragging) {
      ctx.strokeStyle = "#FFFFFF";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    const tip = `${c.labels[hIdx]}  ${fmtNum(vals[hIdx], 1)} ${c.unit || "kW"}`;
    ctx.font = "11px var(--font-mono), Consolas, monospace";
    const tw = ctx.measureText(tip).width + 12;
    const bx = Math.min(w - padR - tw, Math.max(padL, hx - tw / 2));
    const by = hy - 26 < padT ? Math.min(h - padB - 22, hy + 10) : hy - 26;
    ctx.fillStyle = pal.bg || "#0F172A";
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1;
    ctx.fillRect(bx, by, tw, 20);
    ctx.strokeRect(bx, by, tw, 20);
    ctx.fillStyle = pal.text;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(tip, bx + 6, by + 10);
  }
  ctx.restore();
}

function renderDeviceCurveGrid() {
  const wrap = document.getElementById("dcCurveGridWrap");
  const c = DevCfgState.curve;
  if (!wrap) return;
  if (!c || !Array.isArray(c.points)) {
    wrap.innerHTML = `<table class="data-table"><tbody><tr><td class="empty-cell">—</td></tr></tbody></table>`;
    return;
  }
  const nowIdx = Number(c.current_slot_index || 0);
  const rowsHtml = [];
  for (let r = 0; r < 24; r++) {
    const cells = [];
    for (let col = 0; col < 4; col++) {
      const idx = col * 24 + r;
      const isNow = idx === nowIdx;
      cells.push(`
        <td class="mono muted dc-slot-lbl ${isNow ? "is-now-slot" : ""}">${c.labels[idx]}</td>
        <td class="cell-edit num ${isNow ? "is-now-slot" : ""}">
          <input type="number" step="0.1" class="grid-control num" data-curve-idx="${idx}" value="${escapeHtml(String(c.points[idx]))}" />
        </td>`);
    }
    rowsHtml.push(`<tr>${cells.join("")}</tr>`);
  }
  wrap.innerHTML = `
    <table class="data-table curve-grid-table">
      <thead>
        <tr>
          <th>00-05</th><th class="num">kW</th>
          <th>06-11</th><th class="num">kW</th>
          <th>12-17</th><th class="num">kW</th>
          <th>18-23</th><th class="num">kW</th>
        </tr>
      </thead>
      <tbody>${rowsHtml.join("")}</tbody>
    </table>`;

  wrap.querySelectorAll("input[data-curve-idx]").forEach((inp) => {
    inp.addEventListener("input", () => {
      const idx = Number(inp.dataset.curveIdx);
      const val = Number(inp.value);
      const td = inp.closest("td");
      if (!Number.isFinite(val)) {
        if (td) td.classList.add("is-invalid");
        return;
      }
      if (td) td.classList.remove("is-invalid");
      DevCfgState.curve.points[idx] = Math.round(val * 1000) / 1000;
      DevCfgState.curveDirty = true;
      renderDeviceCurveHeader();
      drawDeviceCurveCanvas();
    });
  });
}

function _clampDraggedCurveValue(c, rawVal, b) {
  let val = Number(rawVal);
  const hasMin = c.effective_min !== null && c.effective_min !== undefined && Number.isFinite(Number(c.effective_min));
  const hasMax = c.effective_max !== null && c.effective_max !== undefined && Number.isFinite(Number(c.effective_max));
  if (hasMin && hasMax && Number(c.effective_min) < Number(c.effective_max)) {
    val = Math.min(Number(c.effective_max), Math.max(Number(c.effective_min), val));
  } else {
    val = Math.min(b.maxY, Math.max(b.minY, val));
    if (!c.allow_negative) val = Math.max(0, val);
    if (hasMin && (!hasMax || Number(c.effective_min) < Number(c.effective_max))) {
      val = Math.max(Number(c.effective_min), val);
    }
    if (hasMax && (!hasMin || Number(c.effective_max) > Number(c.effective_min))) {
      val = Math.min(Number(c.effective_max), val);
    }
  }
  return Math.round(val * 10) / 10;
}

function _applyCurveCanvasPointer(ev, isStart = false) {
  const c = DevCfgState.curve;
  const b = DevCfgState.curveBounds;
  const canvas = document.getElementById("dcCurveCanvas");
  if (!c || !b || !canvas) return;
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const y = ev.clientY - rect.top;
  const idx = Math.max(0, Math.min(95, Math.round(((x - b.padL) / b.plotW) * 95)));
  DevCfgState.curveHoverIdx = idx;

  if (DevCfgState.curveDragging) {
    const rawVal = b.maxY - ((y - b.padT) / b.plotH) * (b.maxY - b.minY);
    const val = _clampDraggedCurveValue(c, rawVal, b);

    // 连续拖拽线性插值填充：快速拖动跨越多个 15min 槽位时平滑连接不跳点
    const prevIdx = isStart ? idx : DevCfgState.lastDragIdx;
    const prevVal = isStart || DevCfgState.lastDragVal === null ? val : DevCfgState.lastDragVal;
    const startIdx = Math.min(prevIdx, idx);
    const endIdx = Math.max(prevIdx, idx);
    const span = idx - prevIdx;
    for (let i = startIdx; i <= endIdx; i++) {
      const interpVal = span === 0
        ? val
        : Math.round((prevVal + ((val - prevVal) * (i - prevIdx)) / span) * 10) / 10;
      c.points[i] = interpVal;
      const inp = document.querySelector(`#dcCurveGridWrap input[data-curve-idx="${i}"]`);
      if (inp) inp.value = String(interpVal);
    }
    DevCfgState.lastDragIdx = idx;
    DevCfgState.lastDragVal = val;
    DevCfgState.curveDirty = true;
    renderDeviceCurveHeader();
  }
  drawDeviceCurveCanvas();
}

function applyCurveTemplate(tpl) {
  const c = DevCfgState.curve;
  if (!c || !tpl) return;
  const lo = c.effective_min !== null && c.effective_min !== undefined ? Number(c.effective_min) : 0;
  const hi = c.effective_max !== null && c.effective_max !== undefined ? Number(c.effective_max) : 100;
  const span = Math.max(0, hi - lo);
  const cur = c.current_kw !== null && c.current_kw !== undefined ? Number(c.current_kw) : Math.max(lo, 0);
  const clampV = (v) => Math.round(Math.min(hi, Math.max(lo, v)) * 10) / 10;

  const pts = new Array(96).fill(0);
  for (let i = 0; i < 96; i++) {
    const hour = (i * 15) / 60;
    if (tpl === "current") {
      pts[i] = clampV(cur);
    } else if (tpl === "zero") {
      pts[i] = clampV(0);
    } else if (tpl === "half") {
      pts[i] = clampV(lo >= 0 ? lo + span * 0.5 : hi * 0.5);
    } else if (tpl === "pv_bell") {
      const s = hour >= 6 && hour <= 18 ? Math.sin(((hour - 6) / 12) * Math.PI) : 0;
      pts[i] = clampV(Math.max(lo, 0) + Math.max(0, hi - Math.max(lo, 0)) * Math.pow(s, 1.3));
    } else if (tpl === "double_peak") {
      const p1 = Math.exp(-Math.pow((hour - 10.5) / 2.6, 2));
      const p2 = Math.exp(-Math.pow((hour - 19.5) / 2.8, 2));
      const ratio = 0.25 + 0.7 * Math.max(p1, p2);
      pts[i] = clampV(lo + span * ratio);
    }
  }
  c.points = pts;
  DevCfgState.curveDirty = true;
  renderDeviceCurveHeader();
  drawDeviceCurveCanvas();
  renderDeviceCurveGrid();
}

async function saveDirtyDeviceConfigs() {
  _recomputeValidation();
  if (DevCfgState.invalidCells.size > 0) {
    showToast(t("devcfg.errInvalid"), "danger");
    return;
  }
  if (DevCfgState.dirtyKeys.size === 0) return;

  const payloadItems = [];
  DevCfgState.dirtyKeys.forEach((key) => {
    const item = DevCfgState.items.find((it) => devCfgKey(it) === key);
    if (!item) return;
    const draft = _getRowDraft(item);
    payloadItems.push({
      dev_type: item.dev_type,
      dev_name: item.dev_name,
      set_type: item.set_type,
      control_state: draft.control_state,
      p_min: draft.p_min === "" ? null : Number(draft.p_min),
      p_max: draft.p_max === "" ? null : Number(draft.p_max),
      control_step: draft.control_step === "" ? null : Number(draft.control_step),
      control_deadband: draft.control_deadband === "" ? 0 : Number(draft.control_deadband),
      curve_interp: draft.curve_interp || "step"
    });
  });

  try {
    const res = await apiPost("/api/device-configs", { items: payloadItems });
    DevCfgState.items = res.items || DevCfgState.items;
    DevCfgState.counts = res.counts || DevCfgState.counts;
    DevCfgState.drafts = {};
    DevCfgState.dirtyKeys.clear();
    renderDeviceConfigsToolbar();
    renderDeviceConfigsTable();
    showToast(`${t("devcfg.savedMsg")} (${res.saved_count || payloadItems.length})`, "ok");
  } catch (err) {
    showToast(err.message, "danger");
  }
}

async function saveCurrentDeviceCurve() {
  const c = DevCfgState.curve;
  if (!c) return;
  const interp = document.getElementById("dcCurveInterpSelect")?.value || "step";
  try {
    const res = await apiPost("/api/device-configs/curve", {
      dev_type: c.dev_type,
      dev_name: c.dev_name,
      set_type: c.set_type,
      points: c.points,
      curve_interp: interp
    });
    DevCfgState.curve = res;
    DevCfgState.curveDirty = false;
    // 同步刷新左侧列表中的曲线现值与标记
    const key = devCfgKey(c);
    const listItem = DevCfgState.items.find((it) => devCfgKey(it) === key);
    if (listItem) {
      listItem.has_curve = true;
      listItem.curve_interp = interp;
      if (DevCfgState.drafts[key]) DevCfgState.drafts[key].curve_interp = interp;
    }
    renderDeviceCurveHeader();
    drawDeviceCurveCanvas();
    await refreshDeviceConfigsLiveOnly();
    renderDeviceConfigsTable();
    showToast(`${c.dev_name} ${t("devcfg.curveSaved")}`, "ok");
  } catch (err) {
    showToast(err.message, "danger");
  }
}

function exportCurrentCurveCsv() {
  const c = DevCfgState.curve;
  if (!c) return;
  const lines = ["index,time,target_kw"];
  for (let i = 0; i < 96; i++) {
    lines.push(`${i},${c.labels[i]},${c.points[i]}`);
  }
  const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `curve_${c.dev_name}_${c.set_type}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function importCurveCsvText(text) {
  const c = DevCfgState.curve;
  if (!c) return;
  const rawLines = String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const nums = [];
  rawLines.forEach((line) => {
    const parts = line.split(/[,\t;\s]+/).filter(Boolean);
    const candidate = parts.length >= 3 ? Number(parts[2]) : Number(parts[parts.length - 1]);
    if (Number.isFinite(candidate)) nums.push(candidate);
  });
  if (nums.length !== 96) {
    showToast(`CSV 需包含 96 个有效数值（当前解析 ${nums.length} 个）`, "danger");
    return;
  }
  c.points = nums.map((v) => Math.round(v * 1000) / 1000);
  DevCfgState.curveDirty = true;
  renderDeviceCurveHeader();
  drawDeviceCurveCanvas();
  renderDeviceCurveGrid();
  showToast(t("devcfg.csvImported"), "ok");
}

function bindDeviceConfigEvents() {
  // 分类筛选
  document.querySelectorAll("#dcGroupTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      DevCfgState.groupFilter = btn.dataset.dcGroup || "all";
      document.querySelectorAll("#dcGroupTabs button").forEach((b) => b.classList.toggle("is-active", b === btn));
      renderDeviceConfigsTable();
    });
  });

  document.getElementById("dcStateFilter")?.addEventListener("change", (ev) => {
    DevCfgState.stateFilter = ev.target.value || "all";
    renderDeviceConfigsTable();
  });

  document.getElementById("dcSearchInput")?.addEventListener("input", (ev) => {
    DevCfgState.keyword = ev.target.value || "";
    renderDeviceConfigsTable();
  });

  // 批量修改当前筛选行的控制状态（写入草稿，点击保存落盘）
  const batchSel = document.getElementById("dcBatchStateSelect");
  batchSel?.addEventListener("change", () => {
    const targetState = batchSel.value;
    batchSel.value = "";
    if (!targetState) return;
    const rows = getFilteredDeviceConfigItems();
    rows.forEach((item) => {
      const key = devCfgKey(item);
      const draft = { ..._getRowDraft(item), control_state: targetState };
      DevCfgState.drafts[key] = draft;
      DevCfgState.dirtyKeys.add(key);
    });
    renderDeviceConfigsToolbar();
    renderDeviceConfigsTable();
  });

  document.getElementById("devCfgSaveBtn")?.addEventListener("click", saveDirtyDeviceConfigs);

  // 高危恢复默认确认
  document.getElementById("devCfgResetBtn")?.addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: t("devcfg.resetTitle"),
      target: `${DevCfgState.items.length} ${t("devcfg.devicesUnit")}`,
      message: t("devcfg.resetMsg"),
      confirmText: t("action.restoreDefault")
    });
    if (!ok) return;
    try {
      const res = await apiPost("/api/device-configs/reset", { reset_curves: false });
      DevCfgState.items = res.items || [];
      DevCfgState.counts = res.counts || {};
      DevCfgState.drafts = {};
      DevCfgState.dirtyKeys.clear();
      renderDeviceConfigsToolbar();
      renderDeviceConfigsTable();
      showToast(t("devcfg.resetDone"), "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  // 曲线工具栏事件
  document.getElementById("dcCurveInterpSelect")?.addEventListener("change", (ev) => {
    if (!DevCfgState.curve) return;
    DevCfgState.curve.curve_interp = ev.target.value;
    DevCfgState.curveDirty = true;
    renderDeviceCurveHeader();
    drawDeviceCurveCanvas();
  });

  const tplSel = document.getElementById("dcCurveTemplateSelect");
  tplSel?.addEventListener("change", () => {
    const v = tplSel.value;
    tplSel.value = "";
    if (v) applyCurveTemplate(v);
  });

  document.getElementById("dcSaveCurveBtn")?.addEventListener("click", saveCurrentDeviceCurve);
  document.getElementById("dcExportCurveBtn")?.addEventListener("click", exportCurrentCurveCsv);

  const fileInput = document.getElementById("dcImportCurveFile");
  document.getElementById("dcImportCurveBtn")?.addEventListener("click", () => fileInput?.click());
  fileInput?.addEventListener("change", async () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = "";
    if (!f) return;
    const text = await f.text();
    importCurveCsvText(text);
  });

  // 画布连续拖拽绘制（Pointer Events + 指针捕获 + 拖拽期间锁定 Y 轴视窗）
  const canvas = document.getElementById("dcCurveCanvas");
  if (canvas) {
    const finishDrag = (ev) => {
      if (!DevCfgState.curveDragging) return;
      DevCfgState.curveDragging = false;
      DevCfgState.lastDragIdx = -1;
      DevCfgState.lastDragVal = null;
      DevCfgState.dragLockedRange = null;
      try {
        if (ev && ev.pointerId !== undefined && canvas.hasPointerCapture(ev.pointerId)) {
          canvas.releasePointerCapture(ev.pointerId);
        }
      } catch (_e) {}
      drawDeviceCurveCanvas();
    };
    canvas.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0 || !DevCfgState.curve) return;
      ev.preventDefault();
      DevCfgState.dragLockedRange = _computeCurveYRange(DevCfgState.curve);
      DevCfgState.curveDragging = true;
      DevCfgState.lastDragIdx = -1;
      DevCfgState.lastDragVal = null;
      try { canvas.setPointerCapture(ev.pointerId); } catch (_e) {}
      drawDeviceCurveCanvas();
      _applyCurveCanvasPointer(ev, true);
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (DevCfgState.curveDragging) ev.preventDefault();
      _applyCurveCanvasPointer(ev, false);
    });
    canvas.addEventListener("pointerup", finishDrag);
    canvas.addEventListener("pointercancel", finishDrag);
    canvas.addEventListener("pointerleave", () => {
      if (!DevCfgState.curveDragging) {
        DevCfgState.curveHoverIdx = -1;
        drawDeviceCurveCanvas();
      }
    });
  }

  // AGC 控制页直达按钮
  document.getElementById("openDeviceConfigFromAgcBtn")?.addEventListener("click", () => {
    switchPage("device-config");
  });
}
