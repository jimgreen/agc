// 启动引导、导航切换、14 套主题可视化切换与多语言实时调度

const THEME_CATALOG = [
  {
    groupKey: "theme.group.celestial",
    items: [
      { id: "celestial-aurora", dark: true, canvas: "#081020", surface: "#11223E", accent: "#38E8DF", ok: "#34D399" },
      { id: "celestial-mist", dark: false, canvas: "#F1F7FC", surface: "#FFFFFF", accent: "#0EA5E9", ok: "#0D9488" },
      { id: "glassmorphism", dark: true, canvas: "#101A2E", surface: "#223452", accent: "#60A5FA", ok: "#34D399" },
      { id: "sci-fi-purple", dark: true, canvas: "#0B081A", surface: "#161030", accent: "#C084FC", ok: "#2DD4BF" }
    ]
  },
  {
    groupKey: "theme.group.scifi",
    items: [
      { id: "sci-fi-cyan", dark: true, canvas: "#060D1C", surface: "#0C1930", accent: "#22D3EE", ok: "#34D399" },
      { id: "sci-fi-matrix", dark: true, canvas: "#06110C", surface: "#0C2017", accent: "#34D399", ok: "#34D399" },
      { id: "sci-fi-crimson", dark: true, canvas: "#120910", surface: "#1E101A", accent: "#FB7185", ok: "#34D399" },
      { id: "precision-dark", dark: true, canvas: "#0B101D", surface: "#131C2E", accent: "#38BDF8", ok: "#34D399" }
    ]
  },
  {
    groupKey: "theme.group.light",
    items: [
      { id: "minimalist-light", dark: false, canvas: "#F5F8FC", surface: "#FFFFFF", accent: "#3B82F6", ok: "#0D9488" },
      { id: "bento-grid", dark: false, canvas: "#F5F5F7", surface: "#FFFFFF", accent: "#0071E3", ok: "#0D9488" },
      { id: "nordic-frost", dark: false, canvas: "#ECF4FA", surface: "#F9FCFF", accent: "#0284C7", ok: "#0F766E" },
      { id: "warm-paper", dark: false, canvas: "#F5F0E7", surface: "#FCF9F2", accent: "#2E6F40", ok: "#2E6F40" },
      { id: "blueprint-light", dark: false, canvas: "#ECF4FC", surface: "#F8FBFF", accent: "#2563EB", ok: "#0F766E" },
      { id: "editorial", dark: false, canvas: "#FAF9F6", surface: "#FFFFFF", accent: "#9F1239", ok: "#15803D" }
    ]
  },
  {
    groupKey: "theme.group.dark",
    items: [
      { id: "nord-slate", dark: true, canvas: "#1E2430", surface: "#272F3F", accent: "#88C0D0", ok: "#A3BE8C" },
      { id: "obsidian-amber", dark: true, canvas: "#13110F", surface: "#1D1A16", accent: "#F59E0B", ok: "#34D399" }
    ]
  }
];

const THEME_BY_ID = new Map(THEME_CATALOG.flatMap((g) => g.items).map((it) => [it.id, it]));
const DARK_THEMES = new Set(Array.from(THEME_BY_ID.values()).filter((it) => it.dark).map((it) => it.id));

function syncThemePickerTrigger(themeId) {
  const item = THEME_BY_ID.get(themeId) || THEME_BY_ID.get("celestial-aurora");
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
  } else if (dots.length === 2) {
    dots[0].style.background = item.canvas;
    dots[1].style.background = item.accent;
  }
  const hiddenSel = document.getElementById("themeSelector");
  if (hiddenSel && hiddenSel.value !== item.id) hiddenSel.value = item.id;
}

function syncLangControls(lang) {
  const activeLang = ["zh-CN", "en-US", "ru-RU"].includes(lang) ? lang : "zh-CN";
  document.querySelectorAll("#langSegmented button[data-set-lang]").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.setLang === activeLang);
  });
  const langSel = document.getElementById("langSelector");
  if (langSel && langSel.value !== activeLang) langSel.value = activeLang;
}

function applyLangChoice(lang) {
  const nextLang = ["zh-CN", "en-US", "ru-RU"].includes(lang) ? lang : "zh-CN";
  document.documentElement.dataset.lang = nextLang;
  try { localStorage.setItem("agcUiLang", nextLang); } catch (_e) {}
  syncLangControls(nextLang);
  applyI18n();
  syncWallboardShellControls();
  syncThemePickerTrigger(document.documentElement.dataset.theme || "celestial-aurora");
  renderThemePopover();
  updateTopbar();
  if (AGCState.page === "wallboard") {
    renderWallboardPage();
    renderWallboardTrendChart();
  } else if (AGCState.page === "agc-control") renderAgcControlPage();
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
}

function closeAllPopMenus(exceptId = null) {
  document.querySelectorAll(".pop-menu-panel").forEach((panel) => {
    if (exceptId && panel.id === exceptId) return;
    panel.hidden = true;
  });
  document.querySelectorAll("[data-pop-trigger]").forEach((btn) => {
    const targetId = btn.dataset.popTrigger;
    btn.setAttribute("aria-expanded", exceptId && targetId === exceptId ? "true" : "false");
  });
}

function renderThemePopover() {
  const pop = document.getElementById("themePopover");
  if (!pop) return;
  const active = document.documentElement.dataset.theme || "celestial-aurora";
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
      closeAllPopMenus();
    });
  });
}

function closeThemePopover() {
  closeAllPopMenus();
}

function applyThemeChoice(themeId, persist = true) {
  const theme = THEME_BY_ID.has(themeId) ? themeId : "celestial-aurora";
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

async function refreshSnapshot(forceFull = false) {
  try {
    const prevSnap = AGCState.snapshot;
    const prevAgc = (prevSnap && prevSnap.agc_status) || {};
    const needFullScada = forceFull || !prevSnap || AGCState.page === "measurements" || AGCState.page === "diagram" || AGCState.page === "commands";
    const qs = new URLSearchParams();
    if (!needFullScada) {
      qs.set("lite", "1");
      if (prevAgc.cycle !== undefined) qs.set("since_cycle", String(prevAgc.cycle));
      if (prevAgc.strategy_revision !== undefined) qs.set("since_rev", String(prevAgc.strategy_revision));
    }
    const url = qs.toString() ? `/api/snapshot?${qs.toString()}` : "/api/snapshot";
    const nextSnap = await apiGet(url);
    if (nextSnap.strategy_unchanged && prevSnap && prevSnap.latest_strategy) {
      nextSnap.latest_strategy = prevSnap.latest_strategy;
    }
    if (needFullScada === false && prevSnap) {
      if (!nextSnap.devices || !nextSnap.devices.length) nextSnap.devices = prevSnap.devices || [];
      if (nextSnap.measurements && (!nextSnap.measurements.scada || !nextSnap.measurements.scada.length) && prevSnap.measurements) {
        nextSnap.measurements = prevSnap.measurements;
      }
    }
    AGCState.snapshot = nextSnap;
    if (AGCState.snapshot.runtime_settings) {
      AGCState.runtimeSettings = AGCState.snapshot.runtime_settings;
    }
    updateTopbar();
    if (AGCState.page === "wallboard") renderWallboardPage();
    else if (AGCState.page === "overview") renderOverviewPage();
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
  if (typeof syncWallboardShellControls === "function") {
    syncWallboardShellControls();
  }

  try {
    if (pageName === "wallboard") {
      renderWallboardPage();
      await refreshWallboardExtraData();
    } else if (pageName === "overview") {
      renderOverviewPage();
    } else if (pageName === "agc-control") {
      await refreshAgcControlData();
      if (AGCState.detailTab === "trend") await refreshAgcTrendData();
      else await refreshAgcControlLogs();
    } else if (pageName === "device-config") {
      await refreshDeviceConfigsPage(true);
    } else if (pageName === "diagram") {
      await refreshSnapshot(true);
      await ensureDiagramLoaded();
    } else if (pageName === "model") {
      await renderModelPage();
    } else if (pageName === "measurements") {
      await refreshSnapshot(true);
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
  if (AGCState.page === "wallboard") {
    renderWallboardPage();
    renderWallboardTrendChart();
  } else if (AGCState.page === "agc-control" && AGCState.detailTab === "trend") {
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

let _sidebarHideTimer = null;
let _sidebarCooldownUntil = 0;

function getSidebarMode() {
  return document.documentElement.dataset.sidebarMode === "embed" ? "embed" : "float";
}

function syncSidebarModeControls() {
  const mode = getSidebarMode();
  document.querySelectorAll("#sidebarModeGroup button").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.setSidebarMode === mode);
  });
}

function setSidebarOpen(open, { manual = false, persistEmbed = true } = {}) {
  if (_sidebarHideTimer) {
    clearTimeout(_sidebarHideTimer);
    _sidebarHideTimer = null;
  }
  const flag = Boolean(open);
  const mode = getSidebarMode();
  if (!flag && manual) {
    _sidebarCooldownUntil = Date.now() + 450;
  }
  const changed = (document.documentElement.dataset.sidebarOpen === "true") !== flag;
  document.documentElement.dataset.sidebarOpen = flag ? "true" : "false";
  if (mode === "embed" && persistEmbed) {
    try { localStorage.setItem("agcSidebarEmbedHidden", flag ? "0" : "1"); } catch (_e) {}
  }
  if (changed && mode === "embed") {
    setTimeout(redrawActiveCharts, 200);
  }
}

function setSidebarMode(mode, persist = true) {
  const target = mode === "embed" ? "embed" : "float";
  const prev = getSidebarMode();
  document.documentElement.dataset.sidebarMode = target;
  if (persist) {
    try { localStorage.setItem("agcSidebarMode", target); } catch (_e) {}
  }
  syncSidebarModeControls();
  if (target === "embed") {
    // 切换至内嵌模式时立即常驻展示（用户仍可随时点击隐藏）
    setSidebarOpen(true, { persistEmbed: persist });
  } else if (prev === "embed") {
    // 从内嵌切至悬浮模式时保持当前打开以便用户看见切换反馈，移出左栏后自动隐藏
    setTimeout(redrawActiveCharts, 200);
  }
}

function openFloatingSidebar() {
  if (Date.now() < _sidebarCooldownUntil) return;
  setSidebarOpen(true, { persistEmbed: false });
}

function closeFloatingSidebar(manual = false) {
  setSidebarOpen(false, { manual, persistEmbed: true });
}

function scheduleCloseFloatingSidebar(delayMs = 150) {
  if (getSidebarMode() !== "float") return;
  if (_sidebarHideTimer) clearTimeout(_sidebarHideTimer);
  _sidebarHideTimer = setTimeout(() => {
    _sidebarHideTimer = null;
    if (getSidebarMode() === "float") {
      document.documentElement.dataset.sidebarOpen = "false";
    }
  }, delayMs);
}

function bindGlobalShellEvents() {
  syncSidebarModeControls();

  // 顶栏左侧显隐切换按钮（悬浮/内嵌双模式通用）
  document.getElementById("topbarSidebarBtn")?.addEventListener("click", () => {
    const isOpen = document.documentElement.dataset.sidebarOpen === "true";
    setSidebarOpen(!isOpen, { manual: isOpen, persistEmbed: true });
  });

  // 左栏底部分段模式切换（悬浮 / 内嵌）
  document.querySelectorAll("#sidebarModeGroup button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setSidebarMode(btn.dataset.setSidebarMode, true);
    });
  });

  // 导航切换（悬浮模式下选中后自动收起；内嵌模式下保持常驻）
  document.querySelectorAll(".page-nav button").forEach((btn) => {
    btn.addEventListener("click", () => {
      switchPage(btn.dataset.navPage);
      if (getSidebarMode() === "float") {
        scheduleCloseFloatingSidebar(100);
      }
    });
  });

  // 左边缘感应与离开自动隐藏
  const sidebarEl = document.getElementById("appSidebar");
  const edgeTriggerEl = document.getElementById("sidebarEdgeTrigger");
  edgeTriggerEl?.addEventListener("mouseenter", () => {
    if (getSidebarMode() === "float") openFloatingSidebar();
  });
  edgeTriggerEl?.addEventListener("click", () => {
    setSidebarOpen(true, { persistEmbed: true });
  });
  sidebarEl?.addEventListener("mouseenter", () => {
    if (_sidebarHideTimer) {
      clearTimeout(_sidebarHideTimer);
      _sidebarHideTimer = null;
    }
  });
  sidebarEl?.addEventListener("mouseleave", (ev) => {
    if (getSidebarMode() !== "float") return;
    if (ev.clientX <= 4) return;
    scheduleCloseFloatingSidebar(150);
  });
  document.addEventListener("mousemove", (ev) => {
    if (getSidebarMode() !== "float") return;
    const isOpen = document.documentElement.dataset.sidebarOpen === "true";
    if (!isOpen) {
      if (ev.clientX <= 14 && ev.clientY >= 40 && !document.querySelector("dialog[open]")) {
        openFloatingSidebar();
      }
    } else if (ev.clientX > 212) {
      scheduleCloseFloatingSidebar(140);
    }
  }, { passive: true });

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

  // 侧栏手动立即收起按钮（悬浮/内嵌两种模式下均可一键隐藏）
  document.getElementById("appSidebarToggle")?.addEventListener("click", () => {
    closeFloatingSidebar(true);
  });

  // 顶栏三合一偏好菜单（语言 + 16套风格 + 系统参数）与全站通用收纳弹出层
  const initTheme = document.documentElement.dataset.theme || "celestial-aurora";
  const initLang = document.documentElement.dataset.lang || "zh-CN";
  syncThemePickerTrigger(initTheme);
  syncLangControls(initLang);
  renderThemePopover();

  document.querySelectorAll("[data-pop-trigger]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const targetId = btn.dataset.popTrigger;
      const panel = targetId ? document.getElementById(targetId) : null;
      if (!panel) return;
      const willOpen = panel.hidden;
      closeAllPopMenus(willOpen ? targetId : null);
      if (willOpen && targetId === "prefsPopover") {
        syncLangControls(document.documentElement.dataset.lang || "zh-CN");
        renderThemePopover();
      }
      panel.hidden = !willOpen;
    });
  });

  document.querySelectorAll(".pop-menu-panel").forEach((panel) => {
    panel.addEventListener("click", (ev) => {
      const closeTarget = ev.target.closest("[data-pop-close]");
      if (closeTarget && !closeTarget.disabled) {
        setTimeout(() => closeAllPopMenus(), 0);
      }
    });
  });

  document.addEventListener("click", (ev) => {
    if (ev.target.closest(".pop-menu-wrap")) return;
    closeAllPopMenus();
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeAllPopMenus();
  });

  const themeSel = document.getElementById("themeSelector");
  if (themeSel) {
    themeSel.value = initTheme;
    themeSel.addEventListener("change", () => applyThemeChoice(themeSel.value, true));
  }

  document.querySelectorAll("#langSegmented button[data-set-lang]").forEach((btn) => {
    btn.addEventListener("click", () => applyLangChoice(btn.dataset.setLang));
  });

  const langSel = document.getElementById("langSelector");
  if (langSel) {
    langSel.value = initLang;
    langSel.addEventListener("change", () => applyLangChoice(langSel.value));
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

let _wbExtraTickCounter = 0;
async function pollingTick() {
  if (document.hidden) return;
  await refreshSnapshot();
  if (AGCState.page === "wallboard") {
    _wbExtraTickCounter = (_wbExtraTickCounter + 1) % 3;
    if (_wbExtraTickCounter === 0) {
      await refreshWallboardExtraData();
    }
  } else if (AGCState.page === "agc-control") {
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
  bindWallboardEvents();

  const urlParams = new URLSearchParams(window.location.search);
  const pathName = window.location.pathname;
  const autoImmersive = pathName === "/screen" || pathName === "/wallboard" || urlParams.get("screen") === "1";
  if (autoImmersive) {
    document.documentElement.dataset.wallboardImmersive = "true";
    syncWallboardShellControls();
  }

  await Promise.all([refreshSnapshot(true), refreshAgcControlData(), refreshWallboardExtraData()]);
  renderWallboardPage();
  renderWallboardTrendChart();
  renderOverviewPage();

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      pollingTick();
    }
  });

  const scheduleNext = () => {
    const baseSec = Math.max(0.8, Number(AGCState.runtimeSettings?.web?.frontend_refresh_seconds || 1.5));
    const sec = document.hidden ? Math.max(6.0, baseSec * 4) : baseSec;
    setTimeout(async () => {
      try { await pollingTick(); } catch (_e) {}
      scheduleNext();
    }, sec * 1000);
  };
  scheduleNext();
});
