// 全局状态、HTTP 工具、高危确认、分级提示与全主题自适应 Canvas 2D 图表引擎
const AGCState = {
  page: "overview",
  snapshot: null,
  modelData: null,
  control: null,
  commands: null,
  trendData: null,
  runtimeSettings: null,
  metricTab: "ac",
  strategyTab: "all",
  detailTab: "trend",
  trendWindow: 60,
  trendEndMs: null,
  selectedSeries: new Set([
    "load_kw",
    "renewable_current_kw",
    "renewable_target_kw",
    "storage_current_kw",
    "diesel_current_kw",
    "storage_soc"
  ]),
  controlLogPage: 1,
  runtimeLogPage: 1,
  modelFilterBlock: "ALL",
  measFilterBlock: "ALL",
  selectedMeas: null,
  cmdFilterBlock: "ALL",
  cmdKind: "yt",
  selectedCmd: null,
  curveField: "load_kw",
  curveWindow: 60,
  curveEndMs: null,
  diagramLoaded: false,
  diagramScale: 1,
  diagramTx: 20,
  diagramTy: 20,
  diagramSelectedDev: null
};

async function apiGet(url) {
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

async function apiPost(url, body = {}) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body)
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || data.message || `HTTP ${resp.status}`);
  return data;
}

function fmtNum(v, digits = 2, fallback = "--") {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return fallback;
  return Number(v).toFixed(digits);
}

function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
  const num = Number(v);
  const pct = Math.abs(num) <= 1.5 ? num * 100 : num;
  return `${pct.toFixed(digits)}%`;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function showToast(msg, level = "info") {
  const container = document.getElementById("toastContainer");
  if (!container) return;
  const item = document.createElement("div");
  item.className = `toast-item ${level}`;
  item.textContent = msg;
  container.appendChild(item);
  setTimeout(() => item.remove(), 4500);
}

function confirmDanger({ title, target, message, confirmText }) {
  return new Promise((resolve) => {
    const dlg = document.getElementById("dangerConfirmDialog");
    const titleEl = document.getElementById("dangerTitle");
    const targetEl = document.getElementById("dangerTarget");
    const msgEl = document.getElementById("dangerMessage");
    const cancelBtn = document.getElementById("dangerCancelBtn");
    const okBtn = document.getElementById("dangerOkBtn");

    titleEl.textContent = title || t("danger.badge");
    targetEl.textContent = target || "";
    targetEl.hidden = !target;
    msgEl.textContent = message || "";
    okBtn.textContent = confirmText || t("action.confirm");

    const cleanup = (res) => {
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      dlg.oncancel = null;
      if (dlg.open) dlg.close();
      resolve(res);
    };
    okBtn.onclick = () => cleanup(true);
    cancelBtn.onclick = () => cleanup(false);
    dlg.oncancel = (ev) => {
      ev.preventDefault();
      cleanup(false);
    };
    dlg.showModal();
    cancelBtn.focus();
  });
}

function downloadCsv(filename, headers, rows) {
  const esc = (v) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
  };
  const lines = [headers.map(esc).join(","), ...rows.map((r) => r.map(esc).join(","))];
  const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

// =========================================================================
// 全主题自适应 Canvas 2D 多曲线/双轴折线图绘制引擎（严禁纯白描边糊字）
// =========================================================================
function chartThemePalette() {
  const cs = getComputedStyle(document.documentElement);
  const dark = document.documentElement.dataset.colorScheme === "dark";
  return {
    dark,
    bg: cs.getPropertyValue("--bg-surface").trim() || (dark ? "#111827" : "#FFFFFF"),
    grid: dark ? "rgba(255,255,255,0.08)" : "rgba(15,23,42,0.08)",
    axis: dark ? "rgba(255,255,255,0.20)" : "rgba(15,23,42,0.18)",
    text: cs.getPropertyValue("--text-secondary").trim() || (dark ? "#94A3B8" : "#64748B"),
    textPrimary: cs.getPropertyValue("--text-primary").trim() || (dark ? "#F8FAFC" : "#0F172A"),
    labelBg: dark ? "rgba(15, 23, 42, 0.88)" : "rgba(255, 255, 255, 0.92)",
    labelBorder: cs.getPropertyValue("--border-color").trim() || (dark ? "rgba(255,255,255,0.15)" : "#CBD5E1"),
    crosshair: cs.getPropertyValue("--accent").trim() || (dark ? "#38BDF8" : "#2563EB"),
    colors: dark
      ? ["#38BDF8", "#FBBF24", "#34D399", "#FB7185", "#A78BFA", "#22D3EE", "#FB923C", "#94A3B8"]
      : ["#2563EB", "#D97706", "#0D9488", "#DC2626", "#7C3AED", "#0284C7", "#EA580C", "#64748B"]
  };
}

function drawRoundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function drawLineChart(canvas, { labels = [], series = [], leftUnit = "kW", rightUnit = "%", emptyText = "暂无数据" }) {
  if (!canvas) return;
  const host = canvas.parentElement || canvas;
  const rect = host.getBoundingClientRect();
  const width = Math.max(260, Math.floor(rect.width || 600));
  const height = Math.max(120, Math.floor(rect.height || 220));
  const dpr = Math.min(window.devicePixelRatio || 1, 2);

  if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const pal = chartThemePalette();
  ctx.clearRect(0, 0, width, height);

  const hasRight = series.some((s) => s.axis === "right");
  const padL = 56;
  const padR = hasRight ? 52 : 18;
  const padT = 16;
  const padB = 26;
  const plotW = Math.max(40, width - padL - padR);
  const plotH = Math.max(40, height - padT - padB);

  if (!labels.length || !series.length) {
    ctx.fillStyle = pal.text;
    ctx.font = "12px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(emptyText, width / 2, height / 2);
    return;
  }

  function calcBounds(list) {
    let min = Infinity;
    let max = -Infinity;
    list.forEach((s) => {
      (s.values || []).forEach((v) => {
        if (v !== null && v !== undefined && Number.isFinite(Number(v))) {
          const n = Number(v);
          if (n < min) min = n;
          if (n > max) max = n;
        }
      });
    });
    if (!Number.isFinite(min)) return { min: 0, max: 100 };
    if (Math.abs(max - min) < 1e-6) return { min: min - 1, max: max + 1 };
    const margin = (max - min) * 0.1;
    return { min: min - margin, max: max + margin };
  }

  const leftSeries = series.filter((s) => s.axis !== "right");
  const rightSeries = series.filter((s) => s.axis === "right");
  const lBounds = leftSeries.length ? calcBounds(leftSeries) : { min: 0, max: 100 };
  const rBounds = rightSeries.length ? { min: 0, max: 100 } : null;

  // 水平虚线网格 + 坐标轴刻度
  ctx.font = "11px JetBrains Mono, Consolas, monospace";
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const ratio = i / ticks;
    const y = padT + plotH * (1 - ratio);
    ctx.save();
    ctx.strokeStyle = pal.grid;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.stroke();
    ctx.restore();

    const lv = lBounds.min + (lBounds.max - lBounds.min) * ratio;
    ctx.fillStyle = pal.text;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(lv.toFixed(Math.abs(lv) >= 100 ? 0 : 1), padL - 6, y);

    if (hasRight && rBounds) {
      const rv = rBounds.min + (rBounds.max - rBounds.min) * ratio;
      ctx.textAlign = "left";
      ctx.fillText(`${rv.toFixed(0)}%`, padL + plotW + 6, y);
    }
  }

  // 坐标轴线与单位
  ctx.strokeStyle = pal.axis;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, padT);
  ctx.lineTo(padL, padT + plotH);
  ctx.lineTo(padL + plotW, padT + plotH);
  ctx.stroke();

  ctx.fillStyle = pal.text;
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  ctx.fillText(leftUnit, 4, 2);
  if (hasRight) {
    ctx.textAlign = "right";
    ctx.fillText(rightUnit, width - 4, 2);
  }

  // X 轴时间刻度
  const xStep = Math.max(1, Math.floor(labels.length / 6));
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = 0; i < labels.length; i += xStep) {
    const x = labels.length === 1 ? padL + plotW / 2 : padL + (i / (labels.length - 1)) * plotW;
    ctx.fillText(labels[i], x, padT + plotH + 6);
  }

  // 绘制曲线
  const mapX = (idx) => (labels.length === 1 ? padL + plotW / 2 : padL + (idx / (labels.length - 1)) * plotW);
  const mapY = (val, axis) => {
    const b = axis === "right" && rBounds ? rBounds : lBounds;
    const norm = (val - b.min) / (b.max - b.min || 1);
    return padT + plotH * (1 - norm);
  };

  series.forEach((s, sIdx) => {
    const color = s.color || pal.colors[sIdx % pal.colors.length];
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = s.dashed ? 1.5 : 2;
    if (s.dashed) ctx.setLineDash([5, 4]);
    ctx.beginPath();
    let started = false;
    (s.values || []).forEach((v, i) => {
      if (v === null || v === undefined || !Number.isFinite(Number(v))) {
        started = false;
        return;
      }
      const x = mapX(i);
      const y = mapY(Number(v), s.axis);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
    // 点数较少时显式绘制圆点，避免仅 1 个周期点时折线不可见
    if (labels.length <= 2) {
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      (s.values || []).forEach((v, i) => {
        if (v === null || v === undefined || !Number.isFinite(Number(v))) return;
        const x = mapX(i);
        const y = mapY(Number(v), s.axis);
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
      });
    }
    ctx.restore();
  });

  // 鼠标悬停十字准星与半透明圆角胶囊 Tooltip
  canvas._chartRenderMeta = { labels, series, padL, padT, plotW, plotH, mapX, mapY, pal, leftUnit, rightUnit };
  if (!canvas._resizeObserved && typeof ResizeObserver !== "undefined") {
    canvas._resizeObserved = true;
    const ro = new ResizeObserver(() => {
      const m = canvas._chartRenderMeta;
      if (m && host.clientWidth > 0 && host.clientHeight > 0) {
        drawLineChart(canvas, { labels: m.labels, series: m.series, leftUnit: m.leftUnit, rightUnit: m.rightUnit });
      }
    });
    ro.observe(host);
  }
  if (!canvas._hoverBound) {
    canvas._hoverBound = true;
    canvas.addEventListener("mousemove", (ev) => {
      const meta = canvas._chartRenderMeta;
      if (!meta || !meta.labels.length) return;
      const r = canvas.getBoundingClientRect();
      const mx = ev.clientX - r.left;
      if (mx < meta.padL || mx > meta.padL + meta.plotW) return;
      const ratio = (mx - meta.padL) / (meta.plotW || 1);
      const idx = Math.max(0, Math.min(meta.labels.length - 1, Math.round(ratio * (meta.labels.length - 1))));
      drawLineChart(canvas, { labels: meta.labels, series: meta.series, leftUnit: meta.leftUnit, rightUnit: meta.rightUnit });
      drawChartHover(canvas, idx);
    });
    canvas.addEventListener("mouseleave", () => {
      const meta = canvas._chartRenderMeta;
      if (meta) {
        drawLineChart(canvas, { labels: meta.labels, series: meta.series, leftUnit: meta.leftUnit, rightUnit: meta.rightUnit });
      }
    });
  }
}

function drawChartHover(canvas, idx) {
  const meta = canvas._chartRenderMeta;
  if (!meta) return;
  const ctx = canvas.getContext("2d");
  const { labels, series, padL, padT, plotW, plotH, mapX, mapY, pal } = meta;
  const x = mapX(idx);

  ctx.save();
  ctx.strokeStyle = pal.crosshair;
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(x, padT);
  ctx.lineTo(x, padT + plotH);
  ctx.stroke();
  ctx.restore();

  const lines = [labels[idx]];
  series.forEach((s, i) => {
    const val = s.values[idx];
    if (val !== null && val !== undefined && Number.isFinite(Number(val))) {
      const color = s.color || pal.colors[i % pal.colors.length];
      const y = mapY(Number(val), s.axis);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
      lines.push({ label: s.label, val: `${Number(val).toFixed(2)} ${s.unit || ""}`.trim(), color });
    }
  });

  ctx.font = "11px JetBrains Mono, Consolas, monospace";
  const boxW = Math.min(240, Math.max(130, ...lines.map((l) => ctx.measureText(typeof l === "string" ? l : `${l.label}: ${l.val}`).width + 24)));
  const boxH = 14 + lines.length * 16;
  const bx = x + 12 + boxW > padL + plotW ? Math.max(padL + 4, x - boxW - 12) : x + 12;
  const by = padT + 8;

  ctx.save();
  drawRoundedRect(ctx, bx, by, boxW, boxH, 7);
  ctx.fillStyle = pal.labelBg;
  ctx.fill();
  ctx.strokeStyle = pal.labelBorder;
  ctx.lineWidth = 1;
  ctx.stroke();

  ctx.textBaseline = "top";
  lines.forEach((line, i) => {
    const ly = by + 7 + i * 16;
    if (typeof line === "string") {
      ctx.fillStyle = pal.textPrimary;
      ctx.textAlign = "left";
      ctx.fillText(line, bx + 8, ly);
    } else {
      ctx.fillStyle = line.color;
      ctx.fillRect(bx + 8, ly + 3, 6, 6);
      ctx.fillStyle = pal.text;
      ctx.textAlign = "left";
      ctx.fillText(line.label, bx + 18, ly);
      ctx.fillStyle = pal.textPrimary;
      ctx.textAlign = "right";
      ctx.fillText(line.val, bx + boxW - 8, ly);
    }
  });
  ctx.restore();
}

// =========================================================================
// 通用可拖拽折叠分栏引擎（支持拖拽缩放、一键折叠/恢复、双击复原与本地持久化）
// =========================================================================
function initAllSplitters() {
  const splitters = document.querySelectorAll(".splitter[data-split-id]");
  splitters.forEach((bar) => {
    if (bar._splitReady) return;
    bar._splitReady = true;

    const id = bar.dataset.splitId;
    const axis = bar.dataset.axis === "y" ? "y" : "x";
    const targetSide = bar.dataset.target === "next" ? "next" : "prev";
    const collapseSide = bar.dataset.collapse === "next" ? "next" : "prev";
    const defaultRaw = String(bar.dataset.default || "260").trim();
    const minPx = Math.max(80, Number(bar.dataset.min || 140));
    const storageKey = `agcSplit:${id}`;

    const prevPane = bar.previousElementSibling;
    const nextPane = bar.nextElementSibling;
    const container = bar.parentElement;
    if (!prevPane || !nextPane || !container) return;

    const controlledPane = targetSide === "next" ? nextPane : prevPane;
    const flexPane = targetSide === "next" ? prevPane : nextPane;
    const collapsiblePane = collapseSide === "next" ? nextPane : prevPane;

    // 创建居中一键折叠/展开按钮
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "splitter-toggle";
    btn.setAttribute("aria-label", "折叠或展开分区");
    bar.appendChild(btn);

    let savedState = { size: defaultRaw, collapsed: false };
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") {
          savedState = {
            size: parsed.size || defaultRaw,
            collapsed: Boolean(parsed.collapsed)
          };
        }
      }
    } catch (_e) {}

    function persist() {
      try {
        localStorage.setItem(storageKey, JSON.stringify(savedState));
      } catch (_e) {}
    }

    function updateToggleIcon() {
      const c = savedState.collapsed;
      if (axis === "x") {
        btn.textContent = collapseSide === "prev" ? (c ? "›" : "‹") : (c ? "‹" : "›");
      } else {
        btn.textContent = collapseSide === "prev" ? (c ? "⌄" : "⌃") : (c ? "⌃" : "⌄");
      }
      btn.title = c ? "展开分区" : "折叠分区";
      bar.classList.toggle("is-collapsed-host", c);
    }

    function applySize(sizeSpec) {
      const s = String(sizeSpec || defaultRaw).trim();
      if (s.endsWith("%")) {
        controlledPane.style.flex = `0 0 ${s}`;
      } else {
        const px = Math.max(minPx, parseFloat(s) || 240);
        controlledPane.style.flex = `0 0 ${Math.round(px)}px`;
      }
      flexPane.style.flex = "1 1 0";
    }

    function applyState() {
      if (savedState.collapsed) {
        collapsiblePane.classList.add("is-collapsed");
        const other = collapsiblePane === prevPane ? nextPane : prevPane;
        other.style.flex = "1 1 0";
      } else {
        collapsiblePane.classList.remove("is-collapsed");
        applySize(savedState.size);
      }
      updateToggleIcon();
      if (typeof redrawActiveCharts === "function") {
        requestAnimationFrame(() => redrawActiveCharts());
      }
    }

    // 按钮点击：一键折叠 / 恢复原尺寸
    btn.addEventListener("mousedown", (ev) => ev.stopPropagation());
    btn.addEventListener("dblclick", (ev) => ev.stopPropagation());
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      savedState.collapsed = !savedState.collapsed;
      persist();
      applyState();
    });

    // 双击分割条：恢复默认尺寸并展开
    bar.addEventListener("dblclick", () => {
      savedState = { size: defaultRaw, collapsed: false };
      persist();
      applyState();
    });

    // 鼠标拖拽实时缩放
    bar.addEventListener("mousedown", (ev) => {
      if (ev.target.closest(".splitter-toggle")) return;
      ev.preventDefault();
      if (savedState.collapsed) {
        savedState.collapsed = false;
        collapsiblePane.classList.remove("is-collapsed");
      }
      const rect = container.getBoundingClientRect();
      const total = axis === "x" ? rect.width : rect.height;
      const barSize = axis === "x" ? bar.offsetWidth : bar.offsetHeight;
      const maxPx = Math.max(minPx + 20, total - barSize - minPx);

      bar.classList.add("is-dragging");
      document.body.classList.add(axis === "x" ? "is-resizing-x" : "is-resizing-y");

      const onMove = (moveEv) => {
        const curPos = axis === "x" ? moveEv.clientX - rect.left : moveEv.clientY - rect.top;
        let nextPx = targetSide === "prev" ? curPos - barSize / 2 : total - curPos - barSize / 2;
        nextPx = Math.max(minPx, Math.min(maxPx, nextPx));
        savedState.size = `${Math.round(nextPx)}px`;
        applySize(savedState.size);
      };

      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        bar.classList.remove("is-dragging");
        document.body.classList.remove("is-resizing-x", "is-resizing-y");
        updateToggleIcon();
        persist();
        if (typeof redrawActiveCharts === "function") {
          requestAnimationFrame(() => redrawActiveCharts());
        }
      };

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });

    applyState();
  });
}
