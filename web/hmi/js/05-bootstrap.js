// 启动引导、导航切换、14 套主题可视化切换与多语言实时调度

const THEME_CATALOG = [
  {
    groupKey: "theme.group.light",
    items: [
      { id: "minimalist-light", dark: false, canvas: "#F4F6F8", surface: "#FFFFFF", accent: "#2563EB", ok: "#0D9488" },
      { id: "bento-grid", dark: false, canvas: "#F5F5F7", surface: "#FFFFFF", accent: "#0071E3", ok: "#0D9488" },
      { id: "editorial", dark: false, canvas: "#FAF9F6", surface: "#FFFFFF", accent: "#9F1239", ok: "#15803D" },
      { id: "nordic-frost", dark: false, canvas: "#ECF2F7", surface: "#F9FBFD", accent: "#0284C7", ok: "#0F766E" },
      { id: "warm-paper", dark: false, canvas: "#F4EFE6", surface: "#FCF9F2", accent: "#2E6F40", ok: "#2E6F40" },
      { id: "blueprint-light", dark: false, canvas: "#EAF2FB", surface: "#F7FBFF", accent: "#1D4ED8", ok: "#0F766E" }
    ]
  },
  {
    groupKey: "theme.group.dark",
    items: [
      { id: "precision-dark", dark: true, canvas: "#0B0F19", surface: "#111827", accent: "#38BDF8", ok: "#34D399" },
      { id: "glassmorphism", dark: true, canvas: "#101A2E", surface: "#223452", accent: "#60A5FA", ok: "#34D399" },
      { id: "obsidian-amber", dark: true, canvas: "#11100E", surface: "#1A1815", accent: "#F59E0B", ok: "#34D399" },
      { id: "nord-slate", dark: true, canvas: "#1D222D", surface: "#252C3A", accent: "#88C0D0", ok: "#A3BE8C" }
    ]
  },
  {
    groupKey: "theme.group.scifi",
    items: [
      { id: "sci-fi-cyan", dark: true, canvas: "#040812", surface: "#081121", accent: "#00E5FF", ok: "#00E6A8" },
      { id: "sci-fi-purple", dark: true, canvas: "#090614", surface: "#120C24", accent: "#C084FC", ok: "#2DD4BF" },
      { id: "sci-fi-matrix", dark: true, canvas: "#040D08", surface: "#081810", accent: "#00E676", ok: "#00E676" },
      { id: "sci-fi-crimson", dark: true, canvas: "#10070B", surface: "#1A0C12", accent: "#FF4757", ok: "#2ED573" }
    ]
  }
];

const THEME_BY_ID = new Map(THEME_CATALOG.flatMap((g) => g.items).map((it) => [it.id, it]));
const DARK_THEMES = new Set(Array.from(THEME_BY_ID.values()).filter((it) => it.dark).map((it) => it.id));

function syncThemePickerTrigger(themeId) {
  const item = THEME_BY_ID.get(themeId) || THEME_BY_ID.get("minimalist-light");
  if (!item) return;
  const lbl = document.getElementById("themePickerLabel");
  if (lbl) {
    lbl.dataset.i18n = `theme.${item.id}`;
    lbl.textContent = t(`theme.${item.id}`);
  }
  const dots = document.querySelectorAll("#themePickerDots .theme-dot");
  if (dots.length >= 3) {
    dots[0].style.background = item.canvas;
    dots[1].style.background = item.surface;
    dots[2].style.background = item.accent;
  }
  const hiddenSel = document.getElementById("themeSelector");
  if (hiddenSel && hiddenSel.value !== item.id) hiddenSel.value = item.id;
}

function renderThemePopover() {
  const pop = document.getElementById("themePopover");
  if (!pop) return;
  const active = document.documentElement.dataset.theme || "minimalist-light";
  pop.innerHTML = THEME_CATALOG.map((grp) => `
    <div class="theme-group">
      <h3 class="theme-group-title">${escapeHtml(t(grp.groupKey))}</h3>
      <div class="theme-swatch-grid">
        ${grp.items.map((it) => `
          <button type="button" class="theme-swatch-card ${it.id === active ? "is-active" : ""}" data-pick-theme="${it.id}">
            <span class="theme-mini-preview" style="background:${it.canvas};">
              <span class="theme-mini-bar" style="background:${it.surface};"></span>
              <span class="theme-mini-row">
                <span class="theme-mini-pill" style="background:${it.surface};"></span>
                <span class="theme-mini-accent" style="background:${it.accent};"></span>
              </span>
            </span>
            <span class="theme-swatch-label">${escapeHtml(t(`theme.${it.id}`))}</span>
            <span class="theme-swatch-check" aria-hidden="true">✓</span>
          </button>
        `).join("")}
      </div>
    </div>
  `).join("");
  pop.querySelectorAll("[data-pick-theme]").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyThemeChoice(btn.dataset.pickTheme, true);
      closeThemePopover();
    });
  });
}

function closeThemePopover() {
  const pop = document.getElementById("themePopover");
  const btn = document.getElementById("themePickerBtn");
  if (pop) pop.hidden = true;
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function applyThemeChoice(themeId, persist = true) {
  const theme = THEME_BY_ID.has(themeId) ? themeId : "minimalist-light";
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.colorScheme = DARK_THEMES.has(theme) ? "dark" : "light";
  if (persist) {
    try { localStorage.setItem("agcUiTheme", theme); } catch (_e) {}
  }
  syncThemePickerTrigger(theme);
  renderThemePopover();
  redrawActiveCharts();
}

function updateTopbar() {
  const snap = AGCState.snapshot;
  const dot = document.getElementById("connectionDot");
  const connText = document.getElementById("connectionText");
  if (!snap) {
    if (dot) dot.className = "state-dot danger";
    if (connText) connText.textContent = t("conn.offline");
    return;
  }
  const agc = snap.agc_status || {};
  const hasErr = Boolean(agc.scada_error);
  if (dot) dot.className = `state-dot ${hasErr ? "danger" : "ok"}`;
  if (connText) connText.textContent = hasErr ? t("conn.offline") : t("conn.online");
  document.getElementById("topbarModelName").textContent = (snap.model && snap.model.name) || "qinling";

  const modeEl = document.getElementById("topbarLoopMode");
  modeEl.textContent = agc.loop_mode === "closed" ? t("mode.closed") : t("mode.open");
  modeEl.className = `badge ${agc.loop_mode === "closed" ? "badge-warn" : "badge-info"}`;

  const autoEl = document.getElementById("topbarAutoState");
  autoEl.textContent = agc.auto_enabled ? t("state.running") : t("state.stopped");
  autoEl.className = `badge ${agc.auto_enabled ? "badge-ok" : "badge-neutral"}`;

  document.getElementById("topbarCycle").textContent = `#${agc.cycle || 0}`;
  document.getElementById("topbarClock").textContent = (snap.clock && snap.clock.time) || "--:--:--";

  const alertBtn = document.getElementById("topbarAlertBadge");
  const alertText = document.getElementById("topbarAlertBadgeText");
  const chanAlarm = agc.channel_alarm || "";
  if (hasErr) {
    alertBtn.hidden = false;
    alertText.textContent = "SCADA 采集异常";
  } else if (chanAlarm) {
    alertBtn.hidden = false;
    alertText.textContent = chanAlarm;
  } else {
    alertBtn.hidden = true;
  }
}

async function refreshSnapshot() {
  try {
    AGCState.snapshot = await apiGet("/api/snapshot");
    if (AGCState.snapshot.runtime_settings) {
      AGCState.runtimeSettings = AGCState.snapshot.runtime_settings;
    }
    updateTopbar();
    if (AGCState.page === "overview") renderOverviewPage();
    else if (AGCState.page === "diagram") updateDiagramTelemetryOverlay();
    else if (AGCState.page === "measurements") {
      renderMeasurementsPage();
      refreshMeasurementTrace();
    }
  } catch (err) {
    AGCState.snapshot = null;
    updateTopbar();
  }
}

async function switchPage(pageName) {
  AGCState.page = pageName;
  document.querySelectorAll(".page-nav button").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.navPage === pageName);
  });
  document.querySelectorAll(".page-section").forEach((sec) => {
    sec.classList.toggle("is-active", sec.dataset.page === pageName);
  });

  try {
    if (pageName === "overview") {
      renderOverviewPage();
    } else if (pageName === "agc-control") {
      await refreshAgcControlData();
      if (AGCState.detailTab === "trend") await refreshAgcTrendData();
      else await refreshAgcControlLogs();
    } else if (pageName === "device-config") {
      await refreshDeviceConfigsPage(true);
    } else if (pageName === "diagram") {
      await ensureDiagramLoaded();
    } else if (pageName === "model") {
      await renderModelPage();
    } else if (pageName === "measurements") {
      renderMeasurementsPage();
      await refreshMeasurementTrace();
    } else if (pageName === "commands") {
      await refreshCommandsPage();
    } else if (pageName === "curves") {
      await refreshCurvesPage();
    } else if (pageName === "parameters") {
      await refreshRuntimeSettingsPage();
    } else if (pageName === "history") {
      await refreshRuntimeLogsPage();
    }
  } catch (err) {
    showToast(err.message, "danger");
  }
}

function redrawActiveCharts() {
  if (AGCState.page === "agc-control" && AGCState.detailTab === "trend") {
    renderAgcMetricsTable();
    renderAgcTrendChart();
  } else if (AGCState.page === "measurements") {
    refreshMeasurementTrace();
  } else if (AGCState.page === "commands") {
    refreshCommandTrace();
  } else if (AGCState.page === "curves") {
    renderCurvesPage();
  } else if (AGCState.page === "diagram") {
    updateDiagramTelemetryOverlay();
  } else if (AGCState.page === "device-config") {
    drawDeviceCurveCanvas();
  }
}

function bindGlobalShellEvents() {
  // 导航切换
  document.querySelectorAll(".page-nav button").forEach((btn) => {
    btn.addEventListener("click", () => switchPage(btn.dataset.navPage));
  });

  // 主页快捷按钮与底部多标签分页切换
  document.getElementById("ovGoAgcBtn")?.addEventListener("click", () => switchPage("agc-control"));
  document.querySelectorAll("#ovBottomTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.ovTab || "summary";
      document.querySelectorAll("#ovBottomTabs button").forEach((b) => {
        b.classList.toggle("is-active", b === btn);
      });
      const sumPane = document.getElementById("ovPaneSummary");
      const cmdPane = document.getElementById("ovPaneCommands");
      if (sumPane) sumPane.hidden = tab !== "summary";
      if (cmdPane) cmdPane.hidden = tab !== "commands";
    });
  });
  document.getElementById("ovQuickCalcBtn")?.addEventListener("click", async () => {
    try {
      const res = await apiPost("/api/agc/control", { action: "calculate_once" });
      AGCState.control = res.control;
      await refreshSnapshot();
      showToast(`单次策略计算完成（周期 #${res.control.cycle}）`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  document.getElementById("topbarAlertBadge")?.addEventListener("click", () => {
    const alertChk = document.getElementById("runtimeLogAlertOnly");
    if (alertChk) alertChk.checked = true;
    switchPage("history");
  });

  // 顶栏一键紧急停控（高危确认 -> 停自动 + 强制退开环）
  document.getElementById("topbarEmergencyStopBtn")?.addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: t("topbar.estopConfirmTitle"),
      target: "AGC 自动控制 + 闭环四遥下发",
      message: t("topbar.estopConfirmMsg"),
      confirmText: t("topbar.estop")
    });
    if (!ok) return;
    try {
      const res = await apiPost("/api/agc/emergency-stop", { reason: "顶栏紧急停控按钮触发" });
      AGCState.control = res.control;
      await refreshSnapshot();
      if (AGCState.page === "agc-control") renderAgcControlPage();
      showToast("已执行紧急停控：自动控制已停止并退回开环", "warn");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  // 顶栏系统设置弹窗
  document.getElementById("openSystemSettingsBtn")?.addEventListener("click", async () => {
    try {
      await refreshRuntimeSettingsPage();
      document.getElementById("systemSettingsDialog")?.showModal();
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  // 侧栏折叠
  document.getElementById("appSidebarToggle")?.addEventListener("click", () => {
    const collapsed = document.documentElement.dataset.sidebarCollapsed === "true";
    document.documentElement.dataset.sidebarCollapsed = collapsed ? "false" : "true";
    try { localStorage.setItem("agcSidebarCollapsed", collapsed ? "0" : "1"); } catch (_e) {}
    setTimeout(redrawActiveCharts, 200);
  });

  // 主题色卡选择器（14 套风格分组切换，同步刷新 Canvas 调色板）
  const initTheme = document.documentElement.dataset.theme || "minimalist-light";
  syncThemePickerTrigger(initTheme);
  renderThemePopover();
  const themeBtn = document.getElementById("themePickerBtn");
  const themePop = document.getElementById("themePopover");
  themeBtn?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const open = themePop && themePop.hidden;
    if (themePop) {
      if (open) renderThemePopover();
      themePop.hidden = !open;
    }
    themeBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", (ev) => {
    if (!themePop || themePop.hidden) return;
    if (themePop.contains(ev.target) || themeBtn?.contains(ev.target)) return;
    closeThemePopover();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && themePop && !themePop.hidden) closeThemePopover();
  });
  const themeSel = document.getElementById("themeSelector");
  if (themeSel) {
    themeSel.value = initTheme;
    themeSel.addEventListener("change", () => applyThemeChoice(themeSel.value, true));
  }

  // 语言切换
  const langSel = document.getElementById("langSelector");
  if (langSel) {
    langSel.value = document.documentElement.dataset.lang || "zh-CN";
    langSel.addEventListener("change", () => {
      const lang = langSel.value;
      document.documentElement.dataset.lang = lang;
      try { localStorage.setItem("agcUiLang", lang); } catch (_e) {}
      applyI18n();
      syncThemePickerTrigger(document.documentElement.dataset.theme || "minimalist-light");
      renderThemePopover();
      updateTopbar();
      if (AGCState.page === "agc-control") renderAgcControlPage();
      else if (AGCState.page === "overview") renderOverviewPage();
      else if (AGCState.page === "model") renderModelPage();
      else if (AGCState.page === "measurements") renderMeasurementsPage();
      else if (AGCState.page === "commands") renderCommandsPage();
      else if (AGCState.page === "device-config") {
        renderDeviceConfigsToolbar();
        renderDeviceConfigsTable();
        renderDeviceCurveHeader();
        drawDeviceCurveCanvas();
      }
    });
  }

  // 通用关闭弹窗按钮
  document.querySelectorAll("[data-close-dialog]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById(btn.dataset.closeDialog)?.close();
    });
  });

  window.addEventListener("resize", () => {
    redrawActiveCharts();
  });
}

async function pollingTick() {
  if (document.hidden) return;
  await refreshSnapshot();
  if (AGCState.page === "agc-control") {
    await refreshAgcControlData();
    if (AGCState.detailTab === "trend" && !AGCState.trendEndMs) {
      await refreshAgcTrendData();
    }
  } else if (AGCState.page === "commands") {
    await refreshCommandsPage();
  } else if (AGCState.page === "device-config") {
    await refreshDeviceConfigsLiveOnly();
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  applyI18n();
  initAllSplitters();
  bindGlobalShellEvents();
  bindDiagramInteractions();
  bindAgcControlEvents();
  bindDeviceConfigEvents();
  bindMonitorPageEvents();

  await Promise.all([refreshSnapshot(), refreshAgcControlData()]);
  renderOverviewPage();

  const scheduleNext = () => {
    const sec = Math.max(0.8, Number(AGCState.runtimeSettings?.web?.frontend_refresh_seconds || 1.5));
    setTimeout(async () => {
      try { await pollingTick(); } catch (_e) {}
      scheduleNext();
    }, sec * 1000);
  };
  scheduleNext();
});
