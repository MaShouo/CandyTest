(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const state = { webdav: null, syncBusy: false };

  function flash(message, isError = false) { const el = $("#flash"); el.textContent = message || ""; el.classList.toggle("error", isError); }
  async function api(url, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
    if (csrf && ["POST", "PUT", "PATCH", "DELETE"].includes(method)) headers["X-CSRF-Token"] = csrf;
    const response = await fetch(url, { ...options, headers }); const data = await response.json().catch(() => ({}));
    if (response.status === 401) window.location.assign("/login");
    if (!response.ok) { const error = new Error(data.error?.message || `请求失败 (${response.status})`); error.code = data.error?.code || "HTTP_ERROR"; throw error; }
    return data;
  }
  function shortRevision(value) { return value ? `${value.slice(0, 8)}…` : "无"; }
  function webdavLastText(last) {
    if (!last) return "尚未执行同步。";
    const names = { pull: "Pull", push: "Push", test: "连接测试" }, statuses = { completed: "完成", failed: "失败" };
    const suffix = last.result?.revision ? ` · revision ${shortRevision(last.result.revision)}` : last.error?.message ? ` · ${last.error.message}` : "";
    return `${names[last.operation] || last.operation}：${statuses[last.status] || last.status}${suffix}`;
  }
  function renderWebdav(settings, last = null) {
    state.webdav = settings;
    $("#webdavServerUrl").value = settings.server_url || ""; $("#webdavRemotePath").value = settings.remote_path || ""; $("#webdavUsername").value = settings.username || "";
    const password = $("#webdavPassword"); password.value = ""; password.placeholder = settings.password_saved ? "已保存；留空则保留" : "请输入 WebDAV 密码";
    const badge = $("#webdavStatus"); badge.textContent = settings.configured ? "已配置" : "未配置"; badge.className = `badge ${settings.configured ? "ok" : ""}`;
    $("#webdavInfo").textContent = `${webdavLastText(last)} · 本机已见 revision：${shortRevision(settings.last_seen_revision)}`;
  }
  function webdavBody() { const form = $("#webdavForm"); return { server_url: form.server_url.value, remote_path: form.remote_path.value, username: form.username.value, password: form.password.value }; }
  async function saveWebdav(showMessage = true) { const data = await api("/api/settings/webdav", { method: "PUT", body: JSON.stringify(webdavBody()) }); renderWebdav(data.webdav); if (showMessage) flash("WebDAV 设置已保存。"); }
  async function loadWebdav() { const data = await api("/api/settings/webdav"); renderWebdav(data.webdav, data.last_operation); }
  function setSyncBusy(busy) { state.syncBusy = busy; for (const element of $("#webdavForm").elements) element.disabled = busy; }
  function renderWebdavRemote(status, last = null) {
    const badge = $("#webdavStatus"); badge.textContent = !status.configured ? "未配置" : status.reachable ? status.conflict ? "有冲突" : "远端可用" : "连接失败"; badge.className = `badge ${status.reachable && !status.conflict ? "ok" : status.configured ? "error" : ""}`;
    const remote = `远端 revision：${shortRevision(status.remote_revision)} · 本机已见：${shortRevision(status.last_seen_revision)}`;
    $("#webdavInfo").textContent = `${remote}${status.error ? ` · ${status.error.message}` : ""}${last ? ` · ${webdavLastText(last)}` : ""}`;
  }
  function renderProxy(proxy) { $("#proxyEnabled").checked = proxy.enabled; $("#proxyUrl").value = proxy.url || ""; $("#proxyUrl").required = proxy.enabled; const status = $("#proxyStatus"); status.textContent = proxy.enabled ? "代理已启用" : "未启用"; status.className = `badge ${proxy.enabled ? "ok" : ""}`; }
  async function loadProxy() { renderProxy((await api("/api/settings/proxy")).proxy); }
  function renderAccount(account) {
    $("#accountUsername").value = account.username || "";
    const warning = $("#accountDefaultWarning");
    warning.hidden = !account.default_credentials;
  }
  async function loadAccount() { renderAccount((await api("/api/settings/account")).account); }

  function selectSettingsSection(section) {
    const tabs = [...document.querySelectorAll(".settings-tab")];
    const selected = tabs.find(tab => tab.dataset.section === section) || tabs[0];
    for (const panel of document.querySelectorAll(".settings-section")) panel.hidden = panel.id !== `${selected.dataset.section}Settings`;
    for (const tab of tabs) {
      const active = tab === selected;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    }
  }
  for (const tab of document.querySelectorAll(".settings-tab")) tab.onclick = () => selectSettingsSection(tab.dataset.section);
  selectSettingsSection(new URLSearchParams(window.location.search).get("section") || "webdav");
  $("#webdavForm").onsubmit = async (event) => { event.preventDefault(); try { await saveWebdav(); } catch (error) { flash(error.message, true); } };
  $("#testWebdav").onclick = async () => { setSyncBusy(true); try { await saveWebdav(false); const data = await api("/api/webdav/test", { method: "POST", body: "{}" }); renderWebdavRemote(data.status); flash("WebDAV 读写测试通过。"); } catch (error) { flash(error.message, true); } finally { setSyncBusy(false); } };
  $("#refreshWebdav").onclick = async () => { setSyncBusy(true); try { const data = await api("/api/webdav/status"); renderWebdavRemote(data.status, data.last_operation); flash(data.status.reachable ? "已刷新 WebDAV 状态。" : data.status.error?.message || "远端尚无同步数据。"); } catch (error) { flash(error.message, true); } finally { setSyncBusy(false); } };
  $("#proxyEnabled").onchange = (event) => { $("#proxyUrl").required = event.currentTarget.checked; };
  $("#proxyForm").onsubmit = async (event) => { event.preventDefault(); const form = event.currentTarget; try { const data = await api("/api/settings/proxy", { method: "PUT", body: JSON.stringify({ enabled: form.enabled.checked, url: form.url.value }) }); renderProxy(data.proxy); flash(data.proxy.enabled ? "Clash 代理已启用，将应用于之后启动的测试。" : "代理已关闭，之后启动的测试将直接连接。"); } catch (error) { flash(error.message, true); } };
  const accountForm = $("#accountForm");
  if (accountForm) accountForm.onsubmit = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (form.new_password.value !== form.new_password_confirmation.value) {
      flash("两次输入的新密码不一致。", true); return;
    }
    try {
      const data = await api("/api/settings/account", { method: "PUT", body: JSON.stringify({
        username: form.username.value, current_password: form.current_password.value,
        new_password: form.new_password.value, new_password_confirmation: form.new_password_confirmation.value,
      }) });
      renderAccount(data.account); form.current_password.value = ""; form.new_password.value = ""; form.new_password_confirmation.value = "";
      flash("登录账号已保存；其他旧会话已失效。");
    } catch (error) { flash(error.message, true); }
  };
  const loads = [loadWebdav(), loadProxy()];
  if (accountForm) loads.push(loadAccount());
  Promise.all(loads).catch(error => flash(error.message, true));
})();
