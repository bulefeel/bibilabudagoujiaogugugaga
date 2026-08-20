(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const cookie = (name) => document.cookie.split("; ").find(x => x.startsWith(`${name}=`))?.split("=").slice(1).join("=") || "";
  const csrf = () => decodeURIComponent(cookie("ziniao_csrf"));
  const toast = (message, error = false) => {
    const region = $("#toast-region"); if (!region) return;
    const node = document.createElement("div"); node.className = `toast${error ? " error" : ""}`; node.textContent = message;
    region.append(node); setTimeout(() => node.remove(), 4500);
  };
  async function api(url, options = {}) {
    const headers = {"Content-Type":"application/json", ...options.headers};
    if ((options.method || "GET") !== "GET") headers["X-CSRF-Token"] = csrf();
    const response = await fetch(url, {...options, headers});
    let data = {}; try { data = await response.json(); } catch (_) {}
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map(x => x.msg).join("；") : (data.detail || `请求失败 (${response.status})`);
      if (response.status === 401) location.href = "/login";
      const error = new Error(detail);
      error.statusCode = response.status;
      throw error;
    }
    return data;
  }
  async function apiResult(url, options = {}) {
    const headers = {"Content-Type":"application/json", ...options.headers};
    if ((options.method || "GET") !== "GET") headers["X-CSRF-Token"] = csrf();
    const response = await fetch(url, {...options, headers});
    let data = {}; try { data = await response.json(); } catch (_) {}
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map(x => x.msg).join("；") : (data.detail || `请求失败 (${response.status})`);
      if (response.status === 401) location.href = "/login";
      const error = new Error(detail);
      error.statusCode = response.status;
      throw error;
    }
    return {data, statusCode: response.status};
  }
  const storeSetupProbePath = (storeId, probeId) => `/api/stores/${encodeURIComponent(String(storeId))}/store-setup-probes/${encodeURIComponent(String(probeId))}`;
  const validPositiveId = value => Number.isSafeInteger(Number(value)) && Number(value) > 0;
  const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  function storeSetupUi(form) {
    return {
      panel: $("[data-store-setup-auth-panel]", form),
      message: $("[data-store-setup-auth-message]", form),
      identity: $("[data-store-setup-identity-result]", form),
      result: $("[data-store-setup-result]", form),
      error: $("[data-form-error]", form),
      detect: $('[data-action="detect-store-setup"]', form),
      resume: $('[data-action="continue-store-setup"]', form),
      cancel: $('[data-action="cancel-store-setup"]', form),
      reset: $('[data-action="reset-store-setup"]', form),
    };
  }
  const storeSetupTerminalStatuses = new Set([
    "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "UNAVAILABLE", "NEEDS_REVIEW", "SKIPPED",
  ]);
  const storeSetupTerminalSiteStatuses = new Set([
    "SUCCEEDED", "FAILED", "CANCELLED", "UNAVAILABLE", "NEEDS_REVIEW", "SKIPPED",
  ]);
  function clearStoreSetupProbe(form) {
    delete form.dataset.storeSetupProbeId;
    delete form.dataset.storeSetupProbeStoreId;
    delete form.dataset.storeSetupProbeStatus;
    const dialog = form.closest("dialog");
    if (dialog) {
      delete dialog.dataset.storeSetupProbeId;
      delete dialog.dataset.storeSetupProbeStoreId;
    }
    const ui = storeSetupUi(form);
    if (ui.panel) ui.panel.hidden = true;
    if (ui.resume) { ui.resume.disabled = false; ui.resume.textContent = "再次尝试自动登录并继续"; }
    if (ui.cancel) ui.cancel.disabled = false;
    if (ui.detect) { ui.detect.disabled = false; ui.detect.textContent = "自动获取卖家ID并核验站点"; }
    if (ui.reset) ui.reset.disabled = false;
  }
  function setMarketplaceRowStatus(row, status, message = "") {
    const badge = $("[data-marketplace-status]", row);
    const detail = $("[data-marketplace-account-display]", row);
    const normalized = String(status || "PENDING").toUpperCase();
    // Setup only compares the domain and the seller identity, so a site is
    // either verified or not.  It never reads balances, hence no "no payable
    // funds" style outcomes here — those belong to a disbursement run.
    const labels = {
      PENDING: "待检测",
      CHECKING: "检测中…",
      SUCCEEDED: "✓ 核验通过",
      FAILED: "核验未通过",
      CANCELLED: "已取消",
      WAITING_AUTH: "需验证",
      SKIPPED: "已跳过",
    };
    row.dataset.marketplaceProbeStatus = normalized;
    if (badge) {
      badge.className = `marketplace-status ${normalized.toLowerCase()}`;
      badge.textContent = labels[normalized] || normalized;
    }
    if (detail && message) detail.textContent = message;
  }
  function renderStoreSetupIdentity(form, data) {
    const identity = data && typeof data.identity === "object" && data.identity ? data.identity : null;
    if (!identity) return false;
    const ui = storeSetupUi(form);
    const status = String(identity.status || "PENDING").toUpperCase();
    const sellerId = String(identity.seller_id || "").trim();
    const message = String(identity.message || "").trim();
    const source = String(identity.source || "").trim();
    const marketplaceCode = String(identity.marketplace_code || "").trim().toUpperCase();
    // Only a SUCCEEDED identity is authoritative.  A NEEDS_REVIEW payload
    // contains the newly observed *candidate* seller as evidence; copying that
    // candidate into the editor would make a later Save overwrite the existing
    // safety baseline.  The backend also returns the persisted confirmation and
    // enable flags so this still-open dialog cannot accidentally undo automatic
    // setup with stale checkbox values.
    if (status === "SUCCEEDED" && sellerId) {
      const input = $("[name=expected_seller_id]", form);
      if (input) input.value = sellerId;
      const confirmed = $("[name=identity_confirmed]", form);
      const enabled = $("[name=enabled]", form);
      if (confirmed && typeof identity.identity_confirmed === "boolean") {
        confirmed.checked = identity.identity_confirmed;
      }
      if (enabled && typeof identity.store_enabled === "boolean") enabled.checked = identity.store_enabled;
      form.dataset.persistedSellerId = sellerId;
      const dialog = form.closest("dialog");
      if (dialog) dialog.dataset.storeSetupChanged = "true";
    }
    const location = marketplaceCode ? ` · ${marketplaceCode}` : "";
    const provenance = source ? ` · ${source}` : "";
    if (status === "SUCCEEDED" && sellerId) {
      const state = String(identity.persistence_state || "").toUpperCase();
      const enabledCopy = identity.store_enabled === false ? "；店铺保持手工关闭状态" : "";
      ui.identity.textContent = message || `已自动读取并确认卖家身份：${sellerId}${location}${provenance}${enabledCopy}`;
      if (message && !message.includes(sellerId)) {
        ui.identity.textContent = `${message}：${sellerId}${location}${provenance}${enabledCopy}`;
      }
      form.dataset.identityPersistenceState = state;
      return true;
    }
    if (status === "NEEDS_REVIEW") {
      ui.identity.textContent = message || "检测到的卖家身份与原建档不一致；原建档未被覆盖，请人工复核或重置后重试。";
      return false;
    }
    if (status === "WAITING_AUTH") {
      ui.identity.textContent = message || "卖家身份读取正在等待当前紫鸟窗口完成登录验证。";
      return false;
    }
    if (["FAILED", "CANCELLED", "SKIPPED"].includes(status)) {
      ui.identity.textContent = message || `卖家身份读取状态：${status}`;
      return false;
    }
    ui.identity.textContent = message || "正在从当前紫鸟店铺读取卖家身份…";
    return false;
  }
  function renderStoreSetupResults(form, data) {
    const returnedCodes = new Set();
    const results = Array.isArray(data.marketplaces) ? data.marketplaces : [];
    for (const site of results) {
      const code = String(site.code || "").toUpperCase();
      const row = $(`[data-store-marketplace="${code}"]`, form);
      if (!row) continue;
      returnedCodes.add(code);
      const status = String(site.status || "PENDING").toUpperCase();
      const message = String(site.message || "").trim();
      // A probe result describes this site's reachability, not the operator's
      // selection. Never silently uncheck a requested marketplace.
      const detail = status === "SUCCEEDED"
        ? (message || "域名与卖家身份核验通过")
        : status === "FAILED"
          ? `${message || "本次核验未通过"}；站点选择保持不变，三种模式仍可保存和运行`
          : status === "CANCELLED"
            ? (message || "本次核验已取消，可稍后重新检测")
            : status === "SKIPPED"
              ? (message || "因卖家身份未通过校验，本次站点核验已跳过")
              : (message || (status === "CHECKING" ? "正在核验该站点" : "待核验"));
      setMarketplaceRowStatus(row, status, detail);
    }
    if (["CHECKING", "WAITING_AUTH"].includes(String(data.status || "").toUpperCase())) {
      $$('[data-store-marketplace] [data-marketplace-enabled]:checked', form).forEach(input => {
        const row = input.closest("[data-store-marketplace]");
        if (!returnedCodes.has(row.dataset.storeMarketplace)) {
          setMarketplaceRowStatus(row, "CHECKING", "排队等待检查");
        }
      });
    }
    return results.filter(
      site => String(site.status || "").toUpperCase() === "SUCCEEDED"
    ).length;
  }
  function settleStoreSetupTerminalRows(form, data) {
    const overall = String(data.status || "FAILED").toUpperCase();
    const returnedCodes = new Set((Array.isArray(data.marketplaces) ? data.marketplaces : [])
      .map(site => String(site.code || "").toUpperCase())
      .filter(Boolean));
    const fallback = overall === "PARTIAL" || overall === "UNAVAILABLE"
      ? ["FAILED", "本次检测已结束，但该站点没有返回核验结果；请稍后重试"]
      : overall === "CANCELLED"
        ? ["CANCELLED", data.message || "本次自动检测已取消"]
        : overall === "SKIPPED"
          ? ["SKIPPED", data.message || "本次统一建档已跳过"]
        : overall === "NEEDS_REVIEW"
          ? ["FAILED", data.message || "卖家身份需要人工复核，站点核验未完成"]
          : ["FAILED", data.message || "检测已结束，但该站点没有返回完整结果；请稍后重试"];
    $$('[data-store-marketplace]', form).forEach(row => {
      const code = String(row.dataset.storeMarketplace || "").toUpperCase();
      const current = String(row.dataset.marketplaceProbeStatus || "PENDING").toUpperCase();
      const enabled = $("[data-marketplace-enabled]", row)?.checked;
      const belongedToAttempt = returnedCodes.has(code)
        || (enabled && ["CHECKING", "WAITING_AUTH", "PENDING"].includes(current));
      if (!belongedToAttempt || storeSetupTerminalSiteStatuses.has(current)) return;
      setMarketplaceRowStatus(row, fallback[0], fallback[1]);
    });
  }
  function failStoreSetup(form, message) {
    const ui = storeSetupUi(form);
    const detail = String(message || "自动检测请求失败，请稍后重试");
    settleStoreSetupTerminalRows(form, {status:"FAILED", marketplaces:[], message:detail});
    ui.error.textContent = detail;
    ui.result.textContent = "自动建档已停止；仍显示“检测中”的站点已改为核验失败，已保存的建档不会被清除。";
    clearStoreSetupProbe(form);
  }
  function rememberStoreSetupProbe(form, data) {
    const storeId = Number(data.store_id), probeId = String(data.probe_id || "").trim();
    if (!validPositiveId(storeId) || !probeId) throw new Error("服务端返回的批量检测标识无效，本次检测已停止。");
    form.dataset.storeSetupProbeId = probeId;
    form.dataset.storeSetupProbeStoreId = String(storeId);
    form.dataset.storeSetupProbeStatus = String(data.status || "CHECKING").toUpperCase();
    const dialog = form.closest("dialog");
    if (dialog) {
      dialog.dataset.storeSetupProbeId = probeId;
      dialog.dataset.storeSetupProbeStoreId = String(storeId);
    }
    return {storeId, probeId};
  }
  function showStoreSetupAuth(form, data) {
    rememberStoreSetupProbe(form, data);
    renderStoreSetupIdentity(form, data);
    renderStoreSetupResults(form, data);
    const ui = storeSetupUi(form);
    ui.panel.hidden = false;
    ui.message.textContent = data.message || "自动登录已经尝试邮箱 Continue、紫鸟托管 Passkey 和已填好的 6 位 OTP，但页面仍停在验证步骤。可先再次尝试自动登录；普通密码、其他 Passkey、CAPTCHA 或未填好的验证码需要在当前紫鸟窗口处理。";
    ui.result.textContent = "统一建档只在自动登录未能通过时暂停；已读取结果会保留，再次继续仍复用当前紫鸟窗口。";
    ui.detect.disabled = true;
    ui.detect.textContent = "自动登录未通过，等待处理…";
    if (ui.resume) { ui.resume.disabled = false; ui.resume.textContent = "再次尝试自动登录并继续"; }
    if (ui.cancel) ui.cancel.disabled = false;
    if (ui.reset) ui.reset.disabled = true;
  }
  function hideStoreSetupAuth(form) {
    const ui = storeSetupUi(form);
    if (ui.panel) ui.panel.hidden = true;
    if (ui.resume) ui.resume.disabled = true;
    if (ui.cancel) ui.cancel.disabled = true;
  }
  function finishStoreSetup(form, data) {
    const identitySaved = renderStoreSetupIdentity(form, data);
    const savedCount = renderStoreSetupResults(form, data);
    const status = String(data.status || "FAILED").toUpperCase();
    // A browser/session-level exception can end the whole probe before the
    // backend updates the active site's CHECKING row. Never leave a terminal
    // probe looking live in the editor.
    settleStoreSetupTerminalRows(form, data);
    const ui = storeSetupUi(form);
    if (identitySaved || savedCount) form.closest("dialog").dataset.storeSetupChanged = "true";
    if (status === "SUCCEEDED") {
      ui.result.textContent = `统一建档完成：卖家身份已自动绑定并确认，${savedCount} 个站点核验通过。`;
      toast(`统一建档完成：身份已确认，${savedCount} 个站点核验通过`);
    } else if (status === "PARTIAL") {
      ui.result.textContent = `统一建档部分完成：卖家身份已自动确认，${savedCount} 个站点核验通过；其余站点结果请查看上方状态。`;
      toast(`身份已确认；${savedCount} 个站点核验通过`);
    } else if (status === "NEEDS_REVIEW") {
      ui.result.textContent = data.message || "检测到卖家身份与原建档不一致；原值不会自动覆盖，请人工复核。";
    } else if (status === "CANCELLED") {
      ui.result.textContent = data.message || "本次统一建档已取消，可稍后重新检测。";
    } else if (identitySaved) {
      ui.result.textContent = `卖家身份已自动绑定并确认；本次没有站点通过核验，请查看各站点状态，稍后可重新检测。`;
      toast("卖家身份已自动建档；站点核验仍待完成");
    } else {
      ui.result.textContent = data.message || "本次统一建档没有获得可保存结果，可稍后重试。";
    }
    clearStoreSetupProbe(form);
  }
  async function pollStoreSetupProbe(form, storeId, probeId) {
    // A visible three-site pass can take several minutes. Keep observing the
    // original probe so a browser that closes normally is reflected as a
    // terminal result instead of leaving the page frozen on “检测中”.
    for (let attempt = 0; attempt < 180; attempt += 1) {
      await sleep(2000);
      if (form.dataset.storeSetupProbeId !== probeId) return;
      const {data, statusCode} = await apiResult(storeSetupProbePath(storeId, probeId));
      const status = String(data.status || "").toUpperCase();
      renderStoreSetupIdentity(form, data);
      renderStoreSetupResults(form, data);
      if (storeSetupTerminalStatuses.has(status)) { finishStoreSetup(form, data); return; }
      if (status === "WAITING_AUTH") { showStoreSetupAuth(form, data); return; }
      if (status !== "CHECKING") throw new Error(data.message || `自动建档状态异常：${status || "未知"}`);
      form.dataset.storeSetupProbeStatus = "CHECKING";
    }
    const ui = storeSetupUi(form);
    ui.result.textContent = "检测仍在进行。为避免高频请求，页面已暂停查询；稍后点击“继续自动检测”即可查看原任务，不会重新打开店铺。";
    // CHECKING is not an authentication failure, so keep the authentication
    // controls hidden.  The primary setup button safely reattaches to the same
    // backend probe and resumes polling without opening a second store.
    ui.panel.hidden = true;
    ui.detect.disabled = false;
    ui.detect.textContent = "继续查询当前建档";
    if (ui.reset) ui.reset.disabled = true;
  }
  async function handleStoreSetupResponse(form, data, statusCode) {
    const status = String(data.status || "").toUpperCase();
    if (storeSetupTerminalStatuses.has(status)) { finishStoreSetup(form, data); return; }
    if (status === "WAITING_AUTH") { showStoreSetupAuth(form, data); return; }
    if (statusCode === 202 && status === "CHECKING") {
      const {storeId, probeId} = rememberStoreSetupProbe(form, data);
      renderStoreSetupIdentity(form, data);
      renderStoreSetupResults(form, data);
      const ui = storeSetupUi(form);
      // A successful continue request has left the authentication state. Hide
      // the stale manual controls before the long poll starts, rather than
      // leaving a clickable "continue" button over an already logged-in page.
      hideStoreSetupAuth(form);
      ui.result.textContent = "正在同一紫鸟窗口读取卖家身份，并依次核验所选站点；全程不点击页面上的任何按钮…";
      await pollStoreSetupProbe(form, storeId, probeId);
      return;
    }
    throw new Error(data.message || `自动建档状态异常：${status || "未知"}`);
  }
  const bulkStoreSetup = {
    active: false,
    queue: [],
    index: 0,
    success: 0,
    siteIncomplete: 0,
    auth: 0,
    failed: 0,
    storeId: null,
    probeId: null,
    probeStatus: null,
    authCountedProbeId: null,
    resumeAvailable: false,
  };
  const bulkSetupStorageKey = "ziniao.storeSetupQueue.v2";
  const bulkSetupSummaryStorageKey = "ziniao.storeSetupCompleted.v1";
  function persistBulkStoreSetup() {
    try {
      if (!bulkStoreSetup.queue.length || bulkStoreSetup.index >= bulkStoreSetup.queue.length) {
        sessionStorage.removeItem(bulkSetupStorageKey);
        return;
      }
      sessionStorage.setItem(bulkSetupStorageKey, JSON.stringify({
        version: 2,
        savedAt: Date.now(),
        queue: bulkStoreSetup.queue,
        index: bulkStoreSetup.index,
        success: bulkStoreSetup.success,
        siteIncomplete: bulkStoreSetup.siteIncomplete,
        auth: bulkStoreSetup.auth,
        failed: bulkStoreSetup.failed,
        storeId: bulkStoreSetup.storeId,
        probeId: bulkStoreSetup.probeId,
        probeStatus: bulkStoreSetup.probeStatus,
        authCountedProbeId: bulkStoreSetup.authCountedProbeId,
      }));
    } catch (_) {}
  }
  function restoreBulkStoreSetup() {
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(bulkSetupStorageKey) || "null"); }
    catch (_) { sessionStorage.removeItem(bulkSetupStorageKey); return; }
    if (!saved || !Array.isArray(saved.queue) || !saved.queue.length) return;
    const queue = saved.queue.map(store => ({
      id: Number(store.id),
      name: String(store.name || `店铺 #${store.id}`),
      marketplaceCodes: Array.isArray(store.marketplaceCodes)
        ? [...new Set(store.marketplaceCodes.map(code => String(code).toUpperCase()).filter(code => ["CA", "UK", "AU"].includes(code)))]
        : ["CA", "UK", "AU"],
      defaulted: !!store.defaulted,
      identityState: String(store.identityState || "MISSING").toUpperCase(),
    })).map(store => ({
      ...store,
      marketplaceCodes: store.marketplaceCodes.length ? store.marketplaceCodes : ["CA", "UK", "AU"],
    })).filter(store => validPositiveId(store.id));
    const index = Number(saved.index || 0);
    if (!queue.length || !Number.isSafeInteger(index) || index < 0 || index >= queue.length) {
      sessionStorage.removeItem(bulkSetupStorageKey);
      return;
    }
    Object.assign(bulkStoreSetup, {
      active: false,
      queue,
      index,
      success: Number(saved.success || 0),
      siteIncomplete: Number(saved.siteIncomplete || 0),
      auth: Number(saved.auth || 0),
      failed: Number(saved.failed || 0),
      storeId: validPositiveId(saved.storeId) ? Number(saved.storeId) : null,
      probeId: String(saved.probeId || "") || null,
      probeStatus: String(saved.probeStatus || "").toUpperCase() || null,
      authCountedProbeId: String(saved.authCountedProbeId || "") || null,
      resumeAvailable: true,
    });
    const ui = bulkSetupUi();
    ui.panel.hidden = false;
    // sessionStorage only proves that this browser tab once saw a live probe;
    // it does not prove that the backend probe is still waiting for auth. A
    // refresh must re-query the server before either auth action is exposed.
    ui.actions.hidden = true;
    if (ui.resume) ui.resume.disabled = true;
    if (ui.skip) ui.skip.disabled = true;
    ui.title.textContent = `发现未完成队列：${queue[index].name}`;
    ui.message.textContent = "点击“继续未完成队列”会从上次位置查询原任务，不会从第一家重复触发。";
    ui.start.disabled = false;
    ui.start.textContent = "继续未完成队列";
    updateBulkSetupCounts();
  }
  function bulkSetupUi() {
    const panel = $("[data-bulk-store-setup-progress]");
    return {
      panel,
      title: $("[data-bulk-store-setup-title]", panel || document),
      message: $("[data-bulk-store-setup-message]", panel || document),
      success: $("[data-bulk-store-setup-success]", panel || document),
      siteIncomplete: $("[data-bulk-store-setup-site-incomplete]", panel || document),
      auth: $("[data-bulk-store-setup-auth]", panel || document),
      failed: $("[data-bulk-store-setup-failed]", panel || document),
      actions: $("[data-bulk-store-setup-actions]", panel || document),
      resume: $('[data-action="continue-bulk-store-setup"]', panel || document),
      skip: $('[data-action="skip-bulk-store-setup"]', panel || document),
      start: $('[data-action="detect-all-store-setups"]'),
    };
  }
  function updateBulkSetupCounts() {
    const ui = bulkSetupUi();
    ui.success.textContent = String(bulkStoreSetup.success);
    if (ui.siteIncomplete) ui.siteIncomplete.textContent = String(bulkStoreSetup.siteIncomplete);
    ui.auth.textContent = String(bulkStoreSetup.auth);
    ui.failed.textContent = String(bulkStoreSetup.failed);
  }
  function identitySetupSucceeded(data) {
    const status = String(data?.identity?.status || "").toUpperCase();
    return ["SUCCEEDED", "MATCHED"].includes(status)
      && String(data?.status || "").toUpperCase() !== "NEEDS_REVIEW";
  }
  function siteSetupNeedsFollowup(data) {
    const sites = Array.isArray(data?.marketplaces) ? data.marketplaces : [];
    return !sites.length || sites.some(site => String(site.status || "").toUpperCase() !== "SUCCEEDED");
  }
  function summarizeBulkStoreResult(data) {
    if (identitySetupSucceeded(data)) {
      bulkStoreSetup.success += 1;
      if (siteSetupNeedsFollowup(data)) bulkStoreSetup.siteIncomplete += 1;
    } else {
      bulkStoreSetup.failed += 1;
    }
    updateBulkSetupCounts();
  }
  function persistBulkSetupCompletionSummary() {
    try {
      sessionStorage.setItem(bulkSetupSummaryStorageKey, JSON.stringify({
        version: 1,
        completedAt: Date.now(),
        total: bulkStoreSetup.queue.length,
        success: bulkStoreSetup.success,
        siteIncomplete: bulkStoreSetup.siteIncomplete,
        auth: bulkStoreSetup.auth,
        failed: bulkStoreSetup.failed,
        announced: false,
      }));
    } catch (_) {}
  }
  function restoreBulkSetupCompletionSummary() {
    let summary;
    try { summary = JSON.parse(sessionStorage.getItem(bulkSetupSummaryStorageKey) || "null"); }
    catch (_) { sessionStorage.removeItem(bulkSetupSummaryStorageKey); return; }
    if (!summary || Number(summary.version) !== 1) return;
    // A stale summary from an old tab should not hide the current page state.
    if (!Number.isFinite(Number(summary.completedAt)) || Date.now() - Number(summary.completedAt) > 12 * 60 * 60 * 1000) {
      sessionStorage.removeItem(bulkSetupSummaryStorageKey);
      return;
    }
    Object.assign(bulkStoreSetup, {
      success: Number(summary.success || 0),
      siteIncomplete: Number(summary.siteIncomplete || 0),
      auth: Number(summary.auth || 0),
      failed: Number(summary.failed || 0),
    });
    const ui = bulkSetupUi();
    ui.panel.hidden = false;
    ui.actions.hidden = true;
    ui.title.textContent = "全部店铺自动建档已完成";
    ui.message.textContent = `卖家身份已自动绑定并确认 ${bulkStoreSetup.success} 家；其中 ${bulkStoreSetup.siteIncomplete} 家有部分站点核验未通过；身份失败、冲突或跳过 ${bulkStoreSetup.failed} 家。下方店铺卡片已刷新为数据库最新状态。`;
    ui.start.disabled = false;
    ui.start.textContent = "一键自动建档全部店铺";
    updateBulkSetupCounts();
    if (!summary.announced) {
      toast("批量建档完成，店铺卡片已刷新");
      summary.announced = true;
      try { sessionStorage.setItem(bulkSetupSummaryStorageKey, JSON.stringify(summary)); } catch (_) {}
    }
  }
  async function pollBulkStoreSetup(store, probeId) {
    // A three-site visible-browser pass can legitimately take several minutes.
    // Keep following the original probe instead of abandoning a live backend
    // task and accidentally starting it again from the first store.
    for (let attempt = 0; attempt < 180; attempt += 1) {
      await sleep(2000);
      if (!bulkStoreSetup.active || bulkStoreSetup.probeId !== probeId) return;
      const {data, statusCode} = await apiResult(storeSetupProbePath(store.id, probeId));
      const outcome = await handleBulkStoreSetupResponse(store, data, statusCode);
      if (outcome !== "CHECKING") return;
    }
    bulkStoreSetup.resumeAvailable = true;
    persistBulkStoreSetup();
    const ui = bulkSetupUi();
    ui.title.textContent = `${store.name} 仍在后台检测`;
    ui.message.textContent = "页面已持续追踪 6 分钟。点击批量按钮会继续查询这个原任务，不会从第一家重新触发。";
    ui.start.disabled = false;
    ui.start.textContent = "继续追踪未完成队列";
  }
  async function handleBulkStoreSetupResponse(store, data, statusCode) {
    const status = String(data.status || "").toUpperCase(), ui = bulkSetupUi();
    if (storeSetupTerminalStatuses.has(status)) {
      bulkStoreSetup.probeStatus = status;
      summarizeBulkStoreResult(data);
      bulkStoreSetup.storeId = null;
      bulkStoreSetup.probeId = null;
      bulkStoreSetup.resumeAvailable = false;
      bulkStoreSetup.index += 1;
      // Commit counters and the advanced queue position together. Persisting
      // the counters first could double-count this store if the page closed in
      // the tiny gap before the index was advanced.
      persistBulkStoreSetup();
      await runNextBulkStoreSetup();
      return "DONE";
    }
    if (status === "WAITING_AUTH") {
      const storeId = Number(data.store_id), probeId = String(data.probe_id || "").trim();
      if (!validPositiveId(storeId) || !probeId) throw new Error("服务端返回的批量验证标识无效。");
      const firstPauseForThisProbe = bulkStoreSetup.authCountedProbeId !== probeId;
      bulkStoreSetup.storeId = storeId;
      bulkStoreSetup.probeId = probeId;
      bulkStoreSetup.probeStatus = "WAITING_AUTH";
      bulkStoreSetup.resumeAvailable = true;
      if (firstPauseForThisProbe) {
        bulkStoreSetup.auth += 1;
        bulkStoreSetup.authCountedProbeId = probeId;
      }
      updateBulkSetupCounts();
      persistBulkStoreSetup();
      ui.title.textContent = `${store.name} 自动登录未通过`;
      const authMessage = data.message || "程序已自动尝试邮箱 Continue、紫鸟托管 Passkey 和已填好的 6 位 OTP，但页面仍停在验证步骤。可再次尝试自动登录；普通密码、其他 Passkey、CAPTCHA 或未填好的验证码需要在当前紫鸟窗口处理。队列不会打开下一家店。";
      ui.message.textContent = `${authMessage} 已成功建档的数据已写入SQLite；下方店铺卡片将在队列结束后统一刷新。`;
      ui.actions.hidden = false;
      if (ui.resume) {
        ui.resume.disabled = false;
        ui.resume.textContent = "再次尝试自动登录并继续";
      }
      if (ui.skip) ui.skip.disabled = false;
      return "WAITING_AUTH";
    }
    if (statusCode === 202 && status === "CHECKING") {
      bulkStoreSetup.storeId = Number(data.store_id || store.id);
      bulkStoreSetup.probeId = String(data.probe_id || bulkStoreSetup.probeId || "");
      bulkStoreSetup.probeStatus = "CHECKING";
      bulkStoreSetup.resumeAvailable = true;
      persistBulkStoreSetup();
      ui.title.textContent = `正在检测 ${store.name}`;
      ui.message.textContent = `第 ${bulkStoreSetup.index + 1} / ${bulkStoreSetup.queue.length} 家；完成后才会打开下一家紫鸟店铺。`;
      ui.actions.hidden = true;
      if (ui.resume) ui.resume.disabled = true;
      if (ui.skip) ui.skip.disabled = true;
      return "CHECKING";
    }
    throw new Error(data.message || `${store.name} 返回未知检测状态：${status || "未知"}`);
  }
  async function runNextBulkStoreSetup() {
    if (!bulkStoreSetup.active) return;
    const ui = bulkSetupUi(), store = bulkStoreSetup.queue[bulkStoreSetup.index];
    ui.actions.hidden = true;
    if (!store) {
      bulkStoreSetup.active = false;
      bulkStoreSetup.probeId = null;
      bulkStoreSetup.probeStatus = null;
      bulkStoreSetup.resumeAvailable = false;
      sessionStorage.removeItem(bulkSetupStorageKey);
      ui.title.textContent = "全部店铺自动建档已完成";
      ui.message.textContent = `卖家身份已自动绑定并确认 ${bulkStoreSetup.success} 家；正在刷新下方店铺卡片。`;
      persistBulkSetupCompletionSummary();
      // Terminal probe results are already committed to SQLite.  A full page
      // reload is more reliable than patching many card attributes by hand and
      // guarantees that seller ID, confirmation, enable state and account tails
      // all come from one fresh database snapshot.  The completion summary is
      // restored from sessionStorage after reload.
      setTimeout(() => location.reload(), 100);
      return;
    }
    ui.title.textContent = `正在打开 ${store.name}`;
    ui.message.textContent = `第 ${bulkStoreSetup.index + 1} / ${bulkStoreSetup.queue.length} 家；正在通过紫鸟启动对应店铺。`;
    try {
      bulkStoreSetup.storeId = store.id;
      bulkStoreSetup.probeId = null;
      bulkStoreSetup.probeStatus = "CHECKING";
      persistBulkStoreSetup();
      const {data, statusCode} = await apiResult(`/api/stores/${encodeURIComponent(String(store.id))}/detect-store-setup`, {method:"POST", body:JSON.stringify({marketplace_codes:store.marketplaceCodes})});
      const outcome = await handleBulkStoreSetupResponse(store, data, statusCode);
      if (outcome === "CHECKING") await pollBulkStoreSetup(store, bulkStoreSetup.probeId);
    } catch (exc) {
      bulkStoreSetup.active = false;
      bulkStoreSetup.resumeAvailable = true;
      persistBulkStoreSetup();
      ui.title.textContent = `批量队列暂停在 ${store.name}`;
      ui.message.textContent = `${exc.message} 已保存的结果不会丢失；已成功建档的数据已写入SQLite；下方店铺卡片将在队列结束后统一刷新。点击批量按钮将从当前未完成位置继续。`;
      ui.start.disabled = false;
      ui.start.textContent = "继续未完成队列";
      toast(exc.message, true);
    }
  }
  function formData(form) {
    const fd = new FormData(form), result = {};
    for (const [key, value] of fd.entries()) {
      if (result[key] !== undefined) result[key] = [].concat(result[key], value); else result[key] = value;
    }
    $$('input[type="checkbox"]', form).forEach(input => {
      if (input.name === "marketplace_codes") return;
      result[input.name] = input.checked;
    });
    if (!result.marketplace_codes && $("[name=marketplace_codes]", form)) result.marketplace_codes = [];
    return result;
  }
  const normalizedSellerIdentity = value => String(value || "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
  function storeDraft(form) {
    const id = Number($("[name=id]", form)?.value);
    const name = String($("[name=name]", form)?.value || "").trim();
    const expectedSellerId = String($("[name=expected_seller_id]", form)?.value || "").trim();
    return {
      id,
      data: {
        name,
        expected_seller_id: expectedSellerId || null,
        identity_confirmed: !!$("[name=identity_confirmed]", form)?.checked,
        enabled: !!$("[name=enabled]", form)?.checked,
        marketplaces: $$('[data-store-marketplace]', form).map(row => {
          return {
            code: row.dataset.storeMarketplace,
            enabled: !!$('[data-marketplace-enabled]', row)?.checked,
          };
        }),
      },
    };
  }
  async function persistStoreDraft(form) {
    const draft = storeDraft(form);
    if (!validPositiveId(draft.id)) throw new Error("未识别到当前店铺，请关闭窗口后重新点击“编辑建档”。");
    if (!draft.data.name) throw new Error("请先填写店铺显示名称。");
    if (draft.data.identity_confirmed && !draft.data.expected_seller_id) {
      throw new Error("勾选“我已核对卖家身份”前，请先自动检测或填写预期卖家 ID。");
    }
    const saved = await api(`/api/stores/${encodeURIComponent(String(draft.id))}`, {
      method:"PATCH",
      body:JSON.stringify(draft.data),
    });
    if (Number(saved.id) !== draft.id) throw new Error("服务端返回了错误的店铺记录，已停止自动检测。");
    if (draft.data.identity_confirmed && (
      !saved.identity_confirmed
      || normalizedSellerIdentity(saved.expected_seller_id) !== normalizedSellerIdentity(draft.data.expected_seller_id)
    )) {
      throw new Error("卖家身份建档未完整保存，已停止自动检测。");
    }
    // The server is authoritative.  In particular, changing seller identity
    // revokes every old payment-account baseline.  Reflect that response in
    // the still-open editor before a probe starts, otherwise a later save
    // could accidentally post an old tail back as an unverified manual value.
    $("[name=name]", form).value = String(saved.name || draft.data.name);
    $("[name=expected_seller_id]", form).value = String(saved.expected_seller_id || "");
    $("[name=identity_confirmed]", form).checked = !!saved.identity_confirmed;
    $("[name=enabled]", form).checked = !!saved.enabled;
    const savedMarketplaces = new Map(
      (saved.marketplaces || []).map(site => [String(site.code || "").toUpperCase(), site])
    );
    $$('[data-store-marketplace]', form).forEach(row => {
      const site = savedMarketplaces.get(String(row.dataset.storeMarketplace || "").toUpperCase());
      if (!site) return;
      $('[data-marketplace-enabled]', row).checked = !!site.enabled;
      setMarketplaceRowStatus(row, "PENDING", "勾选后核验域名与卖家身份");
    });
    form.dataset.persistedSellerId = String(saved.expected_seller_id || "");
    return {draft, saved};
  }
  async function confirmAction(message) {
    const dialog = $("#confirm-dialog"); if (!dialog) return window.confirm(message);
    $("#confirm-message", dialog).textContent = message;
    dialog.showModal(); return new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once:true}));
  }
  const scheduleModeLabels = {
    dry_run: "只读检查（不会点击提现）",
    approval: "人工审核（生成清单，批准后才提交）",
    auto: "全自动（检查通过后直接提交）",
  };
  const defaultScheduleDays = ["mon", "tue", "wed", "thu", "fri"];
  function selectedValues(form, name) {
    return $$(`input[name="${name}"]:checked`, form).map(input => input.value);
  }
  function setCheckedValues(form, name, values) {
    const wanted = new Set(values || []);
    $$(`input[name="${name}"]`, form).forEach(input => { input.checked = wanted.has(input.value); });
  }
  function enabledScheduleMarketplaces(form) {
    const option = $('[name="store_id"]', form)?.selectedOptions?.[0];
    if (!option) return new Set();
    try { return new Set(JSON.parse(option.dataset.enabledMarketplaces || "[]")); }
    catch (_) { return new Set(); }
  }
  function refreshScheduleMarketplaces(form, {preserveInvalid = false} = {}) {
    const enabled = enabledScheduleMarketplaces(form);
    const unavailableSelected = [];
    $$('input[name="marketplace_codes"]', form).forEach(input => {
      const available = enabled.has(input.value), label = input.closest("label");
      const invalidLegacySelection = input.checked && !available && preserveInvalid;
      if (invalidLegacySelection) unavailableSelected.push(input.value);
      if (!available) input.checked = false;
      input.disabled = !available;
      label?.classList.toggle("marketplace-disabled", !available);
      label?.classList.toggle("marketplace-invalid-selected", invalidLegacySelection);
      const text = label && $("span", label);
      if (text) {
        const base = text.textContent.replace(/（(?:未启用|原排期已失效)）$/, "");
        text.textContent = available ? base : `${base}${invalidLegacySelection ? "（原排期已失效）" : "（未启用）"}`;
      }
    });
    const help = $("[data-marketplace-help]", form), warning = $("[data-marketplace-warning]", form);
    const storeName = $('[name="store_id"]', form)?.selectedOptions?.[0]?.textContent?.trim() || "当前店铺";
    if (help) help.textContent = enabled.size
      ? `${storeName} 已启用：${[...enabled].join("、")}。只能勾选这些站点。`
      : `${storeName} 还没有启用任何站点，请先到“店铺账册”完成站点配置。`;
    if (warning) {
      warning.hidden = unavailableSelected.length === 0;
      warning.textContent = unavailableSelected.length
        ? `⚠️ 旧排期包含当前未启用的站点：${unavailableSelected.join("、")}，已从本次选择中移除。请改选上方可用站点并保存，之后才能立即执行。`
        : "";
    }
    form.dataset.hasInvalidMarketplaces = unavailableSelected.length ? "true" : "false";
  }
  function updateScheduleModeHelp(form) {
    const mode = $('[name="mode"]', form)?.value || "dry_run";
    const help = $("[data-mode-help]", form);
    if (help) help.textContent = scheduleModeLabels[mode] || mode;
  }
  function resetScheduleForm(form) {
    form.reset();
    delete form.dataset.scheduleId;
    $("[data-schedule-dialog-title]", form).textContent = "新建任务排期";
    $("[data-schedule-submit]", form).textContent = "保存排期";
    const store = $('[name="store_id"]', form);
    if (store) store.disabled = false;
    const editHelp = $("[data-store-edit-help]", form);
    if (editHelp) editHelp.hidden = true;
    $("[data-form-error]", form).textContent = "";
    setCheckedValues(form, "run_days", defaultScheduleDays);
    setCheckedValues(form, "marketplace_codes", []);
    refreshScheduleMarketplaces(form);
    updateScheduleModeHelp(form);
  }
  function openScheduleEditor(data) {
    const dialog = $("#schedule-dialog"), form = $("[data-schedule-form]", dialog);
    resetScheduleForm(form);
    form.dataset.scheduleId = String(data.id);
    $("[data-schedule-dialog-title]", form).textContent = "编辑任务排期";
    $("[data-schedule-submit]", form).textContent = "保存修改";
    for (const key of ["store_id", "name", "local_time", "mode"]) {
      const input = $(`[name="${key}"]`, form);
      if (input) input.value = data[key] ?? "";
    }
    const store = $('[name="store_id"]', form);
    if (store) store.disabled = true;
    const editHelp = $("[data-store-edit-help]", form);
    if (editHelp) editHelp.hidden = false;
    $('[name="enabled"]', form).checked = !!data.enabled;
    setCheckedValues(form, "marketplace_codes", data.marketplace_codes || []);
    refreshScheduleMarketplaces(form, {preserveInvalid:true});
    const days = data.days_of_week === "*" ? ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] : String(data.days_of_week || "").split(",").map(x => x.trim()).filter(Boolean);
    setCheckedValues(form, "run_days", days);
    updateScheduleModeHelp(form);
    dialog.showModal();
  }
  $$('[data-api-form]').forEach(form => form.addEventListener("submit", async event => {
    event.preventDefault(); const error = $("[data-form-error]", form); if (error) error.textContent = "";
    const button = $('button[type="submit"]', form); if (button) button.disabled = true;
    try { const data = await api(form.dataset.apiForm, {method:"POST", body:JSON.stringify(formData(form))}); location.href = data.next || form.dataset.redirect || "/"; }
    catch (exc) { if (error) error.textContent = exc.message; else toast(exc.message, true); }
    finally { if (button) button.disabled = false; }
  }));
  document.addEventListener("click", async event => {
    const target = event.target.closest("[data-action],[data-run-action]"); if (!target) return;
    const action = target.dataset.action;
    if (action === "toggle-menu") return $("#sidebar")?.classList.toggle("open");
    if (action === "logout") { try { const data = await api("/auth/logout", {method:"POST", body:"{}"}); location.href = data.next; } catch (exc) { toast(exc.message, true); } return; }
    if (action === "close-dialog") {
      const form = target.closest("form");
      if (form?.dataset.storeSetupProbeId) {
        const ui = storeSetupUi(form);
        ui.error.textContent = "检测正在占用紫鸟店铺窗口，请先点击当前检测区域中的“取消并关闭该店铺窗口”。";
        return;
      }
      if (target.closest("dialog")?.dataset.storeSetupChanged === "true") location.reload();
      return target.closest("dialog")?.close();
    }
    if (action === "sync-stores") { target.disabled = true; target.textContent = "同步中…"; try { const data = await api("/api/ziniao/sync", {method:"POST", body:"{}"}); toast(`同步完成：新增 ${data.created || 0}，更新 ${data.updated || 0}`); setTimeout(() => location.reload(), 600); } catch (exc) { toast(exc.message, true); } finally { target.disabled = false; target.textContent = "↻ 同步紫鸟店铺"; } return; }
    if (action === "edit-store") {
      const dialog = $("#store-dialog"), form = $("[data-store-form]", dialog), data = JSON.parse(target.dataset.store);
      clearStoreSetupProbe(form); delete dialog.dataset.storeSetupChanged; data.id = target.dataset.storeId || data.id;
      form.dataset.persistedSellerId = String(data.expected_seller_id || "");
      Object.entries(data).forEach(([key,value]) => {
        const input = $(`[name="${key}"]`, form); if (!input) return;
        input.type === "checkbox" ? input.checked = !!value : input.value = value ?? "";
      });
      const marketplaces = new Map((data.marketplaces || []).map(site => [String(site.code || "").toUpperCase(), site]));
      $$('[data-store-marketplace]', form).forEach(row => {
        const site = marketplaces.get(row.dataset.storeMarketplace) || {};
        $('[data-marketplace-enabled]', row).checked = !!site.enabled;
        setMarketplaceRowStatus(row, "PENDING", "勾选后核验域名与卖家身份");
      });
      dialog.dataset.storeId = String(data.id ?? ""); const ui = storeSetupUi(form); ui.error.textContent = "";
      ui.identity.textContent = "统一建档会在同一个紫鸟店铺窗口中读取卖家身份并逐站核验；身份一致时会自动绑定并确认，只有身份冲突才需要人工复核。";
      ui.result.textContent = "先勾选至少一个站点，再点击“自动获取卖家ID并核验站点”。";
      dialog.showModal(); return;
    }
    if (action === "detect-store-setup") {
      const form = target.closest("form"), id = Number($("[name=id]", form)?.value), ui = storeSetupUi(form);
      const marketplaceCodes = $$('[data-store-marketplace] [data-marketplace-enabled]:checked', form).map(input => input.closest("[data-store-marketplace]").dataset.storeMarketplace);
      if (!validPositiveId(id)) { ui.error.textContent = "未识别到当前店铺，请关闭窗口后重新点击“编辑建档”。"; return; }
      if (!marketplaceCodes.length) { ui.error.textContent = "请先勾选至少一个需要统一建档的站点。"; return; }
      target.disabled = true; target.textContent = "正在打开对应紫鸟店铺…"; ui.error.textContent = ""; ui.reset.disabled = true;
      marketplaceCodes.forEach(code => setMarketplaceRowStatus($(`[data-store-marketplace="${code}"]`, form), "CHECKING", "排队等待检查"));
      try {
        const {data, statusCode} = await apiResult(`/api/stores/${encodeURIComponent(String(id))}/detect-store-setup`, {method:"POST", body:JSON.stringify({marketplace_codes:marketplaceCodes})});
        await handleStoreSetupResponse(form, data, statusCode);
      } catch (exc) { failStoreSetup(form, exc.message); } finally {
        if (!form.dataset.storeSetupProbeId) clearStoreSetupProbe(form);
      }
      return;
    }
    if (action === "continue-store-setup") {
      const form = target.closest("form"), ui = storeSetupUi(form);
      const storeId = Number(form.dataset.storeSetupProbeStoreId), probeId = String(form.dataset.storeSetupProbeId || "");
      if (!validPositiveId(storeId) || !probeId) { ui.error.textContent = "找不到原自动建档现场，请取消后重新检测。"; return; }
      target.disabled = true; target.textContent = "正在尝试自动登录…"; ui.cancel.disabled = true; ui.error.textContent = "";
      try {
        const {data, statusCode} = await apiResult(`${storeSetupProbePath(storeId, probeId)}/continue`, {method:"POST", body:"{}"});
        await handleStoreSetupResponse(form, data, statusCode);
      } catch (exc) { failStoreSetup(form, exc.message); }
      finally {
        const stillWaitingForAuth = form.dataset.storeSetupProbeId === probeId
          && form.dataset.storeSetupProbeStatus === "WAITING_AUTH";
        if (stillWaitingForAuth) {
          ui.panel.hidden = false;
          target.disabled = false;
          target.textContent = "再次尝试自动登录并继续";
          ui.cancel.disabled = false;
        } else {
          hideStoreSetupAuth(form);
        }
      }
      return;
    }
    if (action === "cancel-store-setup") {
      const form = target.closest("form"), ui = storeSetupUi(form);
      const storeId = Number(form.dataset.storeSetupProbeStoreId), probeId = String(form.dataset.storeSetupProbeId || "");
      if (!validPositiveId(storeId) || !probeId) { clearStoreSetupProbe(form); return; }
      target.disabled = true; ui.resume.disabled = true; ui.error.textContent = "";
      try {
        const data = await api(`${storeSetupProbePath(storeId, probeId)}/cancel`, {method:"POST", body:"{}"});
        clearStoreSetupProbe(form); ui.result.textContent = data.message || "统一建档已取消，该紫鸟店铺窗口已释放。"; toast("已取消统一建档");
      } catch (exc) { ui.error.textContent = exc.message; target.disabled = false; ui.resume.disabled = false; }
      return;
    }
    if (action === "detect-all-store-setups") {
      if (bulkStoreSetup.queue.length && bulkStoreSetup.index < bulkStoreSetup.queue.length && bulkStoreSetup.resumeAvailable) {
        const currentStore = bulkStoreSetup.queue[bulkStoreSetup.index];
        bulkStoreSetup.active = true; target.disabled = true; target.textContent = "继续追踪中…";
        try {
          if (bulkStoreSetup.probeId) await pollBulkStoreSetup(currentStore, bulkStoreSetup.probeId);
          else await runNextBulkStoreSetup();
        } catch (exc) {
          // A missing/expired probe can no longer be queried. Keep the queue at
          // this store, but clear only the stale probe so an explicit second
          // click retries this unfinished store rather than store #1.
          bulkStoreSetup.storeId = null; bulkStoreSetup.probeId = null; bulkStoreSetup.probeStatus = null; bulkStoreSetup.active = false; persistBulkStoreSetup();
          const ui = bulkSetupUi(); ui.actions.hidden = true; if (ui.resume) ui.resume.disabled = true; if (ui.skip) ui.skip.disabled = true; ui.message.textContent = `${exc.message} 当前店铺位置已保留，再点一次将只重试这家店。`; target.disabled = false; target.textContent = "重试当前未完成店铺"; toast(exc.message, true);
        }
        return;
      }
      const allCards = $$('[data-store-card]');
      const cards = allCards.filter(card => card.dataset.storeSetupNeeded === "true");
      const eligible = cards.map(card => {
        let marketplaceCodes = [];
        try { marketplaceCodes = JSON.parse(card.dataset.storeSetupMarketplaces || "[]"); } catch (_) {}
        marketplaceCodes = [...new Set(marketplaceCodes.map(code => String(code).toUpperCase()).filter(code => ["CA", "UK", "AU"].includes(code)))];
        const defaulted = marketplaceCodes.length === 0;
        if (defaulted) marketplaceCodes = ["CA", "UK", "AU"];
        return {
          id:Number(card.dataset.storeId),
          name:card.dataset.storeName || `店铺 #${card.dataset.storeId}`,
          marketplaceCodes,
          defaulted,
          identityState:String(card.dataset.identityState || "MISSING").toUpperCase(),
        };
      }).filter(store => validPositiveId(store.id));
      const alreadyComplete = allCards.length - cards.length;
      if (!eligible.length) {
        toast(allCards.length ? "全部店铺的卖家身份和已启用站点都已完成建档。" : "暂无可建档店铺。", true);
        return;
      }
      const defaultedCount = eligible.filter(store => store.defaulted).length;
      const defaultedCopy = defaultedCount ? `其中 ${defaultedCount} 家尚未选择站点，将默认按 CA、UK、AU 三站建档。` : "";
      const completeCopy = alreadyComplete ? `另有 ${alreadyComplete} 家已完成，将跳过。` : "";
      if (!(await confirmAction(`将按顺序统一建档 ${eligible.length} 家店铺；一家只打开一次紫鸟，读取卖家 ID 并逐站核验域名。${defaultedCopy}${completeCopy}是否开始？`))) return;
      sessionStorage.removeItem(bulkSetupSummaryStorageKey);
      Object.assign(bulkStoreSetup, {active:true, queue:eligible, index:0, success:0, siteIncomplete:0, auth:0, failed:0, storeId:null, probeId:null, probeStatus:null, authCountedProbeId:null, resumeAvailable:false});
      persistBulkStoreSetup();
      const ui = bulkSetupUi(); ui.panel.hidden = false; ui.actions.hidden = true; target.disabled = true; target.textContent = "批量建档进行中…"; updateBulkSetupCounts();
      await runNextBulkStoreSetup(); return;
    }
    if (action === "continue-bulk-store-setup") {
      if (!bulkStoreSetup.active || !validPositiveId(bulkStoreSetup.storeId) || !bulkStoreSetup.probeId) { toast("批量检测现场已失效，请重新开始。", true); return; }
      const store = bulkStoreSetup.queue[bulkStoreSetup.index], ui = bulkSetupUi();
      target.disabled = true; target.textContent = "正在尝试自动登录…"; ui.actions.hidden = true;
      try {
        const {data, statusCode} = await apiResult(`${storeSetupProbePath(bulkStoreSetup.storeId, bulkStoreSetup.probeId)}/continue`, {method:"POST", body:"{}"});
        const outcome = await handleBulkStoreSetupResponse(store, data, statusCode);
        if (outcome === "CHECKING") await pollBulkStoreSetup(store, bulkStoreSetup.probeId);
      } catch (exc) {
        ui.message.textContent = exc.message;
        toast(exc.message, true);
        if (bulkStoreSetup.probeStatus !== "WAITING_AUTH") {
          bulkStoreSetup.active = false;
          bulkStoreSetup.resumeAvailable = true;
          persistBulkStoreSetup();
          ui.start.disabled = false;
          ui.start.textContent = "继续未完成队列";
        }
      }
      finally {
        const stillWaitingForAuth = bulkStoreSetup.active
          && bulkStoreSetup.probeStatus === "WAITING_AUTH"
          && validPositiveId(bulkStoreSetup.storeId)
          && !!bulkStoreSetup.probeId;
        ui.actions.hidden = !stillWaitingForAuth;
        target.disabled = !stillWaitingForAuth;
        if (stillWaitingForAuth) {
          target.textContent = "再次尝试自动登录并继续";
          if (ui.skip) ui.skip.disabled = false;
        } else if (ui.skip) {
          ui.skip.disabled = true;
        }
      }
      return;
    }
    if (action === "skip-bulk-store-setup") {
      if (!bulkStoreSetup.active || !validPositiveId(bulkStoreSetup.storeId) || !bulkStoreSetup.probeId) return;
      const ui = bulkSetupUi(); target.disabled = true;
      let cancelled = null;
      try { cancelled = await api(`${storeSetupProbePath(bulkStoreSetup.storeId, bulkStoreSetup.probeId)}/cancel`, {method:"POST", body:"{}"}); }
      catch (exc) {
        // A 404 after a refresh/service restart means there is no backend
        // probe left to close. Honour the explicit Skip locally so the whole
        // queue cannot become trapped behind an expired identifier.
        if (exc.statusCode !== 404) { toast(exc.message, true); target.disabled = false; return; }
        toast("原自动建档现场已过期，已跳过这家店并继续队列", true);
      }
      if (cancelled && identitySetupSucceeded(cancelled)) summarizeBulkStoreResult(cancelled);
      else { bulkStoreSetup.failed += 1; updateBulkSetupCounts(); }
      bulkStoreSetup.storeId = null; bulkStoreSetup.probeId = null; bulkStoreSetup.probeStatus = "CANCELLED"; bulkStoreSetup.resumeAvailable = false; bulkStoreSetup.index += 1; persistBulkStoreSetup(); ui.actions.hidden = true; if (ui.resume) ui.resume.disabled = true; target.disabled = false; await runNextBulkStoreSetup(); return;
    }
    if (action === "reset-store-setup") {
      const form = target.closest("form"), ui = storeSetupUi(form), id = Number($("[name=id]", form)?.value);
      if (!validPositiveId(id)) { ui.error.textContent = "当前店铺编号无效，请刷新页面后重试。"; return; }
      if (form.dataset.storeSetupProbeId) { ui.error.textContent = "统一建档仍在运行，请先取消并关闭当前紫鸟店铺窗口。"; return; }
      const name = String($("[name=name]", form)?.value || `店铺 #${id}`).trim();
      if (!(await confirmAction(`确定删除并重置“${name}”的建档吗？卖家 ID、身份确认、启用状态将清空，该店铺的排期会停用；站点勾选、紫鸟店铺环境、历史运行记录与资金记录都会保留。`))) return;
      target.disabled = true; ui.error.textContent = "";
      try {
        await api(`/api/stores/${encodeURIComponent(String(id))}/setup`, {method:"DELETE", body:"{}"});
        toast("店铺建档已重置");
        setTimeout(() => location.reload(), 500);
      } catch (exc) { ui.error.textContent = exc.message; target.disabled = false; }
      return;
    }
    if (action === "new-run") { const dialog = $("#run-dialog"); $("[name=store_id]", dialog).value = target.dataset.storeId; dialog.showModal(); return; }
    if (action === "open-schedule") {
      const dialog = $("#schedule-dialog"), form = $("[data-schedule-form]", dialog);
      resetScheduleForm(form); dialog?.showModal(); return;
    }
    if (action === "edit-schedule") {
      try { openScheduleEditor(JSON.parse(target.dataset.schedule || "{}")); }
      catch (_) { toast("排期数据读取失败，请刷新页面后重试。", true); }
      return;
    }
    if (action === "run-schedule-now") {
      const id = Number(target.dataset.scheduleId);
      if (!validPositiveId(id)) { toast("排期编号无效，请刷新页面后重试。", true); return; }
      const mode = target.dataset.scheduleMode || "dry_run";
      const modeLabel = scheduleModeLabels[mode] || mode;
      const name = target.dataset.scheduleName || `排期 #${id}`;
      if (!(await confirmAction(`确认现在执行“${name}”吗？当前模式：${modeLabel}。本次会立即运行，不用等待设定时间。`))) return;
      target.disabled = true; target.textContent = "正在创建任务…";
      try {
        const data = await api(`/api/schedules/${id}/run-now`, {method:"POST", body:"{}"});
        toast("任务已进入执行队列");
        location.href = data.redirect || `/runs/${data.run_id}`;
      } catch (exc) { toast(exc.message, true); target.disabled = false; target.textContent = "立即执行"; }
      return;
    }
    if (action === "delete-schedule") {
      const id = Number(target.dataset.scheduleId);
      if (!validPositiveId(id)) { toast("排期编号无效，请刷新页面后重试。", true); return; }
      const name = target.dataset.scheduleName || `排期 #${id}`;
      if (!(await confirmAction(`确定永久删除排期“${name}”吗？删除后不会再自动运行，已有运行记录不会被删除。`))) return;
      target.disabled = true;
      try { await api(`/api/schedules/${id}`, {method:"DELETE", body:"{}"}); toast("排期已删除"); setTimeout(() => location.reload(), 400); }
      catch (exc) { toast(exc.message, true); target.disabled = false; }
      return;
    }
    const runAction = target.dataset.runAction;
    if (runAction === "release-guard") {
      // Deleting a money guard, so the operator states the fact rather than
      // confirming a vague "此操作": the server refuses anyway once a dispatch
      // was recorded, but the person clicking should see what they are asserting.
      const site = target.dataset.marketplace || "该站点";
      const agreed = await confirmAction(`确认 ${site} 的 ${target.dataset.amount || "这笔提现"} 从未发出过提现请求吗？释放后这条资金锁定会被删除，该站点可以重新尝试提现。如果它其实已经发出，重新尝试就会重复提现。`);
      if (!agreed) return;
      target.disabled = true;
      try {
        await api(`/api/runs/${target.dataset.runId}/guards/${encodeURIComponent(target.dataset.guardKey)}/release`, {method:"POST", body:"{}"});
        toast("资金锁定已释放，该站点可重新尝试");
        setTimeout(() => location.reload(), 800);
      } catch (exc) { toast(exc.message, true); target.disabled = false; }
      return;
    }
    if (runAction === "acknowledge-guard") {
      // The mirror of release: here the operator asserts the payout DID happen.
      // Make it unmistakable that this is their finding from Seller Central and
      // not something the system read back, because nothing will re-check it.
      const site = target.dataset.marketplace || "该站点";
      const agreed = await confirmAction(`你已经在亚马逊后台看到 ${site} 的 ${target.dataset.amount || "这笔提现"} 确实转出了吗？确认后这条记录结案为「已确认」，系统不会再回读。如果你其实没有核对过，请先去亚马逊付款记录里查证。`);
      if (!agreed) return;
      target.disabled = true;
      try {
        await api(`/api/runs/${target.dataset.runId}/guards/${encodeURIComponent(target.dataset.guardKey)}/acknowledge`, {method:"POST", body:"{}"});
        toast("已按你的人工核对结案");
        setTimeout(() => location.reload(), 800);
      } catch (exc) { toast(exc.message, true); target.disabled = false; }
      return;
    }
    if (runAction) {
      const map = {approve:"approve", cancel:"cancel", reconcile:"reconcile", "continue-auth":"continue-auth"};
      const message = target.dataset.confirm || (runAction === "cancel" ? "确认取消这个任务？资金锁定后的任务仍只会进入回读。" : "确认执行此操作？");
      if (!(await confirmAction(message))) return;
      target.disabled = true; try { await api(`/api/runs/${target.dataset.runId}/${map[runAction]}`, {method:"POST", body:"{}"}); toast("请求已进入队列"); setTimeout(() => location.reload(), 800); } catch (exc) { toast(exc.message, true); target.disabled = false; }
    }
  });
  $("[data-store-form]")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget;
    const error = $("[data-form-error]", form); error.textContent = "";
    const button = $('button[type="submit"]', form); if (button) button.disabled = true;
    try { await persistStoreDraft(form); toast("店铺建档已保存"); setTimeout(() => location.reload(), 500); }
    catch (exc) { error.textContent = exc.message; if (button) button.disabled = false; }
  });
  const storeEditor = $("[data-store-form]");
  $("[name=expected_seller_id]", storeEditor || document)?.addEventListener("input", event => {
    const form = event.currentTarget.form;
    if (!form || normalizedSellerIdentity(event.currentTarget.value) === normalizedSellerIdentity(form.dataset.persistedSellerId)) return;
    $("[name=identity_confirmed]", form).checked = false;
    $("[name=enabled]", form).checked = false;
  });
  $("[name=identity_confirmed]", storeEditor || document)?.addEventListener("change", event => {
    if (!event.currentTarget.checked) $("[name=enabled]", event.currentTarget.form).checked = false;
  });
  $("[data-run-form]")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget, error = $("[data-form-error]", form); error.textContent = "";
    const data = formData(form); data.store_id = Number(data.store_id);
    try { const run = await api("/api/runs", {method:"POST", body:JSON.stringify(data)}); location.href = `/runs/${run.id}`; } catch (exc) { error.textContent = exc.message; }
  });
  $("[data-schedule-form]")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget, error = $("[data-form-error]", form); error.textContent = "";
    const marketplaceCodes = selectedValues(form, "marketplace_codes"), runDays = selectedValues(form, "run_days");
    if (!marketplaceCodes.length) { error.textContent = "请至少勾选一个站点（CA、UK 或 AU）。"; return; }
    if (!runDays.length) { error.textContent = "请至少勾选一个每周运行日。"; return; }
    const editingId = Number(form.dataset.scheduleId || 0);
    const data = formData(form);
    delete data.run_days;
    data.marketplace_codes = marketplaceCodes;
    data.days_of_week = runDays.length === 7 ? "*" : runDays.join(",");
    data.timezone = "Asia/Singapore";
    if (editingId) delete data.store_id;
    else { data.store_id = Number(data.store_id); data.workflow = "amazon_disbursement"; }
    const button = $("[data-schedule-submit]", form); button.disabled = true;
    try {
      await api(editingId ? `/api/schedules/${editingId}` : "/api/schedules", {method:editingId ? "PATCH" : "POST", body:JSON.stringify(data)});
      toast(editingId ? "排期修改已保存" : "排期已保存"); setTimeout(() => location.reload(), 500);
    } catch (exc) { error.textContent = exc.message; button.disabled = false; }
  });
  $('[data-schedule-mode]')?.addEventListener("change", event => updateScheduleModeHelp(event.currentTarget.form));
  $('[data-schedule-store]')?.addEventListener("change", event => refreshScheduleMarketplaces(event.currentTarget.form));
  if ($('[data-action="detect-all-store-setups"]')) {
    restoreBulkStoreSetup();
    if (!bulkStoreSetup.queue.length) restoreBulkSetupCompletionSummary();
  }
})();
