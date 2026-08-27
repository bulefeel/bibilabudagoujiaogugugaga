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
  function apiDetailMessage(detail, fallback = "请求失败") {
    if (Array.isArray(detail)) return detail.map(item => typeof item === "string" ? item : (item?.msg || item?.message || JSON.stringify(item))).join("；") || fallback;
    if (detail && typeof detail === "object") {
      const preview = detail.preview || detail;
      if (preview && Array.isArray(preview.targets)) {
        const bad = preview.targets.filter(item => item && item.eligible === false).map(item => {
          const reasons = Array.isArray(item.reasons) ? item.reasons.join("；") : String(item.reason || "");
          return String(item.store_name || ("店铺 #" + item.store_id)) + (reasons ? "：" + reasons : "");
        });
        if (bad.length) return "批量预检未通过：" + bad.join("；");
      }
      return detail.message || detail.detail || fallback;
    }
    return detail || fallback;
  }
  async function api(url, options = {}) {
    const headers = {"Content-Type":"application/json", ...options.headers};
    if ((options.method || "GET") !== "GET") headers["X-CSRF-Token"] = csrf();
    const response = await fetch(url, {...options, headers});
    let data = {}; try { data = await response.json(); } catch (_) {}
    if (!response.ok) {
      const detail = apiDetailMessage(data.detail, `请求失败 (${response.status})`);
      if (response.status === 401) location.href = "/login";
      const error = new Error(detail);
      error.statusCode = response.status;
      error.detail = data.detail;
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
      const detail = apiDetailMessage(data.detail, `请求失败 (${response.status})`);
      if (response.status === 401) location.href = "/login";
      const error = new Error(detail);
      error.statusCode = response.status;
      error.detail = data.detail;
      throw error;
    }
    return {data, statusCode: response.status};
  }
  const storeSetupProbePath = (storeId, probeId) => `/api/stores/${encodeURIComponent(String(storeId))}/store-setup-probes/${encodeURIComponent(String(probeId))}`;
  const validPositiveId = value => Number.isSafeInteger(Number(value)) && Number(value) > 0;
  const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const ziniaoCredentialErrorCode = "ZINIAO_CREDENTIALS_INVALID";
  const isZiniaoCredentialError = error => error?.statusCode === 409
    && error?.detail?.code === ziniaoCredentialErrorCode;
  const ziniaoCredentialGuidance = error => {
    const message = String(error?.detail?.message || error?.message || "紫鸟凭据预检未通过").trim();
    return `${message} 尚未打开任何紫鸟店铺；请点击左侧“系统诊断”重新保存紫鸟公司、账号和密码，保存后从当前店铺继续。`;
  };
  function storeSetupUi(form) {
    return {
      panel: $("[data-store-setup-auth-panel]", form),
      badge: $("[data-store-setup-auth-badge]", form),
      title: $("[data-store-setup-auth-title]", form),
      message: $("[data-store-setup-auth-message]", form),
      identity: $("[data-store-setup-identity-result]", form),
      result: $("[data-store-setup-result]", form),
      error: $("[data-form-error]", form),
      detect: $('[data-action="detect-store-setup"]', form),
      resume: $('[data-action="continue-store-setup"]', form),
      cancel: $('[data-action="cancel-store-setup"]', form),
      reset: $('[data-action="reset-store-setup"]', form),
      // Selected by its own attribute, never by button[type="submit"]: the
      // diagnostics page already broke once when a button's type changed and a
      // handler kept looking it up by type.
      save: $("[data-store-setup-save]", form),
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
    if (ui.resume) { ui.resume.hidden = false; ui.resume.disabled = false; ui.resume.textContent = "再次尝试自动登录并继续"; }
    if (ui.cancel) ui.cancel.disabled = false;
    if (ui.detect) { ui.detect.disabled = false; ui.detect.textContent = "自动获取卖家ID并核验站点"; }
    if (ui.reset) ui.reset.disabled = false;
    if (ui.save) ui.save.disabled = false;
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
  function pauseStoreSetupForCredential(form, error) {
    const ui = storeSetupUi(form), message = ziniaoCredentialGuidance(error);
    $$('[data-store-marketplace]', form).forEach(row => {
      if (String(row.dataset.marketplaceProbeStatus || "").toUpperCase() === "CHECKING") {
        setMarketplaceRowStatus(row, "PENDING", "尚未启动：请先重新保存紫鸟凭据");
      }
    });
    ui.error.textContent = message;
    ui.result.textContent = "本次统一建档尚未创建检测任务，也未将该店计为核验失败。修复系统凭据后可直接重试。";
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
  // One panel for every live probe state, not just WAITING_AUTH. While a probe
  // was CHECKING the cancel button lived inside a section nothing ever unhid,
  // so the operator was told to "取消检测" with no such control on the page —
  // and the reset they reached for always came back 409.
  function showStoreSetupProbePanel(form, data) {
    rememberStoreSetupProbe(form, data);
    renderStoreSetupIdentity(form, data);
    renderStoreSetupResults(form, data);
    const ui = storeSetupUi(form);
    const checking = String(data.status || "").toUpperCase() === "CHECKING";
    ui.panel.hidden = false;
    if (ui.badge) ui.badge.textContent = checking ? "检测进行中" : "自动登录未通过";
    if (ui.title) ui.title.textContent = checking ? "正在核验，暂时不能保存或重置建档" : "验证仍未完成，可再次自动尝试";
    ui.message.textContent = checking
      ? (data.message || "正在同一个紫鸟店铺窗口读取卖家身份并逐站核验，全程不点击页面上的任何按钮。想中止请点下方“取消并关闭该店铺窗口”。")
      : (data.message || "自动登录已经尝试邮箱 Continue、紫鸟托管 Passkey 和已填好的 6 位 OTP，但页面仍停在验证步骤。可先再次尝试自动登录；普通密码、其他 Passkey、CAPTCHA 或未填好的验证码需要在当前紫鸟窗口处理。");
    ui.result.textContent = checking
      ? "检测占用着这家店的紫鸟窗口，期间不能保存或重置建档；取消后两个按钮会立刻恢复。"
      : "统一建档只在自动登录未能通过时暂停；已读取结果会保留，再次继续仍复用当前紫鸟窗口。";
    ui.detect.disabled = true;
    ui.detect.textContent = checking ? "正在核验，请稍候…" : "自动登录未通过，等待处理…";
    // Continuing only means something while the probe waits on a human.
    if (ui.resume) { ui.resume.hidden = checking; ui.resume.disabled = checking; ui.resume.textContent = "再次尝试自动登录并继续"; }
    if (ui.cancel) ui.cancel.disabled = false;
    if (ui.reset) ui.reset.disabled = true;
    if (ui.save) ui.save.disabled = true;
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
  // The server, not this tab, knows whether a probe still owns a store. Asking
  // on every editor open is what makes the cancel button survive a reload, a
  // reopened dialog or a closed tab — losing the id used to leave the operator
  // with a reset that only ever answered 409.
  //
  // Deliberately not awaited by the caller and fully self-contained: a store
  // editor must open even when this call fails, and app.js is one bundle for
  // every page, so an escaping rejection here must not reach the top level.
  function restoreActiveStoreSetupProbe(form, storeId) {
    const id = Number(storeId);
    if (!validPositiveId(id)) return;
    (async () => {
      const {data, statusCode} = await apiResult(`/api/stores/${encodeURIComponent(String(id))}/store-setup-probe`);
      if (statusCode === 204 || !data || !data.probe_id) return;
      if (Number($("[name=id]", form)?.value) !== id) return;  // editor moved on
      const status = String(data.status || "").toUpperCase();
      if (!["CHECKING", "WAITING_AUTH"].includes(status)) return;
      const {probeId} = rememberStoreSetupProbe(form, data);
      showStoreSetupProbePanel(form, data);
      await pollStoreSetupProbe(form, id, probeId);
    })().catch(exc => {
      console.error("[ziniao] 恢复统一建档现场失败", exc);
    });
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
      if (status === "WAITING_AUTH") { showStoreSetupProbePanel(form, data); return; }
      if (status !== "CHECKING") throw new Error(data.message || `自动建档状态异常：${status || "未知"}`);
      showStoreSetupProbePanel(form, data);
    }
    const ui = storeSetupUi(form);
    ui.result.textContent = "检测仍在进行。为避免高频请求，页面已暂停查询；点击“继续查询当前建档”可查看原任务，不会重新打开店铺。";
    // Polling stops here, the probe does not. Keep the panel — and with it the
    // cancel button — on screen: this is precisely when an operator gives up
    // waiting and reaches for reset, and reset stays blocked until the probe
    // ends. Hiding the only way out was the whole bug.
    ui.detect.disabled = false;
    ui.detect.textContent = "继续查询当前建档";
    if (ui.reset) ui.reset.disabled = true;
    if (ui.save) ui.save.disabled = true;
  }
  async function handleStoreSetupResponse(form, data, statusCode) {
    const status = String(data.status || "").toUpperCase();
    if (storeSetupTerminalStatuses.has(status)) { finishStoreSetup(form, data); return; }
    if (status === "WAITING_AUTH") { showStoreSetupProbePanel(form, data); return; }
    if (statusCode === 202 && status === "CHECKING") {
      const {storeId, probeId} = rememberStoreSetupProbe(form, data);
      // Renders the CHECKING panel: the stale "continue" button is hidden
      // because the probe is no longer waiting on a human, but the cancel
      // button stays, since the probe still owns the store's Ziniao window.
      showStoreSetupProbePanel(form, data);
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
  // The store cards on this page are rendered from SQLite on every load; the
  // archived queue only records what some earlier run believed. Between the two
  // page loads the operator may have undone that run — 「删除 / 重置建档」 clears
  // the identity and then reloads this very tab — and nothing used to notice, so
  // the banner went on announcing 「身份已建档 2」 over three cards all reading
  // 「尚未绑定」. Whatever the archive claims about stores it already processed
  // has to still be true on screen, or the whole archive is fiction.
  function bulkQueueArchiveIsStale(queue, index, success) {
    const cards = new Map(
      $$("[data-store-card]").map(card => [Number(card.dataset.storeId), card])
    );
    if (queue.some(store => !cards.has(store.id))) return true;
    const boundSoFar = queue.slice(0, index).filter(
      store => String(cards.get(store.id).dataset.identityState || "").toUpperCase() === "CONFIRMED"
    ).length;
    // Legitimate runs undershoot this (a store may have genuinely failed);
    // only claiming more bound stores than the database actually has is proof
    // the archive is describing a state that no longer exists.
    return success > boundSoFar;
  }
  function discardBulkStoreSetup() {
    try { sessionStorage.removeItem(bulkSetupStorageKey); } catch (_) {}
    Object.assign(bulkStoreSetup, {
      active: false, queue: [], index: 0, success: 0, siteIncomplete: 0, auth: 0,
      failed: 0, storeId: null, probeId: null, probeStatus: null,
      authCountedProbeId: null, resumeAvailable: false,
    });
    const ui = bulkSetupUi();
    if (ui.panel) ui.panel.hidden = true;
    if (ui.actions) ui.actions.hidden = true;
    if (ui.dismiss) ui.dismiss.hidden = true;
    if (ui.start) { ui.start.disabled = false; ui.start.textContent = "一键自动建档全部店铺"; }
    updateBulkSetupCounts();
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
    if (bulkQueueArchiveIsStale(queue, index, Number(saved.success || 0))) {
      // Silently: the operator did not ask for this queue, and a banner about a
      // run they already undid is exactly the noise being removed.
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
    const credentialPaused = bulkStoreSetup.probeStatus === ziniaoCredentialErrorCode;
    ui.title.textContent = credentialPaused
      ? `批量队列因系统凭据暂停在 ${queue[index].name}`
      : `发现未完成队列：${queue[index].name}`;
    ui.message.textContent = credentialPaused
      ? "请先点击左侧“系统诊断”重新保存紫鸟公司、账号和密码；保存后点击下方按钮，只会重试当前店铺。"
      : "点击“继续未完成队列”会从上次位置查询原任务，不会从第一家重复触发。";
    ui.start.disabled = false;
    ui.start.textContent = credentialPaused ? "凭据保存后重试当前店铺" : "继续未完成队列";
    // A restored queue takes over the toolbar button — it resumes rather than
    // starting a fresh pass — so it must come with a way to put it down. Without
    // one the only escape was closing the tab, and nothing on screen said so.
    if (ui.dismiss) ui.dismiss.hidden = false;
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
      dismiss: $("[data-bulk-store-setup-dismiss]", panel || document),
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
  function pauseBulkStoreSetupForCredential(store, error) {
    const ui = bulkSetupUi(), message = ziniaoCredentialGuidance(error);
    bulkStoreSetup.active = false;
    bulkStoreSetup.storeId = Number(store.id);
    bulkStoreSetup.probeId = null;
    bulkStoreSetup.probeStatus = ziniaoCredentialErrorCode;
    bulkStoreSetup.resumeAvailable = true;
    persistBulkStoreSetup();
    ui.actions.hidden = true;
    if (ui.resume) ui.resume.disabled = true;
    if (ui.skip) ui.skip.disabled = true;
    ui.title.textContent = `批量队列因系统凭据暂停在 ${store.name}`;
    ui.message.textContent = `${message} 当前店铺位置和此前成功结果均已保留；本次不计为店铺失败，也不会继续打开下一家。`;
    ui.start.disabled = false;
    ui.start.textContent = "凭据保存后重试当前店铺";
    toast("紫鸟凭据需要重新保存；批量队列已暂停", true);
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
      if (isZiniaoCredentialError(exc)) {
        pauseBulkStoreSetupForCredential(store, exc);
        return;
      }
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
    dry_run: "只读检查（不会执行实际操作）",
    approval: "人工审核（生成清单，批准后才执行）",
    auto: "全自动（检查通过后执行）",
  };
  const scheduleModeShortLabels = {dry_run:"只读检查（dry_run）", approval:"人工审核（approval）", auto:"全自动（auto）"};
  const defaultScheduleDays = ["mon","tue","wed","thu","fri"];
  // Compatibility notes for the original single-store editor:
  // method:editingId ? "PATCH" : "POST"
  // data.days_of_week = runDays.length === 7 ? "*" : runDays.join(",")
  // 当前模式：${modeLabel}；已有运行记录不会被删除
  // input.disabled = !available; 旧排期包含当前未启用的站点
  // 旧提示：请至少勾选一个站点；请至少勾选一个每周运行日
  // refreshScheduleMarketplaces：旧单店编辑由服务端在 PATCH 时再次校验可用站点。
  const fallbackScheduleWorkflows = [{
    key:"amazon_disbursement", display_name:"亚马逊提现",
    description:"读取指定站点的 PAYABLE 余额；只有 approval/auto 且规则通过时才会提交提现。",
    supported_modes:["dry_run","approval","auto"], default_mode:"dry_run", config_version:1,
    requires_marketplace_targets:true, requires_confirmed_identity:true,
    requires_financial_lock:true, execution_class:"financial", business_priority:1,
    config_schema:{type:"object", additionalProperties:false,
      properties:{marketplace_codes:{type:"array",title:"站点",items:{type:"string",enum:["CA","UK","AU"]},minItems:1}},
      required:["marketplace_codes"]},
  }];
  let scheduleWorkflows = [];
  let scheduleWorkflowReady = null;
  let schedulePreviewSequence = 0;
  function invalidateSchedulePreview(form){const nonce=String(++schedulePreviewSequence);form.dataset.previewNonce=nonce;delete form.dataset.previewed;delete form.dataset.batchPreviewHash;delete form.dataset.batchPreviewPayload;return nonce;}
  function selectedValues(form,name){return $$('input[name="'+name+'"]:checked',form).map(input=>input.value);}
  function setCheckedValues(form,name,values){const wanted=new Set(values||[]); $$('input[name="'+name+'"]',form).forEach(input=>{input.checked=wanted.has(input.value);});}
  function workflowMeta(form){const key=$('[name="workflow"]',form)?.value; return scheduleWorkflows.find(item=>item.key===key)||scheduleWorkflows[0]||fallbackScheduleWorkflows[0];}
  function safeWorkflowKey(value){return String(value||"").trim().replace(/[^a-zA-Z0-9_.-]/g,"");}
  function resolveWorkflowFieldSchema(meta,source){
    const root=meta?.config_schema&&typeof meta.config_schema==="object"?meta.config_schema:{};
    let schema=source&&typeof source==="object"?{...source}:{};
    for(let depth=0;depth<6;depth+=1){
      let changed=false;
      if(typeof schema.$ref==="string"&&schema.$ref.startsWith("#/$defs/")){
        const name=schema.$ref.slice(8),target=root.$defs?.[name];
        if(target&&typeof target==="object"){const siblings={...schema};delete siblings.$ref;schema={...target,...siblings};changed=true;}
      }
      const variantKey=Array.isArray(schema.anyOf)?"anyOf":Array.isArray(schema.oneOf)?"oneOf":null;
      if(variantKey){const variants=schema[variantKey],usable=variants.filter(item=>item&&item.type!=="null"),nullable=variants.some(item=>item&&item.type==="null"),siblings={...schema};delete siblings[variantKey];if(usable.length!==1){schema={...siblings,__uiUnsupported:true};break;}const resolved=resolveWorkflowFieldSchema(meta,usable[0]);schema={...resolved,...siblings,__uiNullable:Boolean(nullable||resolved.__uiNullable)};changed=true;}
      if(Array.isArray(schema.allOf)&&schema.allOf.length){const parts=schema.allOf,siblings={...schema};delete siblings.allOf;schema={...Object.assign({},...parts.map(item=>resolveWorkflowFieldSchema(meta,item))),...siblings};changed=true;}
      if(!changed)break;
    }
    if(Object.prototype.hasOwnProperty.call(schema,"const")&&!Array.isArray(schema.enum))schema={...schema,enum:[schema.const]};
    return schema;
  }
  function workflowProperties(meta){const root=meta?.config_schema;if(!root||typeof root!=="object"||!root.properties||typeof root.properties!=="object")return{};return Object.fromEntries(Object.entries(root.properties).map(([key,value])=>[key,resolveWorkflowFieldSchema(meta,value)]));}
  function fieldLabel(key,schema){const labels={marketplace_codes:"站点",account_tail:"付款账户尾号",amount_limit:"金额上限"}; return schema?.title||labels[key]||key.replace(/[_-]+/g," ");}
  function renderWorkflowGuide(form,meta){
    const descriptionNode=$("[data-workflow-description]",form);if(descriptionNode)descriptionNode.textContent=meta?.description||"仅执行代码中注册的固定流程。";
    const guide=$("[data-workflow-guide]",form); if(!guide)return; guide.replaceChildren();
    const description=document.createElement("p"); description.textContent=meta?.description||"由已注册的固定流程执行，不支持上传或拼接任意脚本。"; guide.append(description);
    (Array.isArray(meta?.supported_modes)?meta.supported_modes:["dry_run"]).forEach(mode=>{const p=document.createElement("p"); const b=document.createElement("b"); b.textContent=(scheduleModeShortLabels[mode]||mode)+"："; p.append(b,document.createTextNode(mode==="dry_run"?"只读取并检查，不点击不可逆操作。":mode==="approval"?"先生成清单，批准后重新核对再提交。":mode==="auto"?"规则全部通过后直接执行已注册动作。":"按该流程的安全规则运行。")); guide.append(p);});
  }
  function renderWorkflowConfig(form,values={}){
    const container=$("[data-workflow-config]",form);if(!container)return;const meta=workflowMeta(form);container.replaceChildren();
    let properties=workflowProperties(meta);if(!Object.keys(properties).length&&meta?.key==="amazon_disbursement")properties={marketplace_codes:{type:"array",title:"站点",items:{type:"string",enum:["CA","UK","AU"]},minItems:1}};
    if(!Object.keys(properties).length){container.hidden=true;return;}container.hidden=false;
    const required=new Set(Array.isArray(meta?.config_schema?.required)?meta.config_schema.required:[]);
    Object.entries(properties).forEach(([rawKey,rawSchema])=>{
      const key=safeWorkflowKey(rawKey);if(!key)return;const schema=resolveWorkflowFieldSchema(meta,rawSchema),type=schema.type||(Array.isArray(schema.enum)?"string":"string"),provided=Object.prototype.hasOwnProperty.call(values,key),initial=provided?values[key]:schema.default;
      const holder=document.createElement(type==="array"?"fieldset":"label");holder.dataset.configField=key;holder.dataset.configRequired=required.has(rawKey)?"true":"false";
      if(type==="array"){
        const legend=document.createElement("legend");legend.textContent=fieldLabel(key,schema)+(Number(schema.minItems||0)>0?"（至少选择 "+schema.minItems+" 个）":"");holder.append(legend);
        const itemSchema=resolveWorkflowFieldSchema(meta,schema.items||{}),enumValues=Array.isArray(itemSchema.enum)?itemSchema.enum:[],supportedEnum=!schema.__uiUnsupported&&!schema.__uiNullable&&!itemSchema.__uiUnsupported&&!itemSchema.__uiNullable&&enumValues.length>0&&enumValues.every(value=>["string","number","boolean"].includes(typeof value)),current=new Set(Array.isArray(initial)?initial.map(value=>JSON.stringify(value)):[]);
        if(!supportedEnum)holder.dataset.configUnsupported="true";
        if(supportedEnum)enumValues.forEach(optionValue=>{const label=document.createElement("label");label.className="mini-check";const input=document.createElement("input");input.type="checkbox";input.dataset.configKey=key;input.dataset.configType="array";input.dataset.configValue=JSON.stringify(optionValue);input.value=String(optionValue);input.checked=current.has(JSON.stringify(optionValue));const span=document.createElement("span");span.textContent={CA:"CA 加拿大",UK:"UK 英国",AU:"AU 澳大利亚"}[String(optionValue)]||String(optionValue);label.append(input,document.createTextNode(" "),span);holder.append(label);});
      }else{
        const title=document.createElement("span");title.textContent=fieldLabel(key,schema)+(required.has(rawKey)?" *":"");holder.append(title);const scalarType=["string","number","integer","boolean"].includes(type),enumValues=Array.isArray(schema.enum)?schema.enum:null,enumSupported=!enumValues||(enumValues.length>0&&enumValues.every(value=>["string","number","boolean"].includes(typeof value))),nullableSupported=!schema.__uiNullable||Boolean(enumValues)||type==="boolean";
        if(schema.__uiUnsupported||!scalarType||!enumSupported||!nullableSupported){holder.dataset.configUnsupported="true";}
        else{let input;
          if(enumValues||(type==="boolean"&&schema.__uiNullable)){input=document.createElement("select");if(schema.__uiNullable){const empty=document.createElement("option");empty.value="__null";empty.dataset.configValue="null";empty.textContent="不设置";input.append(empty);}else if(!required.has(rawKey)&&initial===undefined){const empty=document.createElement("option");empty.value="";empty.textContent="请选择";input.append(empty);}(enumValues||[true,false]).forEach((value,index)=>{const option=document.createElement("option");option.value="option-"+index;option.dataset.configValue=JSON.stringify(value);option.textContent=String(value);input.append(option);});if(initial!==undefined&&initial!==null){const wanted=JSON.stringify(initial),match=[...input.options].find(option=>option.dataset.configValue===wanted);if(match)match.selected=true;else holder.dataset.configUnsupported="true";}}
          else{input=document.createElement("input");input.type=(type==="number"||type==="integer")?"number":type==="boolean"?"checkbox":"text";if(type==="integer")input.step="1";else if(type==="number")input.step=String(schema.multipleOf||"any");if(schema.minimum!==undefined)input.min=String(schema.minimum);if(schema.maximum!==undefined)input.max=String(schema.maximum);if(schema.minLength!==undefined)input.minLength=Number(schema.minLength);if(schema.maxLength!==undefined)input.maxLength=Number(schema.maxLength);if(typeof schema.pattern==="string")input.pattern=schema.pattern;}
          input.dataset.configKey=key;input.dataset.configType=type;input.dataset.configRequired=required.has(rawKey)?"true":"false";if(type==="boolean"&&!enumValues&&!schema.__uiNullable)input.checked=initial===undefined?false:Boolean(initial);else if(input.tagName!=="SELECT"&&initial!==undefined&&initial!==null)input.value=String(initial);holder.append(input);
        }
      }
      const help=document.createElement("small");help.className="fieldset-help";help.textContent=holder.dataset.configUnsupported==="true"?"当前页面不支持该字段结构，请更新程序后再创建。":String(schema.description||"批量排期会把相同配置应用到每一家店铺。");holder.append(help);container.append(holder);
    });
  }
  function collectWorkflowConfig(form){
    const meta=workflowMeta(form),config={},required=new Set(Array.isArray(meta?.config_schema?.required)?meta.config_schema.required:[]);let properties=workflowProperties(meta);if(!Object.keys(properties).length&&meta?.key==="amazon_disbursement")properties={marketplace_codes:{type:"array"}};
    Object.entries(properties).forEach(([rawKey,rawSchema])=>{const key=safeWorkflowKey(rawKey);if(!key)return;const schema=resolveWorkflowFieldSchema(meta,rawSchema),type=schema.type||(Array.isArray(schema.enum)?"string":"string"),fields=$$('[data-config-key="'+key+'"]',form);if(type==="array"){const selected=fields.filter(input=>input.checked).map(input=>{try{return input.dataset.configValue!==undefined?JSON.parse(input.dataset.configValue):input.value;}catch(_){return input.value;}});if(selected.length||required.has(rawKey)||(key==="marketplace_codes"&&meta?.requires_marketplace_targets!==false))config[key]=selected;return;}const field=fields[0];if(!field)return;const selectedOption=field.tagName==="SELECT"?field.selectedOptions?.[0]:null;if(selectedOption?.dataset.configValue!==undefined){try{config[key]=JSON.parse(selectedOption.dataset.configValue);return;}catch(_){/* fall through to normal parsing */}}if(type==="boolean"){if(field.tagName==="SELECT"&&!String(field.value||"").trim()&&!required.has(rawKey))return;config[key]=Boolean(field.checked);return;}const raw=String(field.value??"").trim();if(!raw&&!required.has(rawKey))return;if(type==="number"||type==="integer"){const numeric=Number(raw);config[key]=raw!==""&&Number.isFinite(numeric)?numeric:raw;}else config[key]=raw;});return config;
  }
  function workflowConfigError(form,config=collectWorkflowConfig(form)){
    const meta=workflowMeta(form),properties=workflowProperties(meta),required=new Set(Array.isArray(meta?.config_schema?.required)?meta.config_schema.required:[]);const unsupported=$("[data-config-unsupported=true]",form);if(unsupported)return"当前版本暂不支持该流程的配置字段，请更新程序。";
    for(const [rawKey,rawSchema] of Object.entries(properties)){const key=safeWorkflowKey(rawKey),schema=resolveWorkflowFieldSchema(meta,rawSchema),type=schema.type||(Array.isArray(schema.enum)?"string":"string"),value=config[key],missing=value===undefined||(value===null&&!schema.__uiNullable)||(typeof value==="string"&&!value.trim());if(required.has(rawKey)&&(missing||(Array.isArray(value)&&!value.length)))return"请填写流程参数："+fieldLabel(key,schema);if(missing)continue;if(type==="array"){if(schema.minItems!==undefined&&value.length<Number(schema.minItems))return fieldLabel(key,schema)+"至少选择 "+schema.minItems+" 项";if(schema.maxItems!==undefined&&value.length>Number(schema.maxItems))return fieldLabel(key,schema)+"最多选择 "+schema.maxItems+" 项";continue;}if(type==="number"||type==="integer"){if(value===null&&schema.__uiNullable)continue;const number=Number(value);if(!Number.isFinite(number)||(type==="integer"&&!Number.isInteger(number)))return fieldLabel(key,schema)+"必须填写有效"+(type==="integer"?"整数":"数字");if(schema.minimum!==undefined&&number<Number(schema.minimum))return fieldLabel(key,schema)+"不能小于 "+schema.minimum;if(schema.exclusiveMinimum!==undefined&&number<=Number(schema.exclusiveMinimum))return fieldLabel(key,schema)+"必须大于 "+schema.exclusiveMinimum;if(schema.maximum!==undefined&&number>Number(schema.maximum))return fieldLabel(key,schema)+"不能大于 "+schema.maximum;if(schema.exclusiveMaximum!==undefined&&number>=Number(schema.exclusiveMaximum))return fieldLabel(key,schema)+"必须小于 "+schema.exclusiveMaximum;if(schema.multipleOf!==undefined&&Math.abs(number/Number(schema.multipleOf)-Math.round(number/Number(schema.multipleOf)))>1e-9)return fieldLabel(key,schema)+"必须是 "+schema.multipleOf+" 的倍数";}else if(value!==null){const text=String(value);if(schema.minLength!==undefined&&text.length<Number(schema.minLength))return fieldLabel(key,schema)+"长度不能少于 "+schema.minLength;if(schema.maxLength!==undefined&&text.length>Number(schema.maxLength))return fieldLabel(key,schema)+"长度不能超过 "+schema.maxLength;if(typeof schema.pattern==="string"){try{if(!(new RegExp(schema.pattern)).test(text))return fieldLabel(key,schema)+"格式不正确";}catch(_){return"当前流程的字段规则无法解析，请更新程序。";}}}if(value!==null&&Array.isArray(schema.enum)&&!schema.enum.map(String).includes(String(value)))return fieldLabel(key,schema)+"选项无效";}
    return null;
  }
  function setWorkflowConfig(form,values){renderWorkflowConfig(form,values&&typeof values==="object"?values:{});}  function updateScheduleModeHelp(form){
    const meta=workflowMeta(form),modeSelect=$('[name="mode"]',form); if(modeSelect){const supported=Array.isArray(meta?.supported_modes)&&meta.supported_modes.length?meta.supported_modes:["dry_run"];const current=modeSelect.value;modeSelect.replaceChildren();supported.forEach(mode=>{const option=document.createElement("option");option.value=mode;option.textContent=scheduleModeShortLabels[mode]||mode;modeSelect.append(option);});modeSelect.value=supported.includes(current)?current:(supported.includes(meta?.default_mode)?meta.default_mode:supported[0]);}
    const help=$("[data-mode-help]",form);if(help)help.textContent=scheduleModeLabels[modeSelect?.value]||"按已注册流程规则执行。";renderWorkflowGuide(form,meta);
  }
  function refreshScheduleStoreEligibility(form){const needsIdentity=workflowMeta(form)?.requires_confirmed_identity!==false;$$('[data-store-option]',form).forEach(row=>{const state=$('[data-store-target-state]',row),enabled=row.dataset.storeEnabled!=="false",identity=row.dataset.identityConfirmed!=="false",eligible=enabled&&(!needsIdentity||identity);if(!state)return;state.textContent=eligible?"待预检":(!enabled?"店铺未启用":"卖家身份未确认");state.className="store-target-state"+(eligible?"":" bad");});}
  function renderWorkflowOptions(form,selectedKey){const select=$('[name="workflow"]',form);if(!select)return;select.replaceChildren();(scheduleWorkflows.length?scheduleWorkflows:fallbackScheduleWorkflows).forEach(meta=>{if(!meta?.key)return;const option=document.createElement("option");option.value=meta.key;option.textContent=meta.display_name||meta.key;option.title=meta.description||"";select.append(option);});if(selectedKey&&[...select.options].some(option=>option.value===selectedKey))select.value=selectedKey;updateScheduleModeHelp(form);renderWorkflowConfig(form);refreshScheduleStoreEligibility(form);}
  async function loadScheduleWorkflows(form,selectedKey){if(!scheduleWorkflowReady){scheduleWorkflowReady=api("/api/workflows").then(data=>{const list=Array.isArray(data)?data:(Array.isArray(data?.workflows)?data.workflows:[]);scheduleWorkflows=list.filter(item=>item&&item.key).map(item=>({...item,key:safeWorkflowKey(item.key)}));if(!scheduleWorkflows.length)scheduleWorkflows=fallbackScheduleWorkflows;return scheduleWorkflows;}).catch(()=>{scheduleWorkflows=fallbackScheduleWorkflows;return scheduleWorkflows;});}await scheduleWorkflowReady;const liveKey=selectedKey||$('[name="workflow"]',form)?.value,liveConfig=collectWorkflowConfig(form);renderWorkflowOptions(form,liveKey);if(Object.keys(liveConfig).length&&!form.dataset.scheduleEditing)setWorkflowConfig(form,liveConfig);}
  function setScheduleStep(form,step){const editing=form.dataset.scheduleEditing==="true";$$("[data-schedule-step]",form).forEach(section=>{section.hidden=editing?section.dataset.scheduleStep!=="1":section.dataset.scheduleStep!==String(step);});$$("[data-step-indicator]",form).forEach(item=>item.classList.toggle("active",item.dataset.stepIndicator===String(step)));const summary=$("[data-schedule-step-summary]",form);if(summary&&step===2&&!editing){const meta=workflowMeta(form),sites=collectWorkflowConfig(form).marketplace_codes||[];summary.textContent=(meta?.display_name||meta?.key||"已注册流程")+" · "+($('[name="mode"]',form)?.value||"dry_run")+" · "+sites.join(" / ")+"。下面选择要创建相同排期的店铺，提交前会逐家预检。";}form.dataset.scheduleStep=String(step);}
  function setScheduleRefreshPending(form,pending){if(pending)form.dataset.schedulerRefreshPending="true";else delete form.dataset.schedulerRefreshPending;$$('input,select,textarea,button',form).forEach(control=>{control.disabled=pending&&!control.matches('[data-schedule-submit]');});}
  function resetScheduleForm(form){setScheduleRefreshPending(form,false);form.reset();delete form.dataset.scheduleId;delete form.dataset.scheduleEditing;delete form.dataset.batchRequestId;invalidateSchedulePreview(form);const hiddenStore=$('[name="store_id"]',form);if(hiddenStore)hiddenStore.value="";$("[data-schedule-dialog-title]",form).textContent="新建任务排期";const submit=$("[data-schedule-submit]",form);if(submit){submit.disabled=false;submit.textContent="预检并创建排期";}const createNext=$("[data-schedule-create-next]",form);if(createNext)createNext.hidden=false;const editSubmit=$("[data-schedule-edit-submit]",form);if(editSubmit){editSubmit.hidden=true;editSubmit.disabled=false;}const indicator=$("[data-schedule-step-indicator]",form);if(indicator)indicator.hidden=false;const workflow=$('[name="workflow"]',form);if(workflow){workflow.disabled=false;workflow.value="amazon_disbursement";}renderWorkflowOptions(form,"amazon_disbursement");setCheckedValues(form,"run_days",defaultScheduleDays);$$('input[name="target_store_ids"]',form).forEach(input=>{input.checked=false;});const search=$('[data-store-search]',form);if(search)search.value="";$$('[data-store-option]',form).forEach(row=>{row.hidden=false;});refreshScheduleStoreEligibility(form);const preview=$("[data-batch-preview]",form);if(preview)preview.hidden=true;$("[data-form-error]",form).textContent="";setScheduleStep(form,1);loadScheduleWorkflows(form);}
  function openScheduleEditor(data){const dialog=$("#schedule-dialog"),form=$("[data-schedule-form]",dialog);resetScheduleForm(form);form.dataset.scheduleId=String(data.id);form.dataset.scheduleEditing="true";const lockedWorkflow=$('[name="workflow"]',form);if(lockedWorkflow)lockedWorkflow.disabled=true;const hiddenStore=$('[name="store_id"]',form);if(hiddenStore)hiddenStore.value=String(data.store_id||"");$("[data-schedule-dialog-title]",form).textContent="编辑任务排期";const createNext=$("[data-schedule-create-next]",form);if(createNext)createNext.hidden=true;const editSubmit=$("[data-schedule-edit-submit]",form);if(editSubmit){editSubmit.hidden=false;editSubmit.disabled=false;editSubmit.textContent="保存修改";}const indicator=$("[data-schedule-step-indicator]",form);if(indicator)indicator.hidden=true;for(const key of ["name","local_time","mode"]){const input=$('[name="'+key+'"]',form);if(input)input.value=data[key]??"";}$('[name="enabled"]',form).checked=!!data.enabled;const days=data.days_of_week==="*" ? ["mon","tue","wed","thu","fri","sat","sun"] : String(data.days_of_week||"").split(",").map(value=>value.trim()).filter(Boolean);setCheckedValues(form,"run_days",days);loadScheduleWorkflows(form,data.workflow||"amazon_disbursement").then(()=>{const meta=scheduleWorkflows.find(item=>item.key===String(data.workflow||"amazon_disbursement"));if(!meta){$("[data-form-error]",form).textContent="该排期使用的流程已不在代码白名单中，已禁止修改。";if(editSubmit)editSubmit.disabled=true;return;}const workflow=$('[name="workflow"]',form);if(workflow){workflow.value=data.workflow||"amazon_disbursement";workflow.disabled=true;}updateScheduleModeHelp(form);const description=$("[data-workflow-description]",form);if(description)description.textContent=(meta.description||meta.display_name||meta.key)+"（流程创建后不可更换）";setWorkflowConfig(form,data.workflow_config||{marketplace_codes:data.marketplace_codes||[]});});setScheduleStep(form,1);dialog.showModal();}
  function selectedScheduleStoreIds(form){return $$('input[name="target_store_ids"]:checked',form).map(input=>Number(input.value)).filter(validPositiveId);}
  function scheduleStoreRow(form, storeId){return $$("[data-store-option]", form).find(row => String(row.dataset.storeId || "") === String(storeId));}
  function updateSelectedStoreCount(form){const count=selectedScheduleStoreIds(form),node=$("[data-selected-store-count]",form);if(node)node.textContent="已选择 "+count.length+" 家";}
  function buildScheduleTemplate(form){const runDays=selectedValues(form,"run_days");return{name:String($('[name="name"]',form)?.value||"").trim(),workflow:String($('[name="workflow"]',form)?.value||""),mode:String($('[name="mode"]',form)?.value||""),workflow_config:collectWorkflowConfig(form),local_time:String($('[name="local_time"]',form)?.value||""),days_of_week:runDays.length===7?"*":runDays.join(","),timezone:"Asia/Singapore",enabled:Boolean($('[name="enabled"]',form)?.checked),misfire_grace_seconds:1800};}
  function buildBatchPayload(form){let requestId=form.dataset.batchRequestId;if(!requestId){requestId=window.crypto?.randomUUID?.()||"xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g,c=>{const r=Math.random()*16|0,v=c==="x"?r:(r&3)|8;return v.toString(16);});form.dataset.batchRequestId=requestId;}const template=buildScheduleTemplate(form),storeIds=selectedScheduleStoreIds(form);return{request_id:requestId,store_ids:storeIds,template};}
  function renderBatchPreview(form,data){
    const panel=$("[data-batch-preview]",form);if(!panel)return;const targets=Array.isArray(data?.targets)?data.targets:[],eligible=data?.eligible===true,eligibleCount=Number(data?.eligible_count??targets.filter(item=>item?.eligible===true).length),count=eligible?Number(data?.created_count??targets.length):0,meta=workflowMeta(form),sites=collectWorkflowConfig(form).marketplace_codes||[];panel.hidden=false;
    const countNode=$("[data-batch-preview-count]",panel);if(countNode)countNode.textContent="本次将创建 "+count+" 条";
    const message=$("[data-batch-preview-message]",panel);if(message)message.textContent=eligible?"所有选中的店铺均已通过预检，可以确认创建。":"预检未通过：已选 "+targets.length+" 家，其中 "+eligibleCount+" 家符合条件，本次仍会创建 0 条。请取消不合格店铺，或先完成店铺建档后再试。";
    const list=$("[data-batch-preview-list]",panel);if(!list)return;list.replaceChildren();
    targets.sort((a,b)=>Number(a.order||0)-Number(b.order||0)).forEach(item=>{const row=document.createElement("div");row.className="batch-preview-row "+(item.eligible?"ok":"bad");const name=document.createElement("b"),order=Number(item.order||0),siteCopy=sites.length?" · "+sites.join(" / "):" · 店铺级";name.textContent="#"+order+" "+String(item.store_name||("店铺 #"+item.store_id))+" · "+String(meta?.display_name||meta?.key||"流程")+siteCopy;const reason=document.createElement("span"),reasons=Array.isArray(item.reasons)?item.reasons:[];reason.textContent=item.eligible?"符合条件":(reasons.join("；")||"不符合该流程要求");row.append(name,reason);list.append(row);const targetRow=scheduleStoreRow(form,item.store_id),state=targetRow&&$("[data-store-target-state]",targetRow);if(state){state.textContent=item.eligible?"可创建":"需处理";state.className="store-target-state "+(item.eligible?"ok":"bad");}});
    form.dataset.batchPreviewHash=String(data.definition_hash||"");form.dataset.previewed=eligible?"true":"false";const submit=$("[data-schedule-submit]",form);if(submit)submit.textContent=eligible?"确认创建 "+count+" 条排期":"重新预检";
  }  async function previewBatchSchedule(form){const error=$("[data-form-error]",form),storeIds=selectedScheduleStoreIds(form);if(!storeIds.length){error.textContent="请至少选择一家店铺。";setScheduleStep(form,2);return false;}const nonce=invalidateSchedulePreview(form),payload=buildBatchPayload(form),payloadJson=JSON.stringify(payload),button=$("[data-schedule-submit]",form);if(button){button.disabled=true;button.textContent="预检中…";}try{const result=await api("/api/schedules/batch/preview",{method:"POST",body:payloadJson});if(form.dataset.previewNonce!==nonce)return false;form.dataset.batchPreviewPayload=payloadJson;renderBatchPreview(form,result);return result?.eligible===true;}catch(exc){if(form.dataset.previewNonce===nonce)error.textContent=exc.message;return false;}finally{if(button&&form.dataset.previewNonce===nonce)button.disabled=false;}}
  async function createBatchSchedule(form,payloadJson){const error=$("[data-form-error]",form),button=$("[data-schedule-submit]",form);if(button){button.disabled=true;button.textContent="正在创建…";}try{const result=await api("/api/schedules/batch",{method:"POST",body:payloadJson}),count=Number(result.created_count||result.schedules?.length||0),refreshWarning=result.scheduler_refreshed===false?String(result.warning||"排期已保存，但定时器尚未刷新。请在本页点击重试，不要重新新建。"):"";toast(refreshWarning||(result.status==="existing"?"相同批次已存在，未重复创建":`已创建 ${count} 条排期`),Boolean(refreshWarning));if(refreshWarning){setScheduleRefreshPending(form,true);error.textContent=refreshWarning;if(button){button.disabled=false;button.textContent="重试刷新定时器";}return false;}setScheduleRefreshPending(form,false);setTimeout(()=>location.reload(),650);return true;}catch(exc){if(exc.detail&&typeof exc.detail==="object"&&Array.isArray(exc.detail.targets))renderBatchPreview(form,exc.detail);error.textContent=exc.message;if(button){button.disabled=false;button.textContent=form.dataset.schedulerRefreshPending==="true"?"重试刷新定时器":"确认创建排期";}return false;}}

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
      if (form?.dataset.schedulerRefreshPending === "true") {
        const error = $("[data-form-error]", form);
        if (error) error.textContent = "排期已经保存，请先点击“重试刷新定时器”；也可以直接重启后台。为避免重复排期，本窗口暂不关闭。";
        return;
      }
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
      dialog.showModal();
      restoreActiveStoreSetupProbe(form, data.id);
      return;
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
      } catch (exc) {
        if (isZiniaoCredentialError(exc)) pauseStoreSetupForCredential(form, exc);
        else failStoreSetup(form, exc.message);
      } finally {
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
          target.hidden = false;
          target.disabled = false;
          target.textContent = "再次尝试自动登录并继续";
          ui.cancel.disabled = false;
        } else if (form.dataset.storeSetupProbeId !== probeId) {
          // Only tear the panel down once this probe is genuinely gone. It is
          // still tracked whenever polling gave up on a probe that keeps
          // running, and hiding the panel then would take the cancel button
          // away at exactly the moment the operator needs it.
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
        // Abandoning mid-flight would strand a live Ziniao window; 「跳过此店并继续」
        // is the right control once the probe reports in.
        { const ui = bulkSetupUi(); if (ui.dismiss) ui.dismiss.hidden = true; }
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
      const ui = bulkSetupUi(); ui.panel.hidden = false; ui.actions.hidden = true; if (ui.dismiss) ui.dismiss.hidden = true; target.disabled = true; target.textContent = "批量建档进行中…"; updateBulkSetupCounts();
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
    if (action === "discard-bulk-store-setup") {
      const name = bulkStoreSetup.queue[bulkStoreSetup.index]?.name || "当前店铺";
      if (!(await confirmAction(`放弃这个未完成的队列吗？停在“${name}”的进度和上面的计数都会清除，已经建好的档不受影响；之后点“一键自动建档全部店铺”会按当前卡片重新挑选店铺。`))) return;
      discardBulkStoreSetup();
      toast("已放弃未完成队列");
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
    if (action === "schedule-next-step") {
      const form = target.closest("[data-schedule-form]"); if (!form) return;
      const error = $("[data-form-error]", form); error.textContent = "";
      const runDays = selectedValues(form, "run_days"), config = collectWorkflowConfig(form);
      if (!workflowMeta(form)?.key) { error.textContent = "请选择一个已注册的自动化流程。"; return; }
      if (!String($('[name="name"]', form)?.value || "").trim()) { error.textContent = "请填写排期名称。"; return; }
      if (!String($('[name="local_time"]', form)?.value || "")) { error.textContent = "请选择执行时间。"; return; }
      if (!runDays.length) { error.textContent = "请至少选择一个每周运行日。"; return; }
      const configError = workflowConfigError(form, config);
      if (configError) { error.textContent = configError; return; }
      if (workflowMeta(form)?.requires_marketplace_targets !== false && (!Array.isArray(config.marketplace_codes) || !config.marketplace_codes.length)) { error.textContent = "请至少勾选一个站点。"; return; }
      setScheduleStep(form, 2); updateSelectedStoreCount(form); return;
    }
    if (action === "schedule-prev-step") {
      const form = target.closest("[data-schedule-form]"); if (form) { setScheduleStep(form, 1); $("[data-form-error]", form).textContent = ""; } return;
    }
    if (action === "select-all-schedule-stores") {
      const form = target.closest("[data-schedule-form]"); if (!form) return;
      $$("[data-store-option]", form).filter(row => !row.hidden).forEach(row => { const input = $('input[name="target_store_ids"]', row); if (input) input.checked = true; });
      updateSelectedStoreCount(form); delete form.dataset.previewed; delete form.dataset.batchPreviewHash; return;
    }
    if (action === "clear-schedule-stores") {
      const form = target.closest("[data-schedule-form]"); if (!form) return;
      $$('input[name="target_store_ids"]', form).forEach(input => { input.checked = false; });
      updateSelectedStoreCount(form); delete form.dataset.previewed; delete form.dataset.batchPreviewHash; const preview = $("[data-batch-preview]", form); if (preview) preview.hidden = true; return;
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
      try {
        const data = await api(`/api/schedules/${id}`, {method:"DELETE", body:"{}"});
        if (data.scheduler_refreshed === false) {
          const warning = String(data.warning || "排期已从数据库删除，但定时器尚未应用这次变更；请不要重复删除，重启后台后会自动重新加载。");
          toast(warning, true);
          target.textContent = "已删除 · 定时器待重载";
          // Keep the warning visible before refreshing the now-stale row. The
          // disabled button also prevents a second DELETE while SQLite already
          // contains the authoritative deletion.
          setTimeout(() => location.reload(), 4500);
          return;
        }
        toast("排期已删除"); setTimeout(() => location.reload(), 400);
      }
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
      const map = {approve:"approve", cancel:"cancel", reconcile:"reconcile", "continue-auth":"continue-auth", settle:"settle"};
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
  $("#schedule-dialog")?.addEventListener("cancel", event => {
    const form=$("[data-schedule-form]",event.currentTarget);if(form?.dataset.schedulerRefreshPending==="true"){event.preventDefault();const error=$("[data-form-error]",form);if(error)error.textContent="排期已经保存，请先重试刷新定时器，避免重复创建。";}
  });
  $("[name=expected_seller_id]", storeEditor || document)?.addEventListener("input", event => {
    const form = event.currentTarget.form;
    if (!form || normalizedSellerIdentity(event.currentTarget.value) === normalizedSellerIdentity(form.dataset.persistedSellerId)) return;
    $("[name=identity_confirmed]", form).checked = false;
  });
  $("[data-run-form]")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget, error = $("[data-form-error]", form); error.textContent = "";
    const data = formData(form); data.store_id = Number(data.store_id);
    try { const run = await api("/api/runs", {method:"POST", body:JSON.stringify(data)}); location.href = `/runs/${run.id}`; } catch (exc) { error.textContent = exc.message; }
  });
  $("[data-schedule-form]")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget, error = $("[data-form-error]", form); error.textContent = "";
    const editingId = Number(form.dataset.scheduleId || 0);
    if (!editingId && form.dataset.schedulerRefreshPending === "true") { const savedPayload = form.dataset.batchPreviewPayload; if (!savedPayload) { error.textContent = "已保存批次的重试信息丢失，请刷新页面；数据库不会重复创建。"; return; } await createBatchSchedule(form, savedPayload); return; }
    const runDays = selectedValues(form, "run_days"), config = collectWorkflowConfig(form);
    if (!runDays.length) { error.textContent = "请至少选择一个每周运行日。"; setScheduleStep(form, 1); return; }
    const configError = workflowConfigError(form, config);
    if (configError) { error.textContent = configError; setScheduleStep(form, 1); return; }
    if (workflowMeta(form)?.requires_marketplace_targets !== false && (!Array.isArray(config.marketplace_codes) || !config.marketplace_codes.length)) { error.textContent = "请至少勾选一个站点。"; setScheduleStep(form, 1); return; }
    if (!editingId) {
      if (!workflowMeta(form)?.key) { error.textContent = "请选择一个已注册的自动化流程。"; setScheduleStep(form, 1); return; }
      if (!selectedScheduleStoreIds(form).length) { error.textContent = "请至少选择一家店铺。"; setScheduleStep(form, 2); return; }
      setScheduleStep(form, 2);
      if (form.dataset.previewed !== "true") { await previewBatchSchedule(form); return; }
      const payload = buildBatchPayload(form), payloadJson = JSON.stringify(payload);
      if (form.dataset.batchPreviewPayload !== payloadJson) { invalidateSchedulePreview(form); await previewBatchSchedule(form); return; }
      await createBatchSchedule(form, payloadJson);
      return;
    }
    const data = {name:String($("[name=name]",form)?.value || "").trim(), mode:String($("[name=mode]",form)?.value || ""), workflow_config:config, marketplace_codes:config.marketplace_codes || [], days_of_week:runDays.length===7?"*":runDays.join(","), local_time:$('[name="local_time"]',form).value, timezone:"Asia/Singapore", enabled:Boolean($('[name="enabled"]',form).checked)};
    const button = $("[data-schedule-edit-submit]", form); if (button) button.disabled = true;
    try {
      const result = await api(`/api/schedules/${editingId}`, {method:"PATCH", body:JSON.stringify(data)});
      if (result.scheduler_refreshed === false) {
        const warning = String(result.warning || "排期修改已保存到数据库，但定时器尚未应用；请不要重复提交，重启后台后会自动重新加载。");
        error.textContent = warning;
        toast(warning, true);
        // The durable PATCH has already succeeded. Leave the submit control
        // disabled so an operator cannot mistake projection lag for a failed
        // save and replay the mutation.
        if (button) button.textContent = "已保存 · 定时器待重载";
        return;
      }
      toast("排期修改已保存"); setTimeout(() => location.reload(), 500);
    }
    catch (exc) { error.textContent = exc.message; if (button) button.disabled = false; }
  });
  $('[data-schedule-mode]')?.addEventListener("change", event => { updateScheduleModeHelp(event.currentTarget.form); delete event.currentTarget.form.dataset.previewed; });
  $('[data-schedule-workflow]')?.addEventListener("change", event => { renderWorkflowOptions(event.currentTarget.form, event.currentTarget.value); delete event.currentTarget.form.dataset.previewed; });
  $('[data-schedule-form]')?.addEventListener("input", event => {
    const form = event.currentTarget;
    if (event.target.matches('[data-store-search]')) return;
    invalidateSchedulePreview(form);
    const preview = $("[data-batch-preview]", form); if (preview) preview.hidden = true;
    updateSelectedStoreCount(form);
  });
  $('[data-schedule-form]')?.addEventListener("change", event => {
    if (event.target.matches('[data-store-search]')) return;
    const form = event.currentTarget;
    invalidateSchedulePreview(form);
    const preview = $("[data-batch-preview]", form); if (preview) preview.hidden = true;
    updateSelectedStoreCount(form);
  });
  $('[data-store-search]')?.addEventListener("input", event => {
    const query = String(event.currentTarget.value || "").trim().toLowerCase();
    $$("[data-store-option]", event.currentTarget.form).forEach(row => { row.hidden = query && !String(row.dataset.storeName || "").toLowerCase().includes(query); });
  });
  // Guarded because this file is one bundle for every page.  The schedule form
  // exists only on /schedules, and `$$`'s `root = document` default does NOT
  // apply to an explicit null — only to undefined — so passing the missing form
  // reached `null.querySelectorAll` and threw.  Everything below this line then
  // never ran, including the credentials form's submit handler: the page looked
  // fine, but saving fell back to a native form POST, so it reloaded to the top
  // with no request, no toast and nothing in the log.
  const scheduleForm = $("[data-schedule-form]");
  if (scheduleForm) updateSelectedStoreCount(scheduleForm);
  if ($('[data-action="detect-all-store-setups"]')) {
    restoreBulkStoreSetup();
    if (!bulkStoreSetup.queue.length) restoreBulkSetupCompletionSummary();
  }
  // A page-specific block that throws must not silently disable every block
  // after it.  One bundle serves every page, so the failure lands on a screen
  // that has nothing to do with the broken code — and the symptom is a form
  // quietly reverting to a native submit, which looks like "the button does
  // nothing" rather than like an error.
  window.addEventListener("error", event => {
    console.error("[ziniao] 页面脚本出错，部分按钮可能失效", event.error || event.message);
  });
  // ---- 系统诊断页：WebDriver 切换与凭据配置 ----------------------------------
  (function diagnosticsPage() {
    const webdriverButton = $('[data-action="start-webdriver"]');
    const webdriverState = $("[data-webdriver-state]");

    function say(text, bad) {
      if (!webdriverState) return;
      webdriverState.textContent = text;
      webdriverState.className = "diagnostic-actions-state" + (bad ? " bad" : "");
    }

    webdriverButton?.addEventListener("click", async () => {
      // Naming the consequence beats a generic "确定执行此操作？": the operator
      // may well have store windows open right now with work in them.
      const agreed = await confirmAction(
        "切换到 WebDriver 模式会强制关闭当前所有已打开的紫鸟店铺窗口。\n\n" +
        "如果有店铺窗口里正在人工处理验证，请先处理完。确定继续吗？"
      );
      if (!agreed) return;
      webdriverButton.disabled = true;
      const original = webdriverButton.textContent;
      webdriverButton.textContent = "正在切换…";
      say("正在关闭旧窗口并启动 WebDriver 模式，最长等待 60 秒…");
      try {
        const data = await api("/api/ziniao/webdriver/start", {method:"POST", body:"{}"});
        say(data.message || "已就绪");
        toast("紫鸟 WebDriver 模式已就绪");
        setTimeout(() => location.reload(), 1200);
      } catch (exc) {
        // The server's refusal text names which store or guard is in the way;
        // replacing it with something generic would remove the only actionable
        // part of the message.
        say(exc.message, true);
        toast(exc.message, true);
        webdriverButton.disabled = false;
        webdriverButton.textContent = original;
      }
    });

    function bindSettings(kind, endpoint, successText) {
      const form = $(`[data-settings-form="${kind}"]`);
      if (!form) return;
      // Bound to the button's click, not the form's submit.  A submit handler
      // is only as good as the script that installs it: when an unrelated
      // page-specific block threw earlier in this bundle, the handler never
      // bound and the browser fell back to a native submit — the page reloaded
      // to the top, no request was sent, and the operator was left with a
      // button that appeared to do nothing.  The button is now type="button",
      // so the worst case is an inert control rather than a lost form.
      $(`[data-settings-save="${kind}"]`, form)?.addEventListener("click", async event => {
        const error = $("[data-form-error]", form);
        // The clicked control itself.  Looking it up by `button[type="submit"]`
        // stopped working the moment these became type="button" (so that a
        // script failure degrades to an inert button instead of a native form
        // submit) — the lookup returned null and `submit.disabled = true` threw
        // on every click, which is the same "button does nothing" symptom the
        // type change was meant to prevent.
        const submit = event.currentTarget;
        const payload = {};
        $$("input[name]", form).forEach(input => { payload[input.name] = input.value.trim(); });
        if (Object.values(payload).some(value => !value)) {
          if (error) error.textContent = "所有字段都必须填写；密码类字段不会回显，修改时请重新输入完整值。";
          return;
        }
        if (error) error.textContent = "";
        submit.disabled = true;
        const original = submit.textContent;
        submit.textContent = "保存中…";
        try {
          await api(endpoint, {method:"POST", body:JSON.stringify(payload)});
          toast(successText);
          // Clear the secret from the DOM straight after a successful save so
          // it does not sit in the page until the reload lands.
          $$('input[type="password"]', form).forEach(input => { input.value = ""; });
          setTimeout(() => location.reload(), 800);
        } catch (exc) {
          if (error) error.textContent = exc.message;
          toast(exc.message, true);
          submit.disabled = false;
          submit.textContent = original;
        }
      });
    }

    bindSettings("ziniao", "/api/settings/ziniao", "紫鸟账号已保存到 Windows 凭据管理器");
    bindSettings("feishu", "/api/settings/feishu", "飞书配置已保存到 Windows 凭据管理器");

    $('[data-action="test-feishu"]')?.addEventListener("click", async event => {
      const button = event.currentTarget;
      const error = $("[data-form-error]", button.closest("form"));
      button.disabled = true;
      const original = button.textContent;
      button.textContent = "发送中…";
      try {
        await api("/api/settings/feishu/test", {method:"POST", body:"{}"});
        error.textContent = "";
        toast("测试消息已发出，请到飞书群里确认收到");
      } catch (exc) {
        error.textContent = exc.message;
        toast(exc.message, true);
      } finally {
        button.disabled = false;
        button.textContent = original;
      }
    });
  })();
})();
