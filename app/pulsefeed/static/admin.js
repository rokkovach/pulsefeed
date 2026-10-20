/* Aggregate admin controls. Reads never mutate limits or inference state. */
(() => {
  "use strict";
  const boot = JSON.parse(document.getElementById("admin-bootstrap").textContent);
  const $ = (id) => document.getElementById(id);
  const state = { quotas: boot.quotas, report: null, window: "today", mode: null, modeExplicit: false, writing: false, reading: false,
    armed: false, expired: false, suspended: false, requests: new Set(), readSequence: 0, cooldownUntil: 0, cooldownTimer: null,
    limitDraft: { global_daily_limit: { revision: 0, dirty: false }, feed_article_limit: { revision: 0, dirty: false } } };
  function cancelRequests() { for (const controller of state.requests) controller.abort(); }
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }
  function feedback(message, error = false) {
    $("admin-feedback").hidden = false;
    $("admin-feedback").className = `admin-feedback${error ? " error" : ""}`;
    $("admin-feedback").textContent = message;
  }
  function expire(message = "Your admin session has ended. Sign in again to continue.") {
    if (state.expired) return;
    state.expired = true; state.readSequence++;
    cancelRequests();
    clearInterval(state.cooldownTimer); state.cooldownTimer = null;
    for (const stage of ["qwen", "flux"]) for (const suffix of ["usage-rows", "usage-chart", "bucket-rows"]) $(`${stage}-${suffix}`)?.replaceChildren();
    $("usage-rows").replaceChildren();
    $("admin-dashboard").hidden = true;
    $("admin-access-message").textContent = message;
    $("admin-access-expired").hidden = false;
    window.dispatchEvent?.(new Event("pulsefeed:admin-expired"));
  }
  function updateControls() {
    const blocked = state.writing || state.expired || state.suspended || Date.now() < state.cooldownUntil;
    document.querySelectorAll("[data-admin-write]").forEach((button) => { button.disabled = blocked; });
    $("refresh-usage").disabled = state.reading || state.writing || state.expired || state.suspended;
    $("usage-window").disabled = state.writing || state.expired || state.suspended;
    $("usage-mode").disabled = state.writing || state.expired || state.suspended;
    $("cancel-quota-reset").disabled = state.writing;
  }
  function cooldown(seconds) {
    state.cooldownUntil = Math.max(state.cooldownUntil, Date.now() + seconds * 1000);
    clearInterval(state.cooldownTimer);
    state.cooldownTimer = null;
    if (!state.expired && !state.suspended && Date.now() < state.cooldownUntil)
      state.cooldownTimer = setInterval(() => { updateControls(); if (Date.now() >= state.cooldownUntil) { clearInterval(state.cooldownTimer); state.cooldownTimer = null; } }, 1000);
    updateControls();
  }
  async function api(path, body) {
    if (state.expired || state.suspended) throw new Error("Admin access is required.");
    const options = { method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } };
    if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.headers["X-CSRF-Token"] = boot.csrf; options.body = JSON.stringify(body); }
    const controller = new AbortController(); options.signal = controller.signal;
    state.requests.add(controller);
    // Headers and body share a deadline. Cancellation never automatically repeats an uncertain write.
    const interrupted = () => new Error("Connection interrupted. Refresh usage to check the current allowance before submitting another change.");
    const canceled = new Promise((_, reject) => controller.signal.addEventListener("abort", () => reject(interrupted()), {once: true}));
    const timer = setTimeout(() => controller.abort(), 30000);
    controller.signal.addEventListener("abort", () => clearTimeout(timer), {once: true});
    try {
      let response;
      try { response = await Promise.race([fetch(path, options), canceled]); }
      catch { throw interrupted(); }
      if (controller.signal.aborted) throw interrupted();
      if (response.status === 401) { expire(); throw new Error("Your session has expired."); }
      let data;
      try { data = await Promise.race([response.json(), canceled]); }
      catch {
        if (response.ok || controller.signal.aborted) throw new Error("The response could not be confirmed. Refresh usage before submitting another change.");
        data = {};
      }
      if (controller.signal.aborted) throw interrupted();
      if (!response.ok) {
        if (response.status === 403 && data.error?.code !== "CSRF_FAILED") expire("Admin access is required. Sign in again to continue.");
        let message = data.error?.message || "This request could not be completed. Please refresh and try again.";
        if (response.status === 429) {
          const raw = response.headers.get("Retry-After");
          const delay = Math.max(1, Number(raw) || Math.ceil((Date.parse(raw) - Date.now()) / 1000) || 60);
          cooldown(delay); message += ` Try again in ${delay} seconds.`;
        }
        throw new Error(message);
      }
      return data;
    } finally { clearTimeout(timer); state.requests.delete(controller); }
  }
  const count = (value) => Number.isInteger(value) && value >= 0;
  const format = (value) => value === null || value === undefined ? "Unavailable" : Number(value).toLocaleString();
  function validQuotas(quotas) {
    return quotas?.global && quotas.page
      && ["limit", "charged", "reserved", "used", "remaining", "uncertain_charged"].every((key) => count(quotas.global[key]))
      && quotas.global.limit > 0 && quotas.global.limit <= 10000
      && count(quotas.page.article_limit) && quotas.page.article_limit >= 1 && quotas.page.article_limit <= 10 && quotas.page.max_articles === 10
      && Number.isFinite(quotas.global.reset_at) && Number.isFinite(quotas.global.window_start);
  }
  function dateLabel(value) {
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    if (!Number.isFinite(date.getTime())) return "Unavailable";
    return `${date.toLocaleString(undefined, { timeZone: "UTC", year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })} UTC · ${date.toLocaleString()} local`;
  }
  function renderQuotas(quotas) {
    if (!validQuotas(quotas)) throw new Error("The allowance response was incomplete. Refresh usage before changing limits.");
    state.quotas = quotas;
    for (const key of ["used", "reserved", "remaining", "limit"]) $(`quota-${key}`).textContent = format(quotas.global[key]);
    $("quota-charged").textContent = `${format(quotas.global.charged)} / ${format(quotas.global.limit)}`;
    $("daily-progress").max = quotas.global.limit;
    $("daily-progress").value = Math.min(quotas.global.charged, quotas.global.limit);
    $("daily-progress").textContent = $("quota-charged").textContent;
    $("quota-reset").textContent = dateLabel(quotas.global.reset_at);
    $("quota-uncertain").hidden = !quotas.global.uncertain_charged;
    $("quota-uncertain").textContent = `${format(quotas.global.uncertain_charged)} attempts remain charged because their final usage could not be confirmed.`;
    $("quota-page-summary").textContent = `Up to ${format(quotas.page.article_limit)} illustrated ${quotas.page.article_limit === 1 ? "story" : "stories"} per new page`;
    const hourly = quotas.hourly;
    for (const key of ["limit", "used", "remaining"]) if ($(`hourly-${key}`)) $(`hourly-${key}`).textContent = format(count(hourly?.[key]) ? hourly[key] : null);
    if ($("hourly-next-slot")) $("hourly-next-slot").textContent = Number.isFinite(hourly?.next_slot_at) ? dateLabel(hourly.next_slot_at) : count(hourly?.remaining) && hourly.remaining > 0 ? "Available now" : "Unavailable";
  }
  function syncLimitInputs(quotas, revisions) {
    for (const [key, id, value] of [["global_daily_limit", "global-daily-limit", quotas.global.limit], ["feed_article_limit", "feed-article-limit", quotas.page.article_limit]]) {
      if (state.limitDraft[key].revision !== revisions[key]) continue;
      $(id).value = String(value);
      state.limitDraft[key].dirty = false;
    }
  }
  function validMetric(metric) {
    return metric && (metric.total === null || (Number.isFinite(metric.total) && metric.total >= 0)) && count(metric.reported) && count(metric.missing);
  }
  const modeLabels = {live: "Live", synthetic: "Synthetic demo", unknown: "Unknown mode"};
  const chartSeries = {qwen: [["input_tokens", "Input tokens"], ["output_tokens", "Output tokens"]], flux: [["image_count", "Images"], ["inference_attempts", "Attempts"]]};
  function timestamp(value) { return typeof value === "number" ? value * 1000 : Date.parse(value); }
  function validModel(model) {
    return model && ["qwen", "flux"].includes(model.stage) && Object.hasOwn(modeLabels, model.execution_mode)
      && (model.model_id === null || typeof model.model_id === "string")
      && ["invocations", "succeeded", "failed", "pending", "unknown"].every(key => count(model[key]))
      && typeof model.tokens_applicable === "boolean" && typeof model.images_applicable === "boolean"
      && ["input_tokens", "output_tokens", "total_tokens", "image_count", "inference_attempts"].every(key => validMetric(model[key]))
      && (model.inference_retries === undefined || validMetric(model.inference_retries));
  }
  function validReport(report) {
    return report && ["today", "all"].includes(report.window) && Array.isArray(report.models) && report.models.length <= 100
      && report.models.every(validModel) && validQuotas(report.quotas);
  }
  function validTimeline(report) {
    const timeline = report.timeline;
    if (!timeline || !Number.isInteger(timeline.bucket_seconds) || timeline.bucket_seconds < 3600
      || !Array.isArray(timeline.buckets) || !timeline.buckets.length || timeline.buckets.length > 120
      || !Number.isFinite(timestamp(report.generated_at)) || !count(timeline.unbucketed_invocations ?? 0)) return false;
    if (report.window === "today" && (timeline.bucket_seconds !== 3600 || timeline.buckets.length !== 24)) return false;
    if (report.window === "all" && timeline.bucket_seconds % 86400 !== 0) return false;
    const start = timestamp(timeline.start), end = timestamp(timeline.end), width = timeline.bucket_seconds * 1000;
    if (!Number.isFinite(start) || !Number.isFinite(end) || !Number.isFinite(width) || end <= start || Math.ceil((end - start) / width) !== timeline.buckets.length) return false;
    return timeline.buckets.every((bucket, index) => timestamp(bucket.start) === start + index * width && timestamp(bucket.end) === Math.min(end, start + (index + 1) * width)
      && Array.isArray(bucket.models) && bucket.models.length <= 100 && bucket.models.every(validModel));
  }
  function metricCell(metric, applicable = true, notApplicable = "Not applicable", tag = "td") {
    const cell = node(tag, "usage-metric");
    if (!applicable) { cell.append(node("strong", "", "N/A"), node("small", "", notApplicable)); return cell; }
    cell.append(node("strong", "", format(metric.total)));
    if (metric.missing && metric.total !== null) cell.append(node("small", "partial-coverage", "Known subtotal"));
    return cell;
  }
  function renderModelRows(stage, report) {
    const models = report.models.filter(model => model.stage === stage && model.execution_mode === state.mode);
    const rows = models.map(model => {
      const row = node("tr"), identity = node("th", "usage-model"); identity.scope = "row";
      identity.append(node("strong", "", model.model_id || "Unverified model"), node("span", `mode-badge ${model.execution_mode}`, modeLabels[model.execution_mode]));
      const outcomes = node("td", "usage-outcomes"); outcomes.append(node("strong", "", `${format(model.invocations)} calls`));
      const counts = node("dl");
      for (const [label, key] of [["Succeeded", "succeeded"], ["Failed", "failed"], ["Pending", "pending"], ["Unknown", "unknown"]]) counts.append(node("dt", "", label), node("dd", "", format(model[key])));
      outcomes.append(counts); row.append(identity, outcomes);
      if (stage === "qwen") for (const key of ["input_tokens", "output_tokens", "total_tokens"]) row.append(metricCell(model[key], model.tokens_applicable));
      else row.append(metricCell(model.image_count, model.images_applicable));
      row.append(metricCell(model.inference_attempts), metricCell(model.inference_retries || {total: null, reported: 0, missing: model.invocations}));
      return row;
    });
    if (!rows.length) { const row = node("tr"), cell = node("td", "usage-empty", `No ${stage === "qwen" ? "Qwen" : "FLUX"} calls recorded for this mode and period.`); cell.colSpan = stage === "qwen" ? 7 : 5; row.append(cell); rows.push(row); }
    $(`${stage}-usage-rows`).replaceChildren(...rows);
    return models;
  }
  function bucketMetric(models, key) {
    if (!models.length) return {total: 0, partial: false};
    const applicable = model => key.endsWith("tokens") ? model.tokens_applicable : key === "image_count" ? model.images_applicable : true;
    const measured = models.filter(model => applicable(model) && model[key].total !== null);
    const total = measured.reduce((sum, model) => sum + model[key].total, 0);
    return {total: measured.length && Number.isFinite(total) ? total : null,
      partial: measured.length > 0 && models.some(model => !applicable(model) || model[key].total === null || model[key].missing > 0)};
  }
  function bucketLabel(bucket, hourly = false) {
    const options = {timeZone: "UTC", month: "short", day: "numeric", ...(hourly ? {hour: "2-digit", minute: "2-digit", hour12: false} : {year: "numeric"})};
    const start = new Date(timestamp(bucket.start)).toLocaleString("en-GB", options);
    const end = new Date(timestamp(bucket.end)).toLocaleString("en-GB", options);
    return `${start} – ${end} UTC`;
  }
  function pointValue(metric, future) { return future ? "Not yet elapsed" : metric.total === null ? "Unavailable" : `${format(metric.total)}${metric.partial ? " · Known subtotal" : ""}`; }
  function svgNode(tag, attrs = {}, text) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function renderChart(stage, report, models) {
    const chart = $(`${stage}-usage-chart`), rows = $(`${stage}-bucket-rows`), note = $(`${stage}-chart-note`);
    $(`${stage}-chart-mode`).textContent = modeLabels[state.mode];
    const series = chartSeries[stage], hourly = report.window === "today";
    const ready = validTimeline(report), generated = timestamp(report.generated_at);
    const points = ready ? report.timeline.buckets.map(bucket => {
      const selected = bucket.models.filter(model => model.stage === stage && model.execution_mode === state.mode);
      return {bucket, future: timestamp(bucket.start) > generated, futureRecorded: timestamp(bucket.start) > generated && selected.length > 0, open: timestamp(bucket.start) <= generated && generated < timestamp(bucket.end),
        values: series.map(([key]) => bucketMetric(selected, key))};
    }) : [];
    const tableRows = points.map(point => {
      const row = node("tr"), label = node("th", "", `${bucketLabel(point.bucket, hourly)}${point.open ? " · In progress" : point.futureRecorded ? " · Future-dated measurement (not plotted)" : ""}`); label.scope = "row"; row.append(label);
      for (const value of point.values) row.append(node("td", value.partial ? "partial-coverage" : "", pointValue(value, point.future && !point.futureRecorded)));
      return row;
    });
    if (!tableRows.length) { const row = node("tr"), cell = node("td", "usage-empty", "Timeline unavailable. Refresh usage to try again."); cell.colSpan = 3; row.append(cell); tableRows.push(row); }
    rows.replaceChildren(...tableRows);
    note.textContent = ready ? `${hourly ? "Hourly" : report.timeline.bucket_seconds === 86400 ? "Daily" : `${report.timeline.bucket_seconds / 86400}-day`} buckets · UTC · calls grouped by start time.${points.some(point => point.open) ? " Current bucket is still in progress." : ""} Gaps are unavailable; partial values are known subtotals.` : "Timeline unavailable in this report. Totals remain available below; refresh usage to check for chart data.";
    if (!points.length || !models.length) {
      chart.replaceChildren(node("p", "usage-chart-empty", !ready ? "Timeline unavailable. Refresh usage to try again." : "No calls recorded for this mode and period."));
      return;
    }
    const values = points.flatMap(point => point.future ? [] : point.values.map(value => value.total).filter(value => value !== null));
    const max = Math.max(1, ...values), x = index => 84 + (points.length === 1 ? 252 : index / (points.length - 1) * 504), y = value => 168 - value / max * 140;
    const svg = svgNode("svg", {viewBox: "0 0 620 210", role: "img", "aria-label": `${stage === "qwen" ? "Qwen" : "FLUX"} ${series.map(([, label]) => label).join(" and ")}, ${modeLabels[state.mode]}. Same vertical scale; see chart data table for values.`});
    for (const ratio of [0, .5, 1]) {
      const position = y(max * ratio), value = max * ratio;
      const label = value >= 1e15 ? value.toExponential(1).replace("+", "") : value.toLocaleString("en-GB", {notation: "compact", maximumFractionDigits: 1});
      svg.append(svgNode("line", {x1: 84, x2: 588, y1: position, y2: position, class: "usage-chart-grid"}));
      svg.append(svgNode("text", {x: 76, y: position + 4, "text-anchor": "end", class: "usage-chart-axis"}, label));
    }
    for (const index of [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])]) {
      const date = new Date(timestamp(points[index].bucket.start));
      svg.append(svgNode("text", {x: x(index), y: 195, "text-anchor": "middle", class: "usage-chart-axis"}, date.toLocaleString("en-GB", {timeZone: "UTC", ...(hourly ? {hour: "2-digit", minute: "2-digit", hour12: false} : {month: "short", day: "numeric"})})));
    }
    for (let seriesIndex = 0; seriesIndex < 2; seriesIndex++) {
      let path = "", connected = false; const dots = [];
      points.forEach((point, index) => {
        const value = point.values[seriesIndex];
        if (point.future || value.total === null) { connected = false; return; }
        path += `${connected ? "L" : "M"}${x(index).toFixed(2)},${y(value.total).toFixed(2)} `; connected = true;
        const dot = svgNode("circle", {cx: x(index), cy: y(value.total), r: value.partial ? 3.5 : 2.5, class: `usage-chart-dot series-${seriesIndex}${value.partial ? " partial" : ""}`});
        dot.append(svgNode("title", {}, `${bucketLabel(point.bucket, hourly)} · ${series[seriesIndex][1]}: ${pointValue(value, false)}`)); dots.push(dot);
      });
      svg.append(svgNode("path", {d: path.trim(), class: `usage-chart-line series-${seriesIndex}`, fill: "none"}), ...dots);
    }
    if (!values.length) svg.append(svgNode("text", {x: 310, y: 90, "text-anchor": "middle", class: "usage-chart-axis"}, "No measurements recorded"));
    chart.replaceChildren(svg);
  }
  function renderReports(report) {
    if (!state.modeExplicit) state.mode = ["live", "synthetic", "unknown"].find(mode => report.models.some(model => model.execution_mode === mode)) || "unknown";
    $("usage-mode").value = state.mode;
    for (const stage of ["qwen", "flux"]) renderChart(stage, report, renderModelRows(stage, report));
    $("usage-undated-note").hidden = !(report.timeline?.unbucketed_invocations > 0);
  }
  function renderUsage(report) {
    if (!validReport(report)) throw new Error("The aggregate usage response was incomplete. Please refresh to try again.");
    state.report = report; renderReports(report);
    $("usage-window-caption").textContent = report.window === "today" ? "Calls started today (UTC). Each call stays in the UTC day it began. The allowance above always shows the current UTC day." : "All recorded calls. The allowance above always shows the current UTC day, independently of these reports.";
    $("usage-generated-at").textContent = `Updated ${dateLabel(report.generated_at)}`;
    renderQuotas(report.quotas);
  }
  async function refreshUsage() {
    if (state.expired || state.suspended) return false;
    const sequence = ++state.readSequence;
    state.reading = true; updateControls(); $("usage-rows").setAttribute("aria-busy", "true");
    try {
      const report = await api(`/api/admin/usage?window=${state.window}`);
      if (sequence !== state.readSequence || state.expired) return false;
      if (report.window !== state.window) throw new Error("The usage period did not match this request. Refresh usage to try again.");
      renderUsage(report); return true;
    } catch (error) { if (sequence === state.readSequence && !state.expired) feedback(error.message, true); return false; }
    finally { if (sequence === state.readSequence) { state.reading = false; $("usage-rows").removeAttribute("aria-busy"); updateControls(); } }
  }
  function armReset() {
    if (state.writing || state.expired || state.suspended || Date.now() < state.cooldownUntil) return;
    state.armed = true; $("quota-reset-confirmation").hidden = false; $("arm-quota-reset").hidden = true; $("confirm-quota-reset").focus();
  }
  function cancelReset() {
    if (state.writing) return;
    disarmReset();
    $("arm-quota-reset").focus();
  }
  function disarmReset() {
    state.armed = false; $("quota-reset-confirmation").hidden = true; $("arm-quota-reset").hidden = false;
  }
  async function write(path, body, message, submittedRevisions = null) {
    if (state.writing || state.expired || state.suspended || Date.now() < state.cooldownUntil) return;
    state.writing = true; state.readSequence++; state.reading = false; updateControls();
    try {
      const result = await api(path, body);
      if (state.expired || state.suspended) return;
      renderQuotas(result.quotas);
      if (submittedRevisions) syncLimitInputs(result.quotas, submittedRevisions);
      state.armed = false; $("quota-reset-confirmation").hidden = true; $("arm-quota-reset").hidden = false;
      const fresh = await refreshUsage();
      const unsaved = Object.values(state.limitDraft).some((draft) => draft.dirty);
      if (!state.expired) feedback(`${message}${unsaved ? " Your newer limit edits are still unsaved." : ""}${fresh ? "" : " Aggregate usage could not be refreshed; use Refresh to check again."}`);
    } catch (error) { if (!state.expired) feedback(error.message, true); }
    finally { state.writing = false; updateControls(); }
  }
  async function saveLimits(event) {
    event.preventDefault();
    const global = Number($("global-daily-limit").value);
    const articles = Number($("feed-article-limit").value);
    if (!Number.isInteger(global) || global < 1 || global > 10000 || !Number.isInteger(articles) || articles < 1 || articles > 10) { feedback("Use whole-number limits: global daily image attempts 1–10,000 and articles per page 1–10.", true); return; }
    const revisions = Object.fromEntries(Object.entries(state.limitDraft).map(([key, draft]) => [key, draft.revision]));
    await write("/api/admin/limits", { global_daily_limit: global, feed_article_limit: articles }, "Limits saved. Existing pages and reservations are unchanged.", revisions);
  }
  async function confirmReset() {
    if (!state.armed) return;
    await write("/api/admin/limits/reset", { confirm: true }, "Daily and hourly allowances were reset. Model usage history is retained; active and unresolved work remains reserved. Limits and request rate limits are unchanged.");
  }
  $("admin-limits-form").addEventListener("submit", saveLimits);
  $("admin-limits-form").addEventListener("invalid", () => { $("limits-section").open = true; }, true);
  for (const [key, id] of [["global_daily_limit", "global-daily-limit"], ["feed_article_limit", "feed-article-limit"]]) {
    $(id).addEventListener("input", () => { state.limitDraft[key].revision++; state.limitDraft[key].dirty = true; });
  }
  $("arm-quota-reset").addEventListener("click", armReset);
  $("cancel-quota-reset").addEventListener("click", cancelReset);
  $("confirm-quota-reset").addEventListener("click", confirmReset);
  $("refresh-usage").addEventListener("click", refreshUsage);
  $("usage-window").addEventListener("change", () => {
    state.window = $("usage-window").value === "all" ? "all" : "today";
    try { const url = new URL(window.location.href); url.searchParams.set("period", state.window); url.searchParams.delete("window"); window.history.replaceState(null, "", url); } catch { /* Display still works without URL preference updates. */ }
    refreshUsage();
  });
  $("usage-mode").addEventListener("change", () => {
    if (state.expired || !state.report) return;
    const mode = $("usage-mode").value;
    if (!Object.hasOwn(modeLabels, mode)) return;
    state.mode = mode; state.modeExplicit = true; renderReports(state.report);
  });
  window.addEventListener("pulsefeed:admin-expired", () => expire());
  window.addEventListener("focus", async () => {
    if (state.expired || state.suspended) return;
    try { const session = await api("/api/session"); if (session.account.id !== boot.account.id || session.csrf !== boot.csrf) { expire(); window.location.reload(); } }
    catch { /* A 401 is handled by api; transient reads do not mutate allowance. */ }
  });
  window.addEventListener("pagehide", () => {
    state.suspended = true; state.readSequence++; state.reading = false;
    cancelRequests(); clearInterval(state.cooldownTimer); state.cooldownTimer = null;
    $("usage-rows").removeAttribute("aria-busy"); updateControls();
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted || state.expired) return;
    state.suspended = false; cooldown(0);
    // Refresh only the report; restoring a cached dashboard must preserve unsaved limit drafts.
    refreshUsage();
  });
  async function start() {
    state.window = boot.period === "all" ? "all" : "today";
    try { const value = new URL(window.location.href).searchParams.get("window"); if (value === "today" || value === "all") state.window = value; } catch { /* Use the server-selected period. */ }
    $("usage-window").value = state.window;
    syncLimitInputs(boot.quotas, { global_daily_limit: 0, feed_article_limit: 0 });
    const currentSnapshot = validReport(boot.usage) && boot.usage.window === state.window;
    try { if (currentSnapshot) renderUsage(boot.usage); else renderQuotas(boot.quotas); } catch (error) { feedback(error.message, true); }
    if (!currentSnapshot) await refreshUsage();
  }
  start();
})();
