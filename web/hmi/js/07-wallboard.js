// AGC 专属态势监控大屏主画面模块：南极极端气象展示/输入与多能耦合、资源包络、闭环态势主图、双轴交互曲线与全屏投屏

let wbTrendWindow = 60;
let wbTrendCache = null;
let wbLogsBriefCache = [];
let wbHoverRatio = null;
let wbEnvFormDirty = false;

const WB_ANTARCTIC_PRESETS = {
  polar_day_gale: { wind_speed: 15.5, solar_irradiance: 820, air_temperature: -8, icing_pct: 5, thermal_pct: 0 },
  katabatic_blizzard: { wind_speed: 36.5, solar_irradiance: 35, air_temperature: -28, icing_pct: 25, thermal_pct: 22 },
  polar_night_calm: { wind_speed: 1.6, solar_irradiance: 0, air_temperature: -42, icing_pct: 12, thermal_pct: 35 },
  polar_transition_gust: { wind_speed: 8.8, solar_irradiance: 260, air_temperature: -18, icing_pct: 8, thermal_pct: 12 },
  antarctic_icing_derate: { wind_speed: 11.2, solar_irradiance: 180, air_temperature: -32, icing_pct: 30, thermal_pct: 25 }
};

const WB_POOL_GROUPS = [
  { key: "dc-wind", tab: "dc-wind", i18nKey: "cat.dcWind", match: (r) => r.category === "直流风电" },
  { key: "ac-wind", tab: "ac-wind", i18nKey: "cat.acWind", match: (r) => r.category === "交流风电" },
  { key: "dc-pv", tab: "dc-pv", i18nKey: "cat.dcPv", match: (r) => r.category === "直流光伏" },
  { key: "ac-pv", tab: "ac-pv", i18nKey: "cat.acPv", match: (r) => r.category === "交流光伏" },
  { key: "ac-gfl", tab: "ac-grid-storage", i18nKey: "cat.acGridStorage", match: (r) => r.category === "交流跟网储能" },
  { key: "dc-gfl", tab: "dc-grid-storage", i18nKey: "cat.dcGridStorage", match: (r) => r.category === "直流跟网储能" },
  { key: "gfm", tab: "ac-balance-storage", i18nKey: "cat.acBalStorage", match: (r) => String(r.category || "").includes("平衡储能") },
  { key: "diesel", tab: "diesel", i18nKey: "cat.diesel", match: (r) => String(r.category || "").includes("柴油") },
  { key: "couple", tab: "converter", i18nKey: "cat.converter", match: (r) => String(r.category || "").includes("变流") || String(r.category || "").includes("氢") }
];

function isWallboardImmersive() {
  return Boolean(document.fullscreenElement) || document.documentElement.dataset.wallboardImmersive === "true";
}

function syncWallboardShellControls() {
  const immersive = isWallboardImmersive();
  const titleEl = document.querySelector(".brand-text h1");
  if (titleEl) {
    const key = immersive && AGCState.page === "wallboard" ? "wb.title" : "app.title";
    titleEl.dataset.i18n = key;
    titleEl.textContent = t(key);
  }
  const btn = document.getElementById("topbarWallboardBtn");
  const lbl = document.getElementById("topbarWallboardBtnLabel");
  const ico = document.getElementById("topbarFullscreenIco");
  if (btn && lbl) {
    const key = immersive ? "wb.exitScreen" : "wb.enterScreen";
    lbl.dataset.i18n = key;
    lbl.textContent = t(key);
    btn.title = t(key);
    btn.className = immersive ? "btn btn-sm btn-warn-outline" : "btn btn-sm btn-primary";
  }
  if (ico) {
    ico.innerHTML = immersive
      ? '<path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/>'
      : '<path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>';
  }
}

async function setWallboardImmersive(enabled, toggleBrowserFs = false) {
  const flag = Boolean(enabled);
  document.documentElement.dataset.wallboardImmersive = flag ? "true" : "false";
  if (typeof setSidebarOpen === "function" && typeof getSidebarMode === "function") {
    if (flag) {
      setSidebarOpen(false, { manual: true, persistEmbed: false });
    } else if (getSidebarMode() === "embed" && localStorage.getItem("agcSidebarEmbedHidden") !== "1") {
      setSidebarOpen(true, { persistEmbed: false });
    }
  }
  if (toggleBrowserFs) {
    try {
      if (flag && !document.fullscreenElement && document.documentElement.requestFullscreen) {
        await document.documentElement.requestFullscreen();
      } else if (!flag && document.fullscreenElement && document.exitFullscreen) {
        await document.exitFullscreen();
      }
    } catch (_e) {
      // 浏览器策略限制全屏时保持无侧栏满屏沉浸布局
    }
  }
  syncWallboardShellControls();
  setTimeout(redrawActiveCharts, 120);
}

function setWbSvgNode(id, { cur, tgt, deltaKw, meta, dir, ringPct }) {
  const el = document.getElementById(id);
  if (!el) return;
  const setTxt = (sel, txt) => {
    const n = el.querySelector(sel);
    if (n && txt !== undefined) n.textContent = txt;
  };
  setTxt("[data-wb-cur]", cur);
  setTxt("[data-wb-tgt]", tgt);
  setTxt("[data-wb-meta]", meta);
  setTxt("[data-wb-dir]", dir);

  const deltaEl = el.querySelector("[data-wb-delta]");
  if (deltaEl && deltaKw !== undefined) {
    const d = Number(deltaKw || 0);
    deltaEl.classList.remove("wb-svg-delta-up", "wb-svg-delta-dn", "wb-svg-delta-flat");
    if (!Number.isFinite(d) || Math.abs(d) < 0.05) {
      deltaEl.textContent = "Δ0.0";
      deltaEl.classList.add("wb-svg-delta-flat");
    } else if (d > 0) {
      deltaEl.textContent = `▲+${d.toFixed(1)}`;
      deltaEl.classList.add("wb-svg-delta-up");
    } else {
      deltaEl.textContent = `▼${d.toFixed(1)}`;
      deltaEl.classList.add("wb-svg-delta-dn");
    }
  }

  if (ringPct !== undefined) {
    const ring = el.querySelector("[data-wb-ring]");
    if (ring) {
      const r = Number(ring.getAttribute("r") || 17);
      const circ = 2 * Math.PI * r;
      const clamped = Math.max(0, Math.min(100, Number(ringPct) || 0));
      ring.setAttribute("stroke-dasharray", circ.toFixed(2));
      ring.setAttribute("stroke-dashoffset", (circ * (1 - clamped / 100)).toFixed(2));
    }
  }
}

function windRegimeMeta(regime) {
  switch (regime) {
    case "katabatic_cutout":
      return { cls: "badge-danger", txt: `⚠ ${t("wb.regCutout")}` };
    case "rated_full":
      return { cls: "badge-ok", txt: `✦ ${t("wb.regRated")}` };
    case "ramp_cubic":
      return { cls: "badge-info", txt: `↗ ${t("wb.regRamp")}` };
    case "calm_cutin":
      return { cls: "badge-neutral", txt: `◌ ${t("wb.regCalm")}` };
    default:
      return { cls: "badge-neutral", txt: "--" };
  }
}

function lightRegimeMeta(regime) {
  switch (regime) {
    case "polar_day":
      return { cls: "badge-ok", txt: `☀ ${t("wb.regDay")}` };
    case "twilight_weak":
      return { cls: "badge-info", txt: `◐ ${t("wb.regWeak")}` };
    case "polar_night":
    default:
      return { cls: "badge-neutral", txt: `☾ ${t("wb.regNight")}` };
  }
}

async function applyAntarcticEnvOverride(explicitPreset = null) {
  const presetSel = document.getElementById("wbEnvPresetSelect");
  const preset = explicitPreset || (presetSel ? presetSel.value : "custom");
  if (preset === "scada_live") {
    return resetAntarcticEnvOverride();
  }

  // 高危安全门控：若当前处于闭环模式，先显式确认退回开环再执行人工极端工况注入
  const curLoop = (AGCState.snapshot && AGCState.snapshot.agc_status && AGCState.snapshot.agc_status.loop_mode) || "open";
  if (curLoop === "closed") {
    const ok = await confirmDanger({
      title: "注入南极极端工况并退回开环？",
      target: "AGC 气象耦合仿真",
      message: "人工注入极端气象工况将自动切换至开环安全模式执行策略重算，不向现场设备发送真实四遥指令。",
      confirmText: t("wb.envApply")
    });
    if (!ok) return;
  }

  const ws = Number(document.getElementById("wbInpWind")?.value ?? 10);
  const irr = Number(document.getElementById("wbInpSolar")?.value ?? 200);
  const temp = Number(document.getElementById("wbInpTemp")?.value ?? -15);
  const icingPct = Number(document.getElementById("wbInpIcing")?.value ?? 0);
  const thermalPct = Number(document.getElementById("wbInpThermal")?.value ?? 0);

  try {
    const res = await apiPost("/api/agc/environment", {
      action: "apply",
      preset,
      wind_speed: ws,
      solar_irradiance: irr,
      air_temperature: temp,
      icing_ratio: Math.max(0, Math.min(0.6, icingPct / 100)),
      load_thermal_pct: thermalPct,
      recalculate: true
    });
    wbEnvFormDirty = false;
    AGCState.control = res.control;
    await Promise.all([refreshSnapshot(true), refreshWallboardExtraData()]);
    renderWallboardPage();
    showToast(`南极工况耦合试算已生效（周期 #${res.control?.cycle || 0}）`, "ok");
  } catch (err) {
    showToast(err.message, "danger");
  }
}

async function resetAntarcticEnvOverride() {
  try {
    const res = await apiPost("/api/agc/environment", { action: "reset", recalculate: true });
    wbEnvFormDirty = false;
    AGCState.control = res.control;
    await Promise.all([refreshSnapshot(true), refreshWallboardExtraData()]);
    renderWallboardPage();
    showToast("已恢复 SCADA 实时气象遥测输入", "ok");
  } catch (err) {
    showToast(err.message, "danger");
  }
}

async function refreshWallboardExtraData() {
  try {
    const [trendRes, logRes] = await Promise.all([
      apiGet(`/api/agc/trend?window=${encodeURIComponent(String(wbTrendWindow))}&limit=360`),
      apiGet("/api/agc/logs?page=1&page_size=6&level=all")
    ]);
    wbTrendCache = trendRes;
    wbLogsBriefCache = (logRes && logRes.items) || [];
    if (AGCState.page === "wallboard") {
      renderWallboardTrendChart();
      renderWallboardDecisionStream();
    }
  } catch (_e) {
    // 网络瞬断时保留上一拍缓存
  }
}

function renderWallboardPage() {
  const snap = AGCState.snapshot;
  if (!snap) return;
  const agc = snap.agc_status || {};
  const env = snap.environment || {};
  const cpl = snap.antarctic_coupling || (AGCState.control && AGCState.control.antarctic_coupling) || {};
  const latest = snap.latest_strategy || (AGCState.control && AGCState.control.latest) || null;
  const m = (latest && latest.metrics) || {};
  const rows = (latest && latest.command_rows) || [];
  const scada = (snap.measurements && snap.measurements.scada) || [];
  const settings = (AGCState.control && AGCState.control.settings) || {};
  const diag = (AGCState.control && AGCState.control.island_diagnostics) || {};

  // 1. 核心功率与绿电指标推导
  let acLoadKw = m.acLoadKw;
  let dcLoadKw = m.dcLoadKw;
  let loadKw = m.loadKw;
  if (loadKw === undefined) {
    acLoadKw = scada.filter((x) => x.dev_type === "ACLoad" && x.meas_type === "P_LOAD" && x.valid).reduce((a, b) => a + Number(b.value || 0), 0);
    dcLoadKw = scada.filter((x) => x.dev_type === "DCLoad" && x.meas_type === "P_LOAD" && x.valid).reduce((a, b) => a + Number(b.value || 0), 0);
    loadKw = acLoadKw + dcLoadKw;
  }

  const acWindCur = Number(m.acWindCurrentKw || 0);
  const dcWindCur = Number(m.dcWindCurrentKw || 0);
  const acPvCur = Number(m.acPvCurrentKw || 0);
  const dcPvCur = Number(m.dcPvCurrentKw || 0);
  const acWindTgt = Number(m.acWindTargetKw ?? acWindCur);
  const dcWindTgt = Number(m.dcWindTargetKw ?? dcWindCur);
  const acPvTgt = Number(m.acPvTargetKw ?? acPvCur);
  const dcPvTgt = Number(m.dcPvTargetKw ?? dcPvCur);

  const renCur = acWindCur + dcWindCur + acPvCur + dcPvCur;
  const renTgt = acWindTgt + dcWindTgt + acPvTgt + dcPvTgt;
  const renMax = Number(m.acRenewableMaxAvailableKw || 0) + Number(m.dcRenewableMaxAvailableKw || 0);
  const renUtil = renMax > 0.5 ? Math.min(100, (renTgt / renMax) * 100) : (renCur > 0.1 ? 100 : 0);

  const acStCur = Number(m.acStorageCurrentKw || 0);
  const dcStCur = Number(m.dcStorageCurrentKw || 0);
  const acStTgt = Number(m.acStorageTargetKw ?? acStCur);
  const dcStTgt = Number(m.dcStorageTargetKw ?? dcStCur);
  const stCur = acStCur + dcStCur;
  const totalSoc = pctRatio(m.totalStorageSoc ?? m.storageSoc ?? m.acStorageSoc);

  const dieselCur = Number(m.totalDieselCurrentKw ?? m.dieselCurrentKw ?? 0);
  const dieselTgt = Number(m.totalDieselTargetKw ?? m.dieselTargetKw ?? dieselCur);
  const dieselMin = Number(m.totalDieselMinKw ?? m.dieselMinKw ?? 0);

  const acdcCur = Number(m.acdcCurrentKw || 0);
  const acdcTgt = Number(m.acdcTargetKw ?? acdcCur);

  const elzCur = Number(m.electrolyzerCurrentKw || 0);
  const fcCur = Number(m.fuelCellCurrentKw || 0);
  const h2Press = m.hydrogenStoragePressureMpa;
  const h2Soc = pctRatio(m.hydrogenStorageSoc);

  const totalSupply = Math.max(0.01, renCur + Math.max(0, dieselCur) + Math.max(0, stCur) + Math.max(0, fcCur));
  const greenShare = Math.min(100, (renCur / totalSupply) * 100);

  const changedCnt = rows.filter((r) => r.changed).length;
  const pendingCnt = rows.filter((r) => r.dispatch_status === "pending").length;
  const okCnt = rows.filter((r) => r.dispatch_status === "dispatched").length;

  // 2. 渲染顶部单行 8 项 KPI 条
  document.getElementById("wbKpiLoad").textContent = fmtNum(loadKw, 1);
  document.getElementById("wbKpiRen").textContent = `${fmtNum(renCur, 1)} / ${fmtNum(renTgt, 1)}`;
  document.getElementById("wbKpiUtil").textContent = fmtNum(renUtil, 1);
  document.getElementById("wbKpiShare").textContent = fmtNum(greenShare, 1);
  const dgEl = document.getElementById("wbKpiDiesel");
  dgEl.textContent = `${fmtNum(dieselCur, 1)} / ${fmtNum(dieselMin, 0)}`;
  dgEl.classList.toggle("is-warn", dieselMin > 1 && dieselCur < dieselMin - 0.5);
  document.getElementById("wbKpiStorage").textContent = fmtNum(stCur, 1);
  document.getElementById("wbKpiStorageSub").textContent = `kW · ${fmtNum(totalSoc, 0)}%`;
  document.getElementById("wbKpiHydro").textContent = fmtNum(elzCur - fcCur, 1);
  document.getElementById("wbKpiHydroSub").textContent = `kW · ${fmtNum(h2Press, 2)}MPa`;
  document.getElementById("wbKpiDispatch").textContent = `${changedCnt} / ${rows.length}`;
  document.getElementById("wbKpiAck").textContent = agc.loop_mode === "closed"
    ? (pendingCnt > 0 ? `⏳ ${pendingCnt}` : `✓ ${okCnt}`)
    : (cpl.override_active ? `✦ ${t("wb.srcSim")}` : `◌ ${t("mode.open")}`);

  // 3. 左上：南极极端气象与多能耦合舱
  renderWallboardPolarPanel({ env, cpl, agc, scada });

  // 4. 左下：受控资源包络 + 安全裕度 2×2 尺
  renderWallboardResourcePool(rows);
  renderWallboardGuardMeters({ m, settings, diag, dieselCur, dieselMin, h2Press, h2Soc });

  // 5. 中上：AGC 闭环态势矢量主图（含南极风况/光照/低温/热负荷耦合标注）
  renderWallboardCockpitSvg({
    agc,
    cpl,
    diag,
    loadKw,
    acLoadKw,
    dcLoadKw,
    acWindCur,
    acWindTgt,
    dcWindCur,
    dcWindTgt,
    acPvCur,
    acPvTgt,
    dcPvCur,
    dcPvTgt,
    acStCur,
    acStTgt,
    dcStCur,
    dcStTgt,
    dieselCur,
    dieselTgt,
    dieselMin,
    acdcCur,
    acdcTgt,
    elzCur,
    fcCur,
    h2Press,
    h2Soc,
    renUtil,
    greenShare,
    m
  });

  // 6. 右上：本周期调节指令执行榜
  renderWallboardCommandRank(rows);

  // 7. 右下：实时决策流（置顶南极气象耦合诊断）
  renderWallboardDecisionStream();
}

function renderWallboardPolarPanel({ env, cpl, agc, scada }) {
  const srcBadge = document.getElementById("wbEnvSourceBadge");
  const resetBtn = document.getElementById("wbEnvResetBtn");
  const simActive = Boolean(cpl.override_active);
  if (srcBadge) {
    srcBadge.className = `badge ${simActive ? "badge-warn" : "badge-ok"}`;
    srcBadge.textContent = simActive ? t("wb.srcSim") : t("wb.srcLive");
  }
  if (resetBtn) resetBtn.hidden = !simActive;

  const wMeta = windRegimeMeta(cpl.wind_regime);
  const regWindEl = document.getElementById("wbRegimeWind");
  if (regWindEl) {
    regWindEl.className = `badge ${wMeta.cls}`;
    regWindEl.textContent = wMeta.txt;
  }

  const lMeta = lightRegimeMeta(cpl.polar_light_regime);
  const regLightEl = document.getElementById("wbRegimeLight");
  if (regLightEl) {
    regLightEl.className = `badge ${lMeta.cls}`;
    regLightEl.textContent = lMeta.txt;
  }

  const chillEl = document.getElementById("wbWindChillTag");
  if (chillEl) {
    const wc = Number(cpl.wind_chill_c ?? -12);
    chillEl.textContent = `❄ ${fmtNum(wc, 1)}°C`;
    chillEl.title = `${t("wb.windChill")} ${fmtNum(wc, 1)}°C`;
    chillEl.style.color = wc <= -35 ? "var(--danger)" : (wc <= -20 ? "var(--warn)" : "var(--text-secondary)");
  }

  const icingPct = Math.round(Number(cpl.icing_ratio || 0) * 100);
  const icingTag = document.getElementById("wbIcingTag");
  if (icingTag) {
    icingTag.textContent = icingPct > 0 ? `-${icingPct}%` : "0%";
    icingTag.style.color = icingPct >= 20 ? "var(--warn)" : "var(--text-secondary)";
  }

  const thDelta = Number(cpl.thermal_delta_kw || 0);
  const thTag = document.getElementById("wbThermalTag");
  if (thTag) {
    thTag.textContent = `${thDelta >= 0 ? "+" : ""}${fmtNum(thDelta, 1)} kW`;
    thTag.style.color = thDelta > 1 ? "var(--warn)" : "var(--text-secondary)";
  }

  // 仅当用户未聚焦在气象输入表且无未提交改动时，同步刷新表单数值
  const polarBody = document.querySelector(".wb-polar-body");
  const hasFocusInside = polarBody && polarBody.contains(document.activeElement);
  if (!hasFocusInside && !wbEnvFormDirty) {
    const presetSel = document.getElementById("wbEnvPresetSelect");
    if (presetSel) presetSel.value = simActive ? (cpl.preset || "custom") : "scada_live";
    const setVal = (id, v) => {
      const inp = document.getElementById(id);
      if (inp && v !== null && v !== undefined && !Number.isNaN(Number(v))) inp.value = String(v);
    };
    setVal("wbInpWind", fmtNum(cpl.wind_speed ?? env.wind_speed ?? 0, 1));
    setVal("wbInpSolar", Math.round(Number(cpl.solar_irradiance ?? env.solar_irradiance ?? 0)));
    setVal("wbInpTemp", fmtNum(cpl.air_temperature ?? env.air_temperature ?? -12, 1));

    const icingSel = document.getElementById("wbInpIcing");
    if (icingSel) {
      const targetStr = String(icingPct);
      if (!Array.from(icingSel.options).some((o) => o.value === targetStr)) {
        const opt = document.createElement("option");
        opt.value = targetStr;
        opt.textContent = targetStr;
        icingSel.appendChild(opt);
      }
      icingSel.value = targetStr;
    }

    const thPct = Math.round(Number(cpl.load_thermal_pct || 0));
    const thSel = document.getElementById("wbInpThermal");
    if (thSel) {
      const targetStr = String(thPct);
      if (!Array.from(thSel.options).some((o) => o.value === targetStr)) {
        const opt = document.createElement("option");
        opt.value = targetStr;
        opt.textContent = `${thPct >= 0 ? "+" : ""}${thPct}`;
        thSel.appendChild(opt);
      }
      thSel.value = targetStr;
    }
  }

  // 底部 4 项耦合传导系数
  const wCap = Number(cpl.wind_cap_pct ?? 0);
  const pvCap = Number(cpl.pv_cap_pct ?? 0);
  const bessCap = Number(cpl.bess_cold_pct ?? 100);
  const wEl = document.getElementById("wbCplWind");
  if (wEl) {
    wEl.textContent = `${fmtNum(wCap, 1)}%`;
    wEl.style.color = cpl.wind_regime === "katabatic_cutout" ? "var(--danger)" : (wCap >= 80 ? "var(--ok)" : "var(--text-primary)");
  }
  const pvEl = document.getElementById("wbCplPv");
  if (pvEl) pvEl.textContent = `${fmtNum(pvCap, 1)}%`;
  const bsEl = document.getElementById("wbCplBess");
  if (bsEl) {
    bsEl.textContent = `${fmtNum(bessCap, 0)}%`;
    bsEl.style.color = bessCap < 85 ? "var(--warn)" : "var(--text-primary)";
  }
  const validMeas = agc.valid_meas_count !== undefined
    ? Number(agc.valid_meas_count)
    : scada.filter((x) => Number(x.valid) === 1).length;
  const measEl = document.getElementById("wbEnvMeas");
  if (measEl) {
    measEl.textContent = String(validMeas);
    measEl.title = `${t("overview.measCount")}: ${validMeas} (${fmtNum(agc.age_seconds, 1)}s)`;
  }
}

function renderWallboardResourcePool(rows) {
  const host = document.getElementById("wbResourcePool");
  const summaryBadge = document.getElementById("wbStateSummary");
  if (!host) return;

  let cntClosed = 0;
  let cntTrack = 0;
  let cntOpen = 0;
  rows.forEach((r) => {
    const st = String(r.device_control_state || "closed");
    if (st === "open") cntOpen += 1;
    else if (st === "track") cntTrack += 1;
    else cntClosed += 1;
  });
  if (summaryBadge) {
    summaryBadge.textContent = rows.length
      ? `◉${cntClosed} · ∿${cntTrack} · ○${cntOpen}`
      : "--";
    summaryBadge.title = `${t("devcfg.stateClosed")} ${cntClosed} / ${t("devcfg.stateTrack")} ${cntTrack} / ${t("devcfg.stateOpen")} ${cntOpen}`;
  }

  if (!rows.length) {
    host.innerHTML = `<div class="muted" style="padding:12px 4px;">—</div>`;
    return;
  }

  const clamp01 = (v) => Math.max(0, Math.min(1, v));
  host.innerHTML = WB_POOL_GROUPS.map((grp) => {
    const list = rows.filter(grp.match);
    const onlineCnt = list.filter((r) => r.online).length;
    const curSum = list.reduce((a, r) => a + Number(r.current_kw || 0), 0);
    const tgtSum = list.reduce((a, r) => a + Number(r.target_kw ?? r.current_kw ?? 0), 0);
    let loSum = list.reduce((a, r) => a + Number(r.env_min_kw ?? 0), 0);
    let hiSum = list.reduce((a, r) => a + Number(r.env_max_kw ?? r.available_kw ?? 0), 0);
    if (hiSum - loSum < 1) {
      loSum = Math.min(0, curSum, tgtSum);
      hiSum = Math.max(10, curSum, tgtSum, loSum + 10);
    }
    const span = Math.max(1e-3, hiSum - loSum);
    const zeroPct = loSum < -0.1 && hiSum > 0.1 ? clamp01((0 - loSum) / span) * 100 : null;
    const curPct = clamp01((curSum - loSum) / span) * 100;
    const tgtPct = clamp01((tgtSum - loSum) / span) * 100;
    const barLeft = zeroPct !== null ? Math.min(zeroPct, curPct) : 0;
    const barWidth = zeroPct !== null ? Math.max(1.5, Math.abs(curPct - zeroPct)) : Math.max(1.5, curPct);

    return `
      <div class="wb-pool-row" data-wb-pool-tab="${grp.tab}" title="${escapeHtml(t(grp.i18nKey))} [${fmtNum(loSum, 1)} ~ ${fmtNum(hiSum, 1)} kW]">
        <div class="wb-pool-head">
          <span class="wb-pool-name">${escapeHtml(t(grp.i18nKey))}<small class="mono">${onlineCnt}/${list.length}</small></span>
          <span class="wb-pool-nums mono">${fmtNum(curSum, 1)} → <strong>${fmtNum(tgtSum, 1)}</strong></span>
        </div>
        <div class="wb-pool-track">
          ${zeroPct !== null ? `<span class="wb-pool-zero" style="left:${zeroPct.toFixed(1)}%"></span>` : ""}
          <span class="wb-pool-cur" style="left:${barLeft.toFixed(1)}%;width:${barWidth.toFixed(1)}%"></span>
          <span class="wb-pool-tgt" style="left:calc(${tgtPct.toFixed(1)}% - 1px)"></span>
        </div>
      </div>`;
  }).join("");

  host.querySelectorAll("[data-wb-pool-tab]").forEach((rowEl) => {
    rowEl.addEventListener("click", async () => {
      jumpToAgcStrategyTab(rowEl.dataset.wbPoolTab);
    });
  });
}

function renderWallboardGuardMeters({ m, settings, diag, dieselCur, dieselMin, h2Press, h2Soc }) {
  const badge = document.getElementById("wbIslandBadge");
  if (badge) {
    if (!diag || !diag.status || diag.status === "idle") {
      badge.className = "badge badge-neutral";
      badge.textContent = "--";
    } else {
      const clsMap = { ok: "badge-ok", warn: "badge-warn", danger: "badge-danger" };
      badge.className = `badge ${clsMap[diag.status] || "badge-neutral"}`;
      badge.textContent = diag.summary || "--";
    }
  }

  const socMinPct = pctRatio(settings.soc_min ?? 0.2);
  const socMaxPct = pctRatio(settings.soc_max ?? 0.9);
  const acSoc = pctRatio(m.acStorageSoc);
  const dcSoc = pctRatio(m.dcStorageSoc);
  const dgRated = Math.max(dieselMin * 2.5, dieselCur * 1.2, 600);
  const dgPct = Math.max(0, Math.min(100, (dieselCur / dgRated) * 100));
  const dgMinPct = Math.max(0, Math.min(90, (dieselMin / dgRated) * 100));
  const h2Pct = Math.max(0, Math.min(100, Number(h2Soc || 0)));

  const buildMeter = (label, valText, pct, safeLo, safeHi) => {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    const stateCls = p < safeLo || p > safeHi ? (p < safeLo * 0.75 || p > Math.min(99, safeHi + 6) ? "is-danger" : "is-warn") : "";
    return `
      <div class="wb-guard-item">
        <div class="wb-guard-head">
          <span>${escapeHtml(label)}</span>
          <strong class="mono">${escapeHtml(valText)}</strong>
        </div>
        <div class="wb-guard-bar">
          <span class="wb-guard-safe" style="left:${safeLo.toFixed(1)}%;width:${Math.max(2, safeHi - safeLo).toFixed(1)}%"></span>
          <span class="wb-guard-fill ${stateCls}" style="width:${Math.max(2, p).toFixed(1)}%"></span>
        </div>
      </div>`;
  };

  const metersHost = document.getElementById("wbGuardMeters");
  if (metersHost) {
    metersHost.innerHTML = [
      buildMeter(t("wb.guardAcSoc"), `${fmtNum(acSoc, 1)}%`, acSoc, socMinPct, socMaxPct),
      buildMeter(t("wb.guardDcSoc"), `${fmtNum(dcSoc, 1)}%`, dcSoc, socMinPct, socMaxPct),
      buildMeter(t("wb.guardDiesel"), `${fmtNum(dieselCur, 0)}/${fmtNum(dieselMin, 0)}kW`, dgPct, dgMinPct, 92),
      buildMeter(t("wb.guardH2"), `${fmtNum(h2Press, 2)}MPa`, h2Pct, 15, 90)
    ].join("");
  }
}

function renderWallboardCockpitSvg(ctx) {
  const loopBadge = document.getElementById("wbLoopBadge");
  const isClosed = ctx.agc.loop_mode === "closed";
  if (loopBadge) {
    loopBadge.textContent = isClosed ? `◉ ${t("mode.closed")}` : `◌ ${t("mode.open")}`;
    loopBadge.className = `badge ${isClosed ? "badge-warn" : "badge-info"}`;
  }

  const dcGen = ctx.dcWindCur + ctx.dcPvCur + Math.max(0, ctx.dcStCur) + Math.max(0, ctx.fcCur);
  const acGen = ctx.acWindCur + ctx.acPvCur + Math.max(0, ctx.acStCur) + Math.max(0, ctx.dieselCur);
  document.getElementById("wbDcSummary").textContent = `⚡ ${fmtNum(dcGen, 1)} ⇢ ${fmtNum(ctx.dcLoadKw, 1)} kW`;
  document.getElementById("wbAcSummary").textContent = `⚡ ${fmtNum(acGen, 1)} ⇢ ${fmtNum(ctx.acLoadKw, 1)} kW`;

  const cpl = ctx.cpl || {};
  const wMeta = windRegimeMeta(cpl.wind_regime);
  const lMeta = lightRegimeMeta(cpl.polar_light_regime);
  const maxRes = ctx.diag && ctx.diag.islands && ctx.diag.islands.length
    ? Math.max(...ctx.diag.islands.map((i) => Math.abs(Number(i.max_balance_delta_kw || 0))))
    : 0;
  document.getElementById("wbCenterBanner").textContent =
    `${cpl.override_active ? `✦ ${t("wb.srcSim")} · ` : ""}${wMeta.txt} ${fmtNum(cpl.wind_speed, 1)}m/s · ${lMeta.txt} · ❄ ${fmtNum(cpl.wind_chill_c, 1)}°C`;
  document.getElementById("wbCoreMain").textContent =
    `✨ ${fmtNum(ctx.renUtil, 1)}%  ·  🌱 ${fmtNum(ctx.greenShare, 1)}%`;
  document.getElementById("wbCoreSub").textContent =
    `#${ctx.agc.cycle || 0} · ΔP ${fmtNum(maxRes, 2)} kW${ctx.diag?.solve_ms ? ` · ${ctx.diag.solve_ms}ms` : ""}`;

  const loadSafe = Math.max(0.01, Number(ctx.loadKw || 0));
  const thPct = Number(cpl.load_thermal_pct || 0);
  const thSuffix = Math.abs(thPct) > 0.1 ? ` · 🔥${thPct >= 0 ? "+" : ""}${thPct}%` : "";
  const dcLoadPct = `◔ ${fmtNum((Number(ctx.dcLoadKw || 0) / loadSafe) * 100, 1)}%${thSuffix}`;
  const acLoadPct = `◔ ${fmtNum((Number(ctx.acLoadKw || 0) / loadSafe) * 100, 1)}%${thSuffix}`;

  const windMetaSuffix = cpl.wind_regime === "katabatic_cutout"
    ? "⚠ ≥35m/s"
    : (cpl.wind_regime === "calm_cutin" ? "◌ <3m/s" : `↟ ${fmtNum(cpl.wind_cap_pct, 0)}%`);
  const pvMetaSuffix = cpl.polar_light_regime === "polar_night"
    ? "☾ 0W/m²"
    : `↟ ${fmtNum(cpl.pv_cap_pct, 0)}%`;
  const bessCapPct = Number(cpl.bess_cold_pct ?? 100);
  const bessColdTag = bessCapPct < 99 ? ` · ❄${fmtNum(bessCapPct, 0)}%` : "";

  setWbSvgNode("wbNodeDcWind", {
    cur: `${fmtNum(ctx.dcWindCur, 1)} kW`,
    tgt: `◎ ${fmtNum(ctx.dcWindTgt, 1)}  ·  ↟ ${fmtNum(ctx.m.dcWindMaxAvailableKw, 1)}`,
    deltaKw: ctx.dcWindTgt - ctx.dcWindCur,
    meta: windMetaSuffix
  });
  setEfFlow("wbFlowDcWind", ctx.dcWindCur, { refKw: 80 });

  setWbSvgNode("wbNodeDcPv", {
    cur: `${fmtNum(ctx.dcPvCur, 1)} kW`,
    tgt: `◎ ${fmtNum(ctx.dcPvTgt, 1)}  ·  ↟ ${fmtNum(ctx.m.dcPvMaxAvailableKw, 1)}`,
    deltaKw: ctx.dcPvTgt - ctx.dcPvCur,
    meta: pvMetaSuffix
  });
  setEfFlow("wbFlowDcPv", ctx.dcPvCur, { refKw: 80 });

  setWbSvgNode("wbNodeDcStorage", {
    cur: `${fmtNum(ctx.dcStCur, 1)} kW`,
    tgt: `◎ ${fmtNum(ctx.dcStTgt, 1)} kW`,
    deltaKw: ctx.dcStTgt - ctx.dcStCur,
    meta: `SOC ${fmtPct(ctx.m.dcStorageSoc, 0)}${bessColdTag}`,
    ringPct: pctRatio(ctx.m.dcStorageSoc)
  });
  setEfFlow("wbFlowDcStorage", ctx.dcStCur, { reverse: ctx.dcStCur < 0, refKw: 120 });

  setWbSvgNode("wbNodeDcLoad", {
    cur: `${fmtNum(ctx.dcLoadKw, 1)} kW`,
    tgt: dcLoadPct
  });
  setEfFlow("wbFlowDcLoad", ctx.dcLoadKw, { refKw: 150 });

  setWbSvgNode("wbNodeAcdc", {
    cur: `${fmtNum(ctx.acdcCur, 1)} kW`,
    tgt: `◎ ${fmtNum(ctx.acdcTgt, 1)} kW`,
    deltaKw: ctx.acdcTgt - ctx.acdcCur,
    dir: ctx.acdcCur > 0.2 ? "DC━━▶AC" : ctx.acdcCur < -0.2 ? "DC◀━━AC" : "◀━▶"
  });
  setEfFlow("wbFlowAcdcL", ctx.acdcCur, { reverse: ctx.acdcCur < 0, refKw: 160 });
  setEfFlow("wbFlowAcdcR", ctx.acdcCur, { reverse: ctx.acdcCur < 0, refKw: 160 });

  setWbSvgNode("wbNodeElz", {
    cur: `${fmtNum(ctx.elzCur, 1)} kW`,
    tgt: `${fmtNum(ctx.m.electrolyzerFlowCurrentNm3h, 1)} Nm³/h`
  });
  setEfFlow("wbFlowElzIn", ctx.elzCur, { refKw: 60 });
  setEfFlow("wbFlowElzH2", ctx.elzCur, { refKw: 60 });

  setWbSvgNode("wbNodeH2Tank", {
    cur: fmtPct(ctx.m.hydrogenStorageSoc, 0),
    tgt: `${fmtNum(ctx.h2Press, 2)} MPa`,
    ringPct: ctx.h2Soc
  });

  setWbSvgNode("wbNodeFc", {
    cur: `${fmtNum(ctx.fcCur, 1)} kW`,
    tgt: `${fmtNum(ctx.m.fuelCellFlowCurrentNm3h, 1)} Nm³/h`
  });
  setEfFlow("wbFlowFcH2", ctx.fcCur, { refKw: 60 });
  setEfFlow("wbFlowFcOut", ctx.fcCur, { refKw: 60 });

  setWbSvgNode("wbNodeAcWind", {
    cur: `${fmtNum(ctx.acWindCur, 1)} kW`,
    tgt: `◎ ${fmtNum(ctx.acWindTgt, 1)}  ·  ↟ ${fmtNum(ctx.m.acWindMaxAvailableKw, 1)}`,
    deltaKw: ctx.acWindTgt - ctx.acWindCur,
    meta: windMetaSuffix
  });
  setEfFlow("wbFlowAcWind", ctx.acWindCur, { refKw: 60 });

  setWbSvgNode("wbNodeAcPv", {
    cur: `${fmtNum(ctx.acPvCur, 1)} kW`,
    tgt: `◎ ${fmtNum(ctx.acPvTgt, 1)}  ·  ↟ ${fmtNum(ctx.m.acPvMaxAvailableKw, 1)}`,
    deltaKw: ctx.acPvTgt - ctx.acPvCur,
    meta: pvMetaSuffix
  });
  setEfFlow("wbFlowAcPv", ctx.acPvCur, { refKw: 60 });

  setWbSvgNode("wbNodeAcStorage", {
    cur: `${fmtNum(ctx.acStCur, 1)} kW`,
    tgt: `◎ ${fmtNum(ctx.acStTgt, 1)} kW`,
    deltaKw: ctx.acStTgt - ctx.acStCur,
    meta: `SOC ${fmtPct(ctx.m.acStorageSoc, 0)}${bessColdTag}`,
    ringPct: pctRatio(ctx.m.acStorageSoc)
  });
  setEfFlow("wbFlowAcStorage", ctx.acStCur, { reverse: ctx.acStCur < 0, refKw: 150 });

  setWbSvgNode("wbNodeDiesel", {
    cur: `${fmtNum(ctx.dieselCur, 1)} kW`,
    tgt: `◎ ${fmtNum(ctx.dieselTgt, 1)} kW`,
    deltaKw: ctx.dieselTgt - ctx.dieselCur,
    meta: `↧ ${fmtNum(ctx.dieselMin, 0)}`
  });
  setEfFlow("wbFlowDiesel", ctx.dieselCur, { refKw: 260 });

  setWbSvgNode("wbNodeAcLoad", {
    cur: `${fmtNum(ctx.acLoadKw, 1)} kW`,
    tgt: acLoadPct
  });
  setEfFlow("wbFlowAcLoad", ctx.acLoadKw, { refKw: 300 });
}

function renderWallboardCommandRank(rows) {
  const body = document.getElementById("wbCmdRankBody");
  const meta = document.getElementById("wbCmdRankMeta");
  if (!body) return;
  const changedCnt = rows.filter((r) => r.changed).length;
  if (meta) meta.textContent = `${changedCnt} / ${rows.length}`;

  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;">—</td></tr>`;
    return;
  }

  const sorted = [...rows].sort((a, b) => {
    const ca = a.changed ? 1 : 0;
    const cb = b.changed ? 1 : 0;
    if (ca !== cb) return cb - ca;
    return Math.abs(Number(b.delta_kw || 0)) - Math.abs(Number(a.delta_kw || 0));
  });

  const stateMap = {
    closed: { cls: "badge-ok", txt: `◉ ${t("devcfg.stateClosed")}` },
    track: { cls: "badge-warn", txt: `∿ ${t("devcfg.stateTrack")}` },
    open: { cls: "badge-neutral", txt: `○ ${t("devcfg.stateOpen")}` }
  };

  body.innerHTML = sorted.map((r) => {
    const st = stateMap[r.device_control_state || "closed"] || stateMap.closed;
    const ack = cmdStatusMeta(r.dispatch_status);
    const d = Number(r.delta_kw || 0);
    const dCls = d > 0.05 ? "val-pos" : d < -0.05 ? "val-neg" : "muted";
    const dTxt = `${d >= 0 ? "+" : ""}${fmtNum(d, 2)}`;
    const tgtCell = typeof _renderEnvelopeTargetCell === "function"
      ? _renderEnvelopeTargetCell(r, "")
      : `<td class="num">${fmtNum(r.target_kw, 2)}</td>`;
    return `
      <tr class="${r.changed ? "" : "muted"}">
        <td>
          <button type="button" class="link-dev-btn" data-wb-audit-type="${escapeHtml(r.dev_type)}" data-wb-audit-name="${escapeHtml(r.dev_name)}" data-wb-audit-set="${escapeHtml(r.set_type || "p_set")}">
            ${escapeHtml(r.dev_name)}
          </button>
        </td>
        <td><span class="badge ${st.cls}">${escapeHtml(st.txt)}</span></td>
        <td class="num">${fmtNum(r.current_kw, 2)}</td>
        ${tgtCell}
        <td class="num ${dCls}">${dTxt}</td>
        <td><span class="badge ${ack.cls}">${escapeHtml(ack.label)}</span></td>
      </tr>`;
  }).join("");

  body.querySelectorAll("button[data-wb-audit-name]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (typeof jumpToDeviceTrackingAudit === "function") {
        jumpToDeviceTrackingAudit(btn.dataset.wbAuditType, btn.dataset.wbAuditName, btn.dataset.wbAuditSet);
      }
    });
  });
}

function renderWallboardDecisionStream() {
  const host = document.getElementById("wbDecisionStream");
  const timeEl = document.getElementById("wbLastCalcTime");
  if (!host) return;
  const snap = AGCState.snapshot;
  const cpl = (snap && snap.antarctic_coupling) || (AGCState.control && AGCState.control.antarctic_coupling) || null;
  const latest = (snap && snap.latest_strategy) || (AGCState.control && AGCState.control.latest) || null;
  if (timeEl) {
    const rawTs = (latest && latest.recorded_at) || (AGCState.control && AGCState.control.last_calculated_at) || "";
    timeEl.textContent = rawTs ? rawTs.replace("T", " ").slice(11, 19) : "--";
  }

  let polarBanner = "";
  if (cpl) {
    const wMeta = windRegimeMeta(cpl.wind_regime);
    const lMeta = lightRegimeMeta(cpl.polar_light_regime);
    const icingPct = Math.round(Number(cpl.icing_ratio || 0) * 100);
    const bessCap = Number(cpl.bess_cold_pct ?? 100);
    const isExtreme = cpl.wind_regime === "katabatic_cutout" || cpl.wind_regime === "calm_cutin" || bessCap < 90 || icingPct >= 15;
    polarBanner = `<div class="wb-stream-item ${isExtreme ? "is-warn" : "is-ok"}">❄ ${wMeta.txt} (${fmtNum(cpl.wind_speed, 1)}m/s) · ${lMeta.txt} (${fmtNum(cpl.solar_irradiance, 0)}W/m²) · ❄${fmtNum(cpl.wind_chill_c, 1)}°C · ◈${fmtNum(bessCap, 0)}%${icingPct > 0 ? ` · 🧊-${icingPct}%` : ""}${Math.abs(Number(cpl.thermal_delta_kw || 0)) > 0.1 ? ` · 🔥${cpl.thermal_delta_kw >= 0 ? "+" : ""}${fmtNum(cpl.thermal_delta_kw, 1)}kW` : ""}</div>`;
  }

  const lines = (latest && latest.summary_lines) || (latest && latest.decision_detail) || [];
  const summaryHtml = lines.map((ln) => {
    const isWarn = ln.includes("偏低") || ln.includes("越限") || ln.includes("闭锁") || ln.includes("异常");
    const isOk = ln.includes("新能源") || ln.includes("消纳");
    const cls = isWarn ? "is-warn" : (isOk ? "is-ok" : "");
    return `<div class="wb-stream-item ${cls}">${escapeHtml(ln)}</div>`;
  }).join("");

  const logsHtml = (wbLogsBriefCache || []).slice(0, 3).map((it) => {
    const ts = String(it.logged_at || "").replace("T", " ").slice(11, 19);
    return `
      <div class="wb-stream-item is-log">
        <span class="ellipsis" style="max-width:none;flex:1;" title="${escapeHtml(it.summary || "")}">${escapeHtml(it.subject || it.event_type || "")}: ${escapeHtml(it.summary || "")}</span>
        <small class="mono">${escapeHtml(ts)}</small>
      </div>`;
  }).join("");

  host.innerHTML = (polarBanner + summaryHtml + logsHtml) || `<div class="muted">—</div>`;
}

function getWallboardSeriesDefs() {
  const pal = chartThemePalette();
  return [
    { key: "load_kw", label: t("overview.load"), color: pal.accent, axis: "left", unit: "kW" },
    { key: "renewable_current_kw", label: t("overview.renewable"), color: pal.ok, axis: "left", unit: "kW" },
    { key: "renewable_target_kw", label: `${t("overview.renewable")}(${t("lbl.target")})`, color: pal.ok, dashed: true, axis: "left", unit: "kW" },
    { key: "diesel_current_kw", label: t("cat.diesel"), color: pal.warn, axis: "left", unit: "kW" },
    { key: "diesel_target_kw", label: `${t("cat.diesel")}(${t("lbl.target")})`, color: pal.warn, dashed: true, axis: "left", unit: "kW" },
    { key: "storage_soc", label: "SOC", color: pal.colors[5] || "#22D3EE", axis: "right", isRatio: true, unit: "%" }
  ];
}

function renderWallboardTrendChart() {
  const canvas = document.getElementById("wbTrendCanvas");
  const legEl = document.getElementById("wbTrendLegend");
  const tipEl = document.getElementById("wbTrendTooltip");
  if (!canvas) return;

  const defs = getWallboardSeriesDefs();
  if (legEl) {
    legEl.innerHTML = defs.map((d) => `
      <span class="wb-leg-item">
        <span class="wb-leg-line ${d.dashed ? "is-dashed" : ""}" style="background:${d.color};color:${d.color}"></span>
        <span>${escapeHtml(d.label)}</span>
      </span>`).join("");
  }

  const pts = (wbTrendCache && wbTrendCache.points) || (AGCState.trendData && AGCState.trendData.points) || [];
  const labels = pts.map((p) => String(p.sample_timestamp || p.recorded_at || "").slice(-8));
  const series = defs.map((d) => ({
    label: d.label,
    color: d.color,
    dashed: Boolean(d.dashed),
    axis: d.axis,
    values: pts.map((p) => {
      const v = p[d.key];
      if (v === null || v === undefined) return null;
      return d.isRatio ? pctRatio(v) : Number(v);
    })
  }));

  drawLineChart(canvas, {
    labels,
    series,
    leftUnit: "kW",
    rightUnit: "%",
    emptyText: "—"
  });

  if (wbHoverRatio !== null && pts.length > 0 && tipEl) {
    const rect = (canvas.parentElement || canvas).getBoundingClientRect();
    const width = Math.max(260, Math.floor(rect.width || 600));
    const height = Math.max(120, Math.floor(rect.height || 220));
    const padL = 56;
    const padR = 52;
    const padT = 16;
    const padB = 26;
    const plotW = Math.max(40, width - padL - padR);
    const idx = Math.max(0, Math.min(pts.length - 1, Math.round(wbHoverRatio * (pts.length - 1))));
    const x = pts.length === 1 ? padL + plotW / 2 : padL + (idx / (pts.length - 1)) * plotW;

    const pal = chartThemePalette();
    const ctx = canvas.getContext("2d");
    ctx.save();
    ctx.strokeStyle = pal.crosshair;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, height - padB);
    ctx.stroke();
    ctx.restore();

    tipEl.hidden = false;
    const linesHtml = defs.map((d, i) => {
      const val = series[i].values[idx];
      return `<div style="display:flex;justify-content:space-between;gap:10px;"><span style="color:${d.color}">${escapeHtml(d.label)}</span><strong>${fmtNum(val, 1)} ${d.unit}</strong></div>`;
    }).join("");
    tipEl.innerHTML = `<div style="margin-bottom:2px;color:var(--text-secondary)">${escapeHtml(labels[idx] || "--")}</div>${linesHtml}`;
    const tipLeft = x > width * 0.62 ? Math.max(8, x - 178) : Math.min(width - 178, x + 10);
    tipEl.style.left = `${Math.round(tipLeft)}px`;
  } else if (tipEl) {
    tipEl.hidden = true;
  }
}

function bindWallboardEvents() {
  // 顶栏全屏大屏切换按钮
  document.getElementById("topbarWallboardBtn")?.addEventListener("click", () => {
    setWallboardImmersive(!isWallboardImmersive(), true);
  });

  document.addEventListener("fullscreenchange", () => {
    const fsActive = Boolean(document.fullscreenElement);
    document.documentElement.dataset.wallboardImmersive = fsActive ? "true" : "false";
    if (!fsActive && typeof setSidebarOpen === "function" && typeof getSidebarMode === "function") {
      if (getSidebarMode() === "embed" && localStorage.getItem("agcSidebarEmbedHidden") !== "1") {
        setSidebarOpen(true, { persistEmbed: false });
      }
    }
    syncWallboardShellControls();
    setTimeout(redrawActiveCharts, 100);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && isWallboardImmersive() && !document.querySelector("dialog[open]")) {
      setWallboardImmersive(false, false);
    }
  });

  // 南极极端气象预设切换、参数微调与一键注入/复位
  const presetSel = document.getElementById("wbEnvPresetSelect");
  presetSel?.addEventListener("change", () => {
    const p = presetSel.value;
    if (p === "scada_live") {
      resetAntarcticEnvOverride();
      return;
    }
    const tpl = WB_ANTARCTIC_PRESETS[p];
    if (tpl) {
      document.getElementById("wbInpWind").value = String(tpl.wind_speed);
      document.getElementById("wbInpSolar").value = String(tpl.solar_irradiance);
      document.getElementById("wbInpTemp").value = String(tpl.air_temperature);
      document.getElementById("wbInpIcing").value = String(tpl.icing_pct);
      document.getElementById("wbInpThermal").value = String(tpl.thermal_pct);
      applyAntarcticEnvOverride(p);
    } else {
      wbEnvFormDirty = true;
    }
  });

  ["wbInpWind", "wbInpSolar", "wbInpTemp", "wbInpIcing", "wbInpThermal"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", () => {
      wbEnvFormDirty = true;
      if (presetSel && presetSel.value !== "custom") presetSel.value = "custom";
    });
    el.addEventListener("change", () => {
      wbEnvFormDirty = true;
      if (presetSel && presetSel.value !== "custom") presetSel.value = "custom";
    });
    el.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        applyAntarcticEnvOverride("custom");
      }
    });
  });

  document.getElementById("wbApplyEnvBtn")?.addEventListener("click", () => {
    applyAntarcticEnvOverride();
  });
  document.getElementById("wbEnvResetBtn")?.addEventListener("click", () => {
    resetAntarcticEnvOverride();
  });

  // 大屏中栏快捷操作按钮
  document.getElementById("wbQuickCalcBtn")?.addEventListener("click", async () => {
    try {
      const res = await apiPost("/api/agc/control", { action: "calculate_once" });
      AGCState.control = res.control;
      await Promise.all([refreshSnapshot(), refreshWallboardExtraData()]);
      renderWallboardPage();
      showToast(`单次计算完成（周期 #${res.control.cycle}）`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });
  document.getElementById("wbOpenAgcBtn")?.addEventListener("click", async () => {
    switchPage("agc-control");
  });
  document.getElementById("wbOpenDiagBtn")?.addEventListener("click", async () => {
    switchPage("diagram");
  });

  // 态势 SVG 节点点击下钻
  document.querySelectorAll("#wbCockpitSvg .wb-svg-node").forEach((node) => {
    node.addEventListener("click", async () => {
      const tab = node.dataset.wbTab;
      const diagType = node.dataset.wbDiag;
      if (tab) {
        jumpToAgcStrategyTab(tab);
      } else if (diagType) {
        await switchPage("diagram");
      }
    });
  });

  // 大屏趋势时间窗切换
  document.getElementById("wbTrendWindowSelect")?.addEventListener("change", (ev) => {
    wbTrendWindow = Number(ev.target.value) || 60;
    refreshWallboardExtraData();
  });

  document.querySelectorAll("#wbTrendWindowGroup button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#wbTrendWindowGroup button").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      wbTrendWindow = Number(btn.dataset.wbWin) || 60;
      refreshWallboardExtraData();
    });
  });

  // 大屏趋势 Canvas 十字准星悬停交互
  const canvas = document.getElementById("wbTrendCanvas");
  if (canvas) {
    canvas.addEventListener("mousemove", (ev) => {
      const rect = canvas.getBoundingClientRect();
      const padL = 56;
      const padR = 52;
      const plotW = Math.max(40, rect.width - padL - padR);
      const relX = ev.clientX - rect.left - padL;
      if (relX < -4 || relX > plotW + 4) {
        if (wbHoverRatio !== null) {
          wbHoverRatio = null;
          renderWallboardTrendChart();
        }
        return;
      }
      wbHoverRatio = Math.max(0, Math.min(1, relX / plotW));
      renderWallboardTrendChart();
    });
    canvas.addEventListener("mouseleave", () => {
      wbHoverRatio = null;
      renderWallboardTrendChart();
    });
  }
}
