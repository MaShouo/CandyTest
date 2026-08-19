(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const state = { gateways: [], history: new Map(), runtime: null, webdav: null, syncBusy: false, editing: null, currentId: null, currentJob: null, currentJobId: null, selectedGatewayIds: new Set() };
  const efforts = { pi: ["off", "minimal", "low", "medium", "high", "xhigh", "max"], codex: ["low", "medium", "high", "xhigh", "max", "ultra"] };

  function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
  function clear(el, children = []) { el.replaceChildren(...children); }
  function pct(value) { return value == null ? "—" : `${value.toFixed ? value.toFixed(1) : value}%`; }
  function num(value) { return value == null ? "—" : String(value); }
  function flash(message, isError = false) { const el = $("#flash"); el.textContent = message || ""; el.classList.toggle("error", isError); }
  async function api(url, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
    if (csrf && ["POST", "PUT", "PATCH", "DELETE"].includes(method)) headers["X-CSRF-Token"] = csrf;
    const opts = { ...options, headers };
    const response = await fetch(url, opts); const data = await response.json().catch(() => ({}));
    if (response.status === 401) { window.location.assign("/login"); }
    if (!response.ok) { const error = new Error(data.error?.message || `请求失败 (${response.status})`); error.code = data.error?.code || "HTTP_ERROR"; error.status = response.status; throw error; }
    return data;
  }
  function accuracyBadge(value) { const low = value != null && value < 80; return node("span", pct(value), `badge ${low ? "error" : "ok"}`); }
  function runVisual(run) {
    if (run.status === "error") return run.error_kind === "gateway_unavailable" ? { text: "中转站不可用", className: "warn" } : { text: "API 错误", className: "warn" };
    if (run.status === "cancelled") return { text: "已中断", className: "warn" };
    return { text: run.is_correct ? "正确" : "错误", className: "" };
  }
  function jobStatus(status) { return ({ queued: "排队中", running: "运行中", cancelling: "正在中断", cancelled: "已中断", completed: "已完成", failed: "失败" })[status] || status; }
  function updateJobControls(job) {
    const active = Boolean(job && ["queued", "running", "cancelling"].includes(job.status));
    const stop = $("#stopJob");
    stop.hidden = !active;
    stop.disabled = job?.status === "cancelling";
    stop.textContent = job?.status === "cancelling" ? "正在中断…" : "中断测试";
    const engines = state.runtime?.engines || {};
    $("#startJob").disabled = active || (!engines.pi && !engines.codex);
  }
  function openDetailKeys(container) { return new Set([...container.querySelectorAll("details[open][data-detail-key]")].map(item => item.dataset.detailKey)); }
  function details(answer, error, key, opened = new Set()) { const d = document.createElement("details"), s = node("summary", "查看"); d.dataset.detailKey = key; d.open = opened.has(key); d.append(s); if (answer) { d.append(node("strong", "回答全文")); d.append(node("pre", answer)); } if (error) { d.append(node("strong", "错误详情", "warn")); d.append(node("pre", error)); } if (!answer && !error) d.append(node("span", "无额外详情")); return d; }
  function setEfforts() { const select = $("#effort"), previous = select.value || "low"; clear(select, efforts[$("#engine").value].map(value => { const o = node("option", value); o.value = value; if (value === previous || (!efforts[$("#engine").value].includes(previous) && value === "low")) o.selected = true; return o; })); }
  function renderEngines() { const el = $("#engineStatus"); const engines = state.runtime.engines; clear(el, ["pi", "codex"].map(name => node("span", `${name}: ${engines[name] ? "可用" : "未安装"}`, `badge ${engines[name] ? "ok" : "error"}`))); const select = $("#engine"); for (const option of select.options) option.disabled = !engines[option.value]; if (select.selectedOptions[0]?.disabled) select.value = engines.pi ? "pi" : "codex"; setEfforts(); $("#startJob").disabled = !engines.pi && !engines.codex; }
  function shortRevision(value) { return value ? `${value.slice(0, 8)}…` : "无"; }
  function renderWebdav(settings) { state.webdav = settings; updateSyncControls(); }
  async function loadWebdav() { const data = await api("/api/settings/webdav"); renderWebdav(data.webdav); }
  function updateSyncControls() { for (const id of ["#pushWebdav", "#pullWebdav"]) $(id).disabled = state.syncBusy || !state.webdav?.configured; }
  function setSyncBusy(busy) { state.syncBusy = busy; updateSyncControls(); }
  function renderGateways() {
    const tbody = $("#gatewayRows");
    const selected = new Set([...tbody.querySelectorAll("input[type=checkbox]:checked")].map(item => Number(item.value)));
    const rows = [];
    for (const gateway of state.gateways) {
      const tr = document.createElement("tr"); const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.value = gateway.id; checkbox.disabled = !gateway.enabled; checkbox.checked = gateway.enabled && selected.has(gateway.id); tr.append(node("td")); tr.firstChild.append(checkbox);
      tr.append(node("td", gateway.name + (gateway.enabled ? "" : "（已停用）")), node("td", gateway.base_url + (gateway.base_url.startsWith("http://") ? " ⚠ 不安全 HTTP" : "")), node("td", gateway.model), node("td", gateway.api_key_saved ? `已保存 ${gateway.api_key_masked || "••••••••"}` : "未保存"));
      const h = state.history.get(gateway.id); const stat = node("td"); stat.append(accuracyBadge(h?.accuracy ?? null)); tr.append(stat);
      const actions = node("td"), group = node("div", undefined, "row-actions"); const edit = node("button", "编辑", "secondary"); edit.type = "button"; edit.onclick = () => showGateway(gateway); const remove = node("button", "删除", "secondary danger"); remove.type = "button"; remove.onclick = () => removeGateway(gateway); group.append(edit, remove); actions.append(group); tr.append(actions); rows.push(tr);
    }
    if (!state.gateways.length) { const tr = document.createElement("tr"), td = node("td", "尚未添加中转站", "empty"); td.colSpan = 7; tr.append(td); rows.length = 0; rows.push(tr); }
    clear(tbody, rows);
    syncGatewaySelectAll();
  }
  function syncGatewaySelectAll() {
    const selectAll = $("#selectAllGateways");
    const enabled = [...document.querySelectorAll("#gatewayRows input[type=checkbox]:not(:disabled)")];
    const checked = enabled.filter(item => item.checked).length;
    selectAll.disabled = enabled.length === 0;
    selectAll.checked = enabled.length > 0 && checked === enabled.length;
    selectAll.indeterminate = checked > 0 && checked < enabled.length;
  }
  function showGateway(gateway) { state.editing = gateway || null; const form = $("#gatewayForm"); form.reset(); $("#gatewayTitle").textContent = gateway ? `编辑：${gateway.name}` : "添加中转站"; if (gateway) { form.name.value = gateway.name; form.base_url.value = gateway.base_url; form.model.value = gateway.model; form.enabled.checked = gateway.enabled; } $("#gatewayPanel").hidden = false; form.name.focus(); }
  async function removeGateway(gateway) { if (!confirm(`删除“${gateway.name}”？历史测试记录会保留。`)) return; try { await api(`/api/gateways/${gateway.id}`, { method: "DELETE" }); flash("中转站已删除。"); await loadGateways(); } catch (e) { flash(e.message, true); } }
  function statCard(site, historical = false, rounds = null, selectable = false) {
    const currentLow = site.accuracy != null && site.accuracy < 80;
    const historyLow = !historical && site.historical_accuracy != null && site.historical_accuracy < 80;
    const low = currentLow || historyLow;
    const selected = selectable && state.selectedGatewayIds.has(Number(site.gateway_id));
    const classes = ["stat"];
    if (low) classes.push("low");
    if (selectable) classes.push("stat-selectable");
    if (selected) classes.push("selected");
    if (selectable && state.selectedGatewayIds.size > 0 && !selected) classes.push("not-selected");
    const div = node("article", undefined, classes.join(" "));
    if (selectable) {
      div.dataset.gatewayId = String(site.gateway_id);
      div.tabIndex = 0;
      div.setAttribute("role", "button");
      div.setAttribute("aria-pressed", String(selected));
      div.setAttribute("aria-label", `${site.gateway_name || site.name}筛选${selected ? "已选中" : "未选中"}`);
    }
    div.append(node("h3", site.gateway_name || site.name));
    div.append(node("strong", pct(site.accuracy)));
    const statDetails = node("div", undefined, "stat-details");
    if (!historical) statDetails.append(node("p", `进度 ${site.completed || 0} / ${rounds ?? "—"}`));
    if (historical) {
      statDetails.append(node("p", `正确 ${site.correct || 0} / 已判分 ${site.graded || 0} · API 错误 ${site.errors || 0} · 中断 ${site.cancelled || 0}`));
    } else {
      statDetails.append(node("p", `本任务：正确 ${site.correct || 0} / 已判分 ${site.graded || 0} · API 错误 ${site.errors || 0} · 中断 ${site.cancelled || 0}`));
      statDetails.append(node("p", `历史：${pct(site.historical_accuracy)}（${site.historical_correct || 0} / ${site.historical_graded || 0} · API 错误 ${site.historical_errors || 0} · 中断 ${site.historical_cancelled || 0}）`));
    }
    div.append(statDetails);
    const alerts = node("div", undefined, "stat-alerts");
    if (currentLow) alerts.append(node("p", historical ? "历史正确率低于 80%，请重点复核。" : "当前正确率低于 80%，请重点复核。", "warn"));
    if (historyLow) alerts.append(node("p", "历史正确率低于 80%，请重点复核。", "warn"));
    div.append(alerts);
    return div;
  }
  function toggleGatewayFilter(stat) {
    const gatewayId = Number(stat.dataset.gatewayId);
    if (!Number.isInteger(gatewayId) || !state.currentJob) return;
    if (state.selectedGatewayIds.has(gatewayId)) state.selectedGatewayIds.delete(gatewayId);
    else state.selectedGatewayIds.add(gatewayId);
    renderJob(state.currentJob);
  }

  function renderJob(job) {
    updateJobControls(job);
    if (!job) {
      state.currentJob = null;
      state.currentJobId = null;
      state.selectedGatewayIds.clear();
      $("#jobCaption").textContent = "尚未运行测试。API 错误会保存记录。";
      $("#jobSummary").textContent = "—";
      clear($("#jobStats"));
      const td = node("td", "暂无测试记录", "empty"); td.colSpan = 9;
      const tr = document.createElement("tr"); tr.append(td); clear($("#runRows"), [tr]);
      return;
    }
    if (state.currentJobId !== job.id) {
      state.currentJobId = job.id;
      state.selectedGatewayIds.clear();
    }
    const validGatewayIds = new Set(job.gateways.map(site => Number(site.gateway_id)));
    for (const gatewayId of state.selectedGatewayIds) if (!validGatewayIds.has(gatewayId)) state.selectedGatewayIds.delete(gatewayId);
    state.currentJob = job;
    $("#jobCaption").textContent = `任务 #${job.id} · ${job.engine} · ${job.mode === "parallel" ? "并行" : "串行"} · 每站 ${job.rounds} 轮 · ${jobStatus(job.status)}`;
    const summary = $("#jobSummary"); summary.textContent = `总正确率 ${pct(job.summary.accuracy)} · ${job.summary.correct}/${job.summary.graded} · API 错误 ${job.summary.errors} · 中断 ${job.summary.cancelled || 0} · 进度 ${job.summary.completed}/${job.summary.planned}`; summary.className = `metric ${job.summary.accuracy != null && job.summary.accuracy < 80 ? "low" : ""}`;
    clear($("#jobStats"), job.gateways.map(site => statCard(site, false, job.rounds, true)));
    const opened = openDetailKeys($("#runRows"));
    const visibleRuns = state.selectedGatewayIds.size === 0 ? job.runs : job.runs.filter(run => state.selectedGatewayIds.has(Number(run.gateway_id)));
    const rows = visibleRuns.map(run => { const tr = document.createElement("tr"), visual = runVisual(run); tr.append(node("td", run.gateway_name), node("td", String(run.round_number)), node("td", visual.text, visual.className), node("td", run.elapsed_seconds == null ? "—" : `${run.elapsed_seconds.toFixed(2)}s`), node("td", num(run.input_tokens)), node("td", num(run.output_tokens)), node("td", num(run.reasoning_tokens)), node("td", num(run.total_tokens))); const d = node("td"); d.append(details(run.answer, run.error, `job-${job.id}-run-${run.id}`, opened)); tr.append(d); return tr; });
    if (!rows.length) { const tr = document.createElement("tr"), td = node("td", state.selectedGatewayIds.size ? "所选中转站暂无测试记录" : "等待第一轮完成…", "empty"); td.colSpan = 9; tr.append(td); rows.push(tr); } clear($("#runRows"), rows);
  }
  function renderHistory(data) { state.history = new Map(data.gateways.map(x => [x.gateway_id, x])); clear($("#historyStats"), data.gateways.length ? data.gateways.map(site => statCard(site, true)) : [node("p", "暂无历史判分记录。", "empty")]); const opened = openDetailKeys($("#historyRows")); const rows = data.runs.map(run => { const tr = document.createElement("tr"), visual = runVisual(run); tr.append(node("td", run.gateway_name), node("td", run.engine), node("td", String(run.round_number)), node("td", visual.text, visual.className), node("td", run.is_correct == null ? "—" : run.is_correct ? "正确" : "错误"), node("td", run.elapsed_seconds == null ? "—" : `${run.elapsed_seconds.toFixed(2)}s`), node("td", run.created_at)); const d = node("td"); d.append(details(run.answer, run.error, `history-run-${run.id}`, opened)); tr.append(d); return tr; }); if (!rows.length) { const tr = document.createElement("tr"), td = node("td", "暂无历史记录", "empty"); td.colSpan = 8; tr.append(td); rows.push(tr); } clear($("#historyRows"), rows); renderGateways(); }
  async function loadGateways() { const data = await api("/api/gateways"); state.gateways = data.gateways; renderGateways(); }
  async function loadHistory() { renderHistory(await api("/api/history")); }
  async function refreshCurrent() { const data = await api("/api/jobs/current"); renderJob(data.job); state.currentId = data.job?.id || null; if (["completed", "failed", "cancelled"].includes(data.job?.status)) await loadHistory(); }
  $("#jobStats").onclick = (event) => { const stat = event.target.closest(".stat-selectable"); if (stat && $("#jobStats").contains(stat)) toggleGatewayFilter(stat); };
  $("#jobStats").onkeydown = (event) => { if (event.key !== "Enter" && event.key !== " ") return; const stat = event.target.closest(".stat-selectable"); if (!stat || !$("#jobStats").contains(stat)) return; event.preventDefault(); toggleGatewayFilter(stat); };
  $("#newGateway").onclick = () => showGateway(); $("#cancelGateway").onclick = () => { $("#gatewayPanel").hidden = true; };
  $("#selectAllGateways").onchange = (event) => { for (const checkbox of document.querySelectorAll("#gatewayRows input[type=checkbox]:not(:disabled)")) checkbox.checked = event.currentTarget.checked; syncGatewaySelectAll(); };
  $("#gatewayRows").onchange = (event) => { if (event.target.matches("input[type=checkbox]")) syncGatewaySelectAll(); };
  $("#pushWebdav").onclick = async () => { if (!confirm("Push 会用本机全部数据覆盖云端。继续吗？")) return; setSyncBusy(true); try { let data; try { data = await api("/api/webdav/push", { method: "POST", body: JSON.stringify({ force: false }) }); } catch (e) { if (e.code !== "WEBDAV_CONFLICT" || !confirm("远端已被其他设备更新。确定强制用本机数据覆盖云端吗？")) throw e; data = await api("/api/webdav/push", { method: "POST", body: JSON.stringify({ force: true }) }); } await loadWebdav(); flash(`Push 完成：revision ${shortRevision(data.result.revision)}${data.result.warnings?.length ? `；${data.result.warnings.join("；")}` : ""}`); } catch (e) { flash(e.message, true); } finally { setSyncBusy(false); } };
  $("#pullWebdav").onclick = async () => { if (!confirm("危险：Pull 不会备份，会用云端快照替换本机全部中转站、API Key、代理设置和历史。确定继续吗？")) return; setSyncBusy(true); try { const data = await api("/api/webdav/pull", { method: "POST", body: JSON.stringify({ confirm: true }) }); flash(`Pull 完成：revision ${shortRevision(data.result.revision)}，正在刷新页面…`); setTimeout(() => location.reload(), 300); } catch (e) { flash(e.message, true); setSyncBusy(false); } };
  $("#gatewayForm").onsubmit = async (event) => { event.preventDefault(); const f = event.currentTarget; const body = { name: f.name.value, base_url: f.base_url.value, model: f.model.value, api_key: f.api_key.value, enabled: f.enabled.checked }; try { await api(state.editing ? `/api/gateways/${state.editing.id}` : "/api/gateways", { method: state.editing ? "PUT" : "POST", body: JSON.stringify(body) }); $("#gatewayPanel").hidden = true; flash("中转站已保存。"); await loadGateways(); } catch (e) { flash(e.message, true); } };
  $("#engine").onchange = setEfforts;
  $("#jobForm").onsubmit = async (event) => { event.preventDefault(); const f = event.currentTarget, gateway_ids = [...document.querySelectorAll("#gatewayRows input[type=checkbox]:checked")].map(x => Number(x.value)); const body = { engine: f.engine.value, rounds: Number(f.rounds.value), reasoning_effort: f.reasoning_effort.value, model_override: f.model_override.value, mode: f.mode.value, gateway_ids }; try { const result = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) }); flash(`任务 #${result.job_id} 已启动。`); await refreshCurrent(); } catch (e) { flash(e.message, true); } };
  $("#stopJob").onclick = async () => { if (!state.currentId || !confirm("确定中断当前测试？已完成的轮次会保留，正在调用的 CLI 进程将被终止。")) return; const button = $("#stopJob"); button.disabled = true; button.textContent = "正在中断…"; try { await api(`/api/jobs/${state.currentId}/cancel`, { method: "POST" }); flash("已发送中断请求，正在终止 CLI 调用…"); await refreshCurrent(); } catch (e) { flash(e.message, true); button.disabled = false; button.textContent = "中断测试"; } };
  $("#clearHistory").onclick = async () => { if (!confirm("确认清空所有测试任务和运行历史？中转站配置不会删除。")) return; try { await api("/api/history", { method: "DELETE", body: JSON.stringify({ confirm: true }) }); flash("历史已清空。"); await loadHistory(); renderJob(null); } catch (e) { flash(e.message, true); } };
  const logoutButton = $("#logout");
  if (logoutButton) logoutButton.onclick = async () => { try { await api("/logout", { method: "POST", body: "{}" }); window.location.assign("/login"); } catch (e) { flash(e.message, true); } };
  async function init() { try { state.runtime = await api("/api/runtime"); renderEngines(); await Promise.all([loadWebdav(), loadHistory(), loadGateways(), refreshCurrent()]); setInterval(() => refreshCurrent().catch(e => flash(e.message, true)), 2000); } catch (e) { flash(e.message, true); } }
  init();
})();
