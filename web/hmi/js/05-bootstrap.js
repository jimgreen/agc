// 启动引导、导航切换、主题与多语言实时切换及定时轮询调度

const DARK_THEMES = new Set(["precision-dark", "glassmorphism", "sci-fi-cyan", "sci-fi-purple", "sci-fi-matrix"]);

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
  if (hasErr) {
    alertBtn.hidden = false;
    alertText.textContent = "SCADA 采集异常";
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
      renderSeriesPicker();
      if (AGCState.detailTab === "trend") await refreshAgcTrendData();
      else await refreshAgcControlLogs();
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
    renderSeriesPicker();
    renderAgcTrendChart();
  } else if (AGCState.page === "measurements") {
    refreshMeasurementTrace();
  } else if (AGCState.page === "commands") {
    refreshCommandTrace();
  } else if (AGCState.page === "curves") {
    renderCurvesPage();
  } else if (AGCState.page === "diagram") {
    updateDiagramTelemetryOverlay();
  }
}

function bindGlobalShellEvents() {
  // 导航切换
  document.querySelectorAll(".page-nav button").forEach((btn) => {
    btn.addEventListener("click", () => switchPage(btn.dataset.navPage));
  });

  // 主页快捷按钮
  document.getElementById("ovGoAgcBtn")?.addEventListener("click", () => switchPage("agc-control"));
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

  document.getElementById("topbarAlertBadge")?.addEventListener("click", () => switchPage("history"));

  // 侧栏折叠
  document.getElementById("appSidebarToggle")?.addEventListener("click", () => {
    const collapsed = document.documentElement.dataset.sidebarCollapsed === "true";
    document.documentElement.dataset.sidebarCollapsed = collapsed ? "false" : "true";
    try { localStorage.setItem("agcSidebarCollapsed", collapsed ? "0" : "1"); } catch (_e) {}
    setTimeout(redrawActiveCharts, 200);
  });

  // 主题切换（同步刷新 Canvas 调色板与 SVG 标注）
  const themeSel = document.getElementById("themeSelector");
  if (themeSel) {
    themeSel.value = document.documentElement.dataset.theme || "minimalist-light";
    themeSel.addEventListener("change", () => {
      const theme = themeSel.value;
      document.documentElement.dataset.theme = theme;
      document.documentElement.dataset.colorScheme = DARK_THEMES.has(theme) ? "dark" : "light";
      try { localStorage.setItem("agcUiTheme", theme); } catch (_e) {}
      redrawActiveCharts();
    });
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
      updateTopbar();
      if (AGCState.page === "agc-control") renderAgcControlPage();
      else if (AGCState.page === "overview") renderOverviewPage();
      else if (AGCState.page === "model") renderModelPage();
      else if (AGCState.page === "measurements") renderMeasurementsPage();
      else if (AGCState.page === "commands") renderCommandsPage();
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
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  applyI18n();
  initAllSplitters();
  bindGlobalShellEvents();
  bindDiagramInteractions();
  bindAgcControlEvents();
  bindMonitorPageEvents();
  renderSeriesPicker();

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
