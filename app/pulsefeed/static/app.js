/* PulseFeed browser state. Image blobs are account-partitioned; credentials are never persisted. */
(() => {
  "use strict";
  const boot = JSON.parse(document.getElementById("bootstrap").textContent);
  const $ = (id) => document.getElementById(id);
  const store = window.pulseImages;
  const state = { account: null, csrf: boot.csrf, view: boot.view || null, viewExpired: false, currentFeed: null,
    items: [], runs: new Map(), images: new Map(), imageDecodes: new Map(), loadingImages: new Set(),
    unavailable: new Set(), requests: new Set(), suspended: false, batchId: null, pollTimer: null, pollFailures: 0, polling: false, expired: false, submitting: false, navigating: false,
    cooldownUntil: 0, cooldownTimer: null, dbError: null, storageReady: null, funTimer: null, funIndex: 0, funPosition: 0, loaderTimer: null, loaderPhase: "idle",
    pendingChecking: false, restoring: Boolean(boot.view), revealPhase: boot.view ? "holding" : "ready", revealEnteredAt: Date.now(), revealReadyAt: null, revealDuration: 350, revealBypass: false, revealSatisfied: false };
  const statuses = { queued: "Creating illustration", creating_prompt: "Creating illustration", generating_image: "Creating illustration",
    completed: "Illustrated", failed: "Illustration interrupted", submission_unknown: "Checking illustration request" };
  const terminal = new Set(["completed", "failed"]);

  function beginRequest() {
    const controller = new AbortController();
    state.requests.add(controller);
    // One owner covers both headers and body; aborting a POST cannot establish whether it was accepted.
    const canceled = new Promise((_, reject) => controller.signal.addEventListener("abort", () => {
      const error = new Error("Connection interrupted. Your request may still be finishing. Retry explicitly to reconnect safely.");
      error.ambiguous = true; reject(error);
    }, {once: true}));
    const timer = setTimeout(() => controller.abort(), 30000);
    controller.signal.addEventListener("abort", () => clearTimeout(timer), {once: true});
    return { signal: controller.signal, wait: (promise) => Promise.race([promise, canceled]),
      finish() { clearTimeout(timer); state.requests.delete(controller); } };
  }
  function cancelRequests() { for (const controller of state.requests) controller.abort(); }
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }
  function announce(message, error = false) {
    if (!state.view && !boot.pending_feed) {
      const status = $("reader-status");
      if (status) { status.className = `reader-status small${error ? " error" : " muted"}`; status.textContent = message; }
      return;
    }
    if (boot.pending_feed) { pendingMessage(message); return; }
    if (error) stopLoader();
    const feedback = $("feedback");
    if (feedback) { feedback.hidden = false; feedback.className = error ? "error" : ""; feedback.textContent = message; }
  }
  function urlError(message) { announce(message, true); $("feed-url")?.setAttribute("aria-invalid", "true"); }
  function expireSession() {
    state.expired = true;
    cancelRequests();
    cancelImageDecodes();
    clearTimeout(state.pollTimer); state.pollTimer = null;
    clearInterval(state.cooldownTimer); state.cooldownTimer = null;
    stopLoader();
    for (const image of state.images.values()) URL.revokeObjectURL(image.url);
    state.images.clear();
    $("item-list")?.replaceChildren();
    $("studio").hidden = true;
    $("session-expired").hidden = false;
    document.title = "Access expired · PulseFeed";
  }
  function startCooldown(seconds) {
    state.cooldownUntil = Math.max(state.cooldownUntil, Date.now() + seconds * 1000);
    clearInterval(state.cooldownTimer);
    state.cooldownTimer = setInterval(() => {
      updateControls();
      if (Date.now() >= state.cooldownUntil) clearInterval(state.cooldownTimer);
    }, 1000);
    updateControls();
  }
  async function api(path, { method = "GET", body } = {}) {
    if (state.expired || state.suspended) throw new Error("Your session is not active.");
    const options = { method, credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRF-Token"] = state.csrf;
      options.body = JSON.stringify(body);
    }
    const request = beginRequest(); options.signal = request.signal;
    try {
      let response;
      try { response = await request.wait(fetch(path, options)); }
      catch { const error = new Error("Connection interrupted. Your request may still be finishing. Click View RSS again when you are online."); error.ambiguous = true; throw error; }
      if (request.signal.aborted) throw Object.assign(new Error("The request could not be confirmed. Retry explicitly to reconnect safely."), {ambiguous: true});
      if (response.status === 401) { expireSession(); throw new Error("Your session has expired."); }
      if (state.view && !liveView()) { expireView(); throw new Error("This feed’s one-hour window has ended."); }
      let payload;
      try { payload = await request.wait(response.json()); }
      catch {
        if (response.ok || request.signal.aborted) {
          const error = new Error("The connection ended before your request could be confirmed. Click View RSS again to reconnect safely.");
          error.ambiguous = true; throw error;
        }
        payload = {};
      }
      if (request.signal.aborted) throw Object.assign(new Error("The request could not be confirmed. Retry explicitly to reconnect safely."), {ambiguous: true});
      if (state.view && !liveView()) { expireView(); throw new Error("This feed’s one-hour window has ended."); }
      if (!response.ok) {
        if (state.view && response.status === 404 && payload.error?.code === "VIEW_NOT_FOUND") { expireView(true); throw new Error("This feed is no longer available."); }
        let message = payload.error?.message || "We could not complete that request. Please try again.";
        if (response.status === 429) {
          const raw = response.headers.get("Retry-After");
          const seconds = Math.max(1, Number(raw) || Math.ceil((Date.parse(raw) - Date.now()) / 1000) || 60);
          startCooldown(seconds);
          message += ` Try again in ${seconds} seconds.`;
        }
        const error = new Error(message); error.status = response.status; error.code = payload.error?.code; error.ambiguous = response.status >= 500; throw error;
      }
      return payload;
    } finally { request.finish(); }
  }
  function liveView() { return !state.expired && !state.viewExpired && !state.suspended && store.live(state.view); }
  function viewTarget(view = state.view) { return `${view.url}${boot.admin_mode ? "?admin" : ""}`; }
  function expireView(redirect = false) {
    if (!state.view || state.viewExpired) return;
    state.viewExpired = true; state.batchId = null;
    cancelRequests();
    cancelImageDecodes();
    clearTimeout(state.pollTimer); state.pollTimer = null;
    clearInterval(state.cooldownTimer); state.cooldownTimer = null; stopLoader();
    for (const image of state.images.values()) URL.revokeObjectURL(image.url);
    state.images.clear(); state.runs.clear(); state.items = [];
    if (state.expired) return;
    $("item-list")?.replaceChildren();
    $("studio").hidden = true; $("view-expired").hidden = false;
    document.title = "404 · This feed flew away · PulseFeed";
    store.maintain();
    if (redirect) window.location.replace(viewTarget());
  }
  function scoped(path) {
    if (!liveView()) { expireView(); throw new Error("This feed’s one-hour window has ended."); }
    return `${path}?view_id=${encodeURIComponent(state.view.id)}`;
  }
  const funLines = ["Reading between the RSS lines...", "Turning headlines into tiny tokens...",
    "Asking Jensen for more compute...", "Securing more GPUs...", "Untangling the tensor spaghetti...",
    "Sending tokens through the transformer...", "Giving the attention heads a pep talk...",
    "Adding a pinch of parallelism...", "Melting some GPUs...",
    "Exploring the latent space...", "Inviting the pixels out to play...", "Teaching bits to paint...",
    "Putting the tiny bricks together...", "Convincing pixels to cooperate..."];
  const motion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  function stopLoader() {
    clearTimeout(state.funTimer); clearTimeout(state.loaderTimer);
    state.funTimer = null; state.loaderTimer = null;
    state.loaderPhase = "idle"; state.revealBypass = true; state.revealPhase = "ready";
    const list = $("item-list");
    if (list) { list.dataset.reveal = "ready"; list.inert = false; }
    const loader = $("fun-loader");
    if (loader) { loader.dataset.active = "false"; loader.dataset.phase = "idle"; }
  }
  function typeFunLine() {
    state.funTimer = null;
    if (state.loaderPhase === "idle" || document.hidden || motion?.matches) return;
    const message = $("fun-message"); if (!message) return;
    const line = funLines[state.funIndex % funLines.length];
    state.funPosition++;
    message.textContent = line.slice(0, state.funPosition);
    if (state.funPosition >= line.length) {
      state.funTimer = setTimeout(() => {
        state.funIndex = (state.funIndex + 1) % funLines.length; state.funPosition = 0; typeFunLine();
      }, 3200);
    } else state.funTimer = setTimeout(typeFunLine, 42);
  }
  function updateLoader(immediate = false) {
    const loader = $("fun-loader"), message = $("fun-message"), list = $("item-list");
    if (!loader || !message) return;
    if (state.expired || state.viewExpired || state.suspended || immediate === true) { stopLoader(); return; }
    const now = Date.now();
    // Only images still needed by this page hold its reveal; saving an available blob can finish afterwards.
    const waitingForImages = state.items.some(item => {
      const run = latestRunForItem(item.id);
      return run && state.loadingImages.has(run.id) && !state.images.has(run.id);
    });
    const working = state.pendingChecking || (state.submitting && !state.navigating)
      || (boot.pending_feed && state.navigating) || Boolean(state.batchId)
      || waitingForImages || (state.view && state.restoring);
    let active = Boolean(working), phase = working ? "working" : "idle", deadline = null, elapsed = 0;
    if (state.view) {
      const selected = state.items.map(item => latestRunForItem(item.id)).filter(Boolean);
      const threshold = Math.min(3, selected.length);
      // Production images enter this cache only after decode/load succeeds, not at API completion.
      if (threshold && selected.filter(run => state.images.has(run.id)).length >= threshold) state.revealSatisfied = true;
      const failed = state.items.some(item => {
        const run = latestRunForItem(item.id);
        return run && (run.state === "failed" || run.state === "submission_unknown" || run.error || run.retrievalError || state.unavailable.has(run.id));
      });
      if (failed) state.revealBypass = true;
      if (state.revealBypass || motion?.matches) state.revealPhase = "ready";
      if (!state.revealBypass && !motion?.matches && state.revealPhase !== "ready") {
        if (state.revealPhase === "holding" && (state.revealSatisfied || !working)) {
          state.revealReadyAt = now;
          // Scale the one-shot fade to the observed load, never add a fixed hold or guess an API ETA.
          state.revealDuration = Math.round(Math.min(1600, Math.max(350, (now - state.revealEnteredAt) * .12)));
          state.revealPhase = "revealing";
          list?.style?.setProperty("--reveal-duration", `${state.revealDuration}ms`);
          list?.style?.setProperty("--reveal-stagger", `${state.revealDuration * .1}ms`);
          loader.style?.setProperty("--reveal-duration", `${state.revealDuration}ms`);
        }
        if (state.revealPhase === "holding") {
          active = true; phase = "working";
        } else if (state.revealPhase === "revealing") {
          elapsed = Math.max(0, now - state.revealReadyAt);
          if (elapsed >= state.revealDuration) state.revealPhase = "ready";
          else { active = true; phase = "settling"; deadline = state.revealReadyAt + state.revealDuration; }
        }
      }
      if (state.revealPhase === "ready") {
        // Revealed content stays interactive even when a recovered request starts working again.
        active = !failed && !state.revealBypass && !state.revealSatisfied && state.revealReadyAt === null && Boolean(working);
        phase = active ? "working" : "idle";
      }
      if (list) {
        if (list.dataset.reveal !== state.revealPhase) {
          list.style?.setProperty("--reveal-elapsed", `${elapsed}ms`);
          list.dataset.reveal = state.revealPhase;
        }
        list.inert = state.revealPhase === "holding" && !motion?.matches;
      }
    }
    clearTimeout(state.loaderTimer); state.loaderTimer = null;
    if (deadline !== null && !document.hidden) state.loaderTimer = setTimeout(updateLoader, Math.max(0, deadline - now));
    if (state.loaderPhase !== phase) loader.style?.setProperty("--reveal-elapsed", `${elapsed}ms`);
    state.loaderPhase = phase;
    loader.dataset.active = String(active); loader.dataset.phase = phase;
    if (!active || document.hidden || motion?.matches) {
      clearTimeout(state.funTimer); state.funTimer = null;
      message.textContent = active ? funLines[state.funIndex % funLines.length]
        : state.view ? "Your illustrated stories live here." : "A little reading. A little imagination.";
    } else if (!state.funTimer) typeFunLine();
  }
  motion?.addEventListener?.("change", updateLoader);
  function updateControls() {
    const blocked = boot.inference_enabled === false || state.expired || state.viewExpired || state.suspended || state.submitting || state.pendingChecking || state.navigating || Date.now() < state.cooldownUntil || !state.account;
    document.querySelectorAll("[data-mutation]").forEach((button) => { button.disabled = blocked; });
    if ($("pending-retry")) $("pending-retry").disabled = boot.inference_enabled === false || state.expired || state.suspended || state.submitting || state.pendingChecking || state.navigating || Date.now() < state.cooldownUntil;
    if ($("view-feed")) $("view-feed").textContent = "View RSS ↗";
    if ($("feed-url")) $("feed-url").disabled = boot.inference_enabled === false || state.submitting || state.navigating || state.expired || state.suspended || !state.account;
    updateLoader();
  }
  function safeSource(link) {
    try { if (typeof link !== "string" || /[\\\u0000-\u0020\u007f]/.test(link)) return null; const url = new URL(link); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
    catch { return null; }
  }
  function sourceLink(link) {
    const url = safeSource(link);
    if (!url) return node("span", "small muted", "Source link unavailable");
    const a = node("a", "button button-dark source-link reader-story-cta", "Read original story ↗");
    a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
    return a;
  }
  function readerDate(value) {
    if (typeof value !== "string" || !value.trim()) return "";
    let input = value.trim();
    if (/^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$/.test(input)) input = input.replace(" ", "T") + (input.length === 10 ? "T00:00:00Z" : "Z");
    else if (!/(?:[A-Z]{1,5}|[+-]\d{2}:?\d{2})\s*$/i.test(input)) input += " UTC";
    const iso = input.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (iso) {
      const calendar = new Date(0); calendar.setUTCFullYear(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));
      if (calendar.getUTCFullYear() !== Number(iso[1]) || calendar.getUTCMonth() !== Number(iso[2]) - 1 || calendar.getUTCDate() !== Number(iso[3])) return "";
    }
    const timestamp = Date.parse(input); if (!Number.isFinite(timestamp)) return "";
    const date = new Date(timestamp);
    return `${date.getUTCDate()} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][date.getUTCMonth()]} ${date.getUTCFullYear()}`;
  }
  function readerExcerpt(value) {
    if (typeof value !== "string") return "";
    const whole = value.match(/^\s*comments(?:[\t ]+https?:\/\/[^\s]+)?[\t ]*$/i);
    if (whole && whole[0].length === value.length) return "";
    return value.replace(/(?:(?<=[.!?])[\t ]+|[\t ]*[\r\n]+[\t ]*)comments(?:[\t ]+https?:\/\/[^\s]+)?[\t ]*$/i, "").trimEnd();
  }
  function unavailableNotice(item, run) {
    const notice = node("aside", "reader-illustration-notice notice");
    notice.dataset.itemId = item.id; notice.dataset.runId = run.id;
    notice.append(node("strong", "", "Illustration unavailable"), node("p", "", item.title));
    const actions = node("div", "card-actions");
    if (run.state === "completed" && state.unavailable.has(run.id)) actions.append(action("Regenerate image ↗", () => retryRun(run), true));
    else if (run.retrievalError) actions.append(action("↻ Load image again", async () => { delete run.retrievalError; await recoverImage(run); }));
    else if (run.state === "failed" && run.error?.retryable) actions.append(action("↻ Retry illustration", () => retryRun(run), true));
    actions.append(sourceLink(item.link)); notice.append(actions); return notice;
  }
  function renderAdmission(generation) {
    const notice = $("generation-notice");
    if (!notice) return;
    const blocked = generation?.status === "blocked";
    notice.hidden = !blocked;
    if (!blocked) return;
    const messages = {
      "FEED_DAILY_LIMIT": [
            "This page used the previous feed allowance",
            "This page was opened under the previous feed allowance. Load a new feed to use the current limits."
      ],
      "GLOBAL_DAILY_LIMIT": [
            "Daily illustration allowance reached",
            "There was not enough shared daily image allowance left to start this page when it opened. The allowance resets at 00:00 UTC. Try loading a feed after the reset."
      ],
      "GLOBAL_HOURLY_LIMIT": [
            "Hourly generation allowance reached",
            "The shared hourly generation allowance could not fit this page when it opened. Capacity returns as earlier starts leave the rolling one-hour window. Try loading a feed later."
      ],
      "BUSY": [
            "Another request was still finishing",
            "Another illustration request was still finishing when this page opened. Try loading a feed again in a moment."
      ],
      "OTHER": [
            "Illustrations could not start",
            "Illustrations could not start for this page. Try loading a feed again later."
      ]
};
    const code = generation.error?.code;
    const copy = Object.hasOwn(messages, code) ? messages[code] : messages.OTHER;
    $("generation-notice-title").textContent = copy[0];
    $("generation-notice-message").textContent = copy[1];
  }
  function showFeed(data) {
    if (state.expired || state.viewExpired) return;
    if (!data.feed || !Array.isArray(data.items)) throw new Error("This feed could not be opened. Please try View RSS again.");
    const previousRuns = state.runs;
    state.currentFeed = data.feed;
    state.items = data.items.slice(0, 10);
    state.runs = new Map();
    for (const run of data.runs || []) {
      const old = previousRuns.get(run.id);
      if (old?.retrievalError) run.retrievalError = old.retrievalError;
      if (old?.retryKey) run.retryKey = old.retryKey;
    }
    $("feed-content").hidden = false;
    $("current-feed-title").textContent = data.feed.title || "Your feed";
    const feedSource = $("feed-source"); feedSource.textContent = data.feed.url;
    if (feedSource.tagName === "A") { const url = safeSource(data.feed.url); if (url) feedSource.href = url; else feedSource.removeAttribute("href"); }
    state.batchId = data.active_batch_id || null;
    renderAdmission(data.generation);
    if (data.generation?.status === "blocked" || !(data.runs || []).length) state.revealBypass = true;
    state.pollFailures = 0;
    acceptRuns(data.runs);
    $("batch-progress").textContent = state.batchId ? "Illustrations are on their way. You can keep reading." : "";
    clearTimeout(state.pollTimer);
    if (state.batchId && state.account) schedulePoll(100);
    updateControls();
  }
  function readImage(id) {
    if (!liveView() || !state.account) return Promise.resolve(null);
    return store.read(state.account.id, state.view, id);
  }
  async function saveImage(id, blob) {
    if (!liveView() || !state.account) throw new Error("This feed’s one-hour window has ended.");
    await store.save(state.account.id, state.view, id, blob);
  }
  function cacheImage(id, blob, saved, saveError = null, readyUrl = null) {
    if (!liveView()) return;
    const old = state.images.get(id);
    if (old) URL.revokeObjectURL(old.url);
    state.images.set(id, { blob, url: readyUrl || URL.createObjectURL(blob), saved, saveError });
  }
  function cancelImageDecodes() {
    for (const cancel of state.imageDecodes.values()) cancel();
    state.imageDecodes.clear();
  }
  async function cacheReadyImage(id, blob, saved) {
    if (!liveView()) return false;
    // Successful retrieval alone cannot reveal a card: the browser must decode its blob first.
    const url = URL.createObjectURL(blob), image = document.createElement("img");
    image.decoding = "async";
    let transferred = false, cancel, timer;
    const deadline = Date.now() + 15000;
    const canceled = new Promise((resolve, reject) => { cancel = () => { image.src = ""; reject(new Error("Image loading canceled.")); }; });
    state.imageDecodes.set(url, cancel);
    try {
      timer = setTimeout(cancel, 15000);
      let ready;
      if (typeof image.decode === "function") { image.src = url; ready = image.decode(); }
      else ready = new Promise((resolve, reject) => {
        image.onload = resolve; image.onerror = () => reject(new Error("Image load failed.")); image.src = url;
        if (image.complete) image.naturalWidth > 0 ? resolve() : reject(new Error("Image load failed."));
      });
      await Promise.race([ready, canceled]);
      if (!state.imageDecodes.has(url) || !liveView()) return false;
      if (Date.now() >= deadline) throw new Error("Image loading timed out.");
      cacheImage(id, blob, saved, null, url); transferred = true;
      return true;
    } catch {
      if (!state.imageDecodes.has(url) || !liveView()) return false;
      throw new Error("The existing image could not be opened. Try loading the image again.");
    } finally {
      clearTimeout(timer); state.imageDecodes.delete(url); image.onload = null; image.onerror = null;
      if (!transferred) URL.revokeObjectURL(url);
    }
  }
  function pruneSavedImages() {
    const visible = new Set(state.items.map((item) => latestRunForItem(item.id)?.id));
    for (const [id, image] of state.images) {
      // Unsaved blobs are the only copy after a failed write: retain them for retry save.
      if (!image.saved || visible.has(id)) continue;
      URL.revokeObjectURL(image.url); state.images.delete(id);
    }
  }
  async function recoverImage(run) {
    if (!state.account || state.loadingImages.has(run.id) || state.images.has(run.id) || state.unavailable.has(run.id) || !liveView()) return;
    state.loadingImages.add(run.id);
    let retrievalFailed = false;
    delete run.retrievalError;
    const current = state.runs.get(run.id);
    if (current) delete current.retrievalError;
    try {
      let blob;
      if (state.storageReady) await state.storageReady;
      if (!liveView()) return;
      try { blob = await readImage(run.id); } catch { /* Retrieve the existing result if local storage cannot be read. */ }
      if (!liveView()) return;
      if (blob) {
        try { await cacheReadyImage(run.id, blob, true); return; }
        catch { /* A damaged browser copy may still be retrieved from the existing result. */ }
      }
      if (run.availability === "unavailable") { state.unavailable.add(run.id); retrievalFailed = true; return; }
      updateLoader();
      const imagePath = scoped(`/api/runs/${encodeURIComponent(run.id)}/image`);
      const request = beginRequest();
      try {
        const response = await request.wait(fetch(imagePath, { credentials: "same-origin", cache: "no-store", signal: request.signal }));
        if (request.signal.aborted) return;
        if (response.status === 401) { expireSession(); return; }
        if (response.status === 404) { expireView(true); return; }
        if (response.status === 410) { state.unavailable.add(run.id); retrievalFailed = true; return; }
        if (!response.ok) throw new Error("The existing image could not be retrieved. Check your connection and try retrieval again.");
        if (!["image/webp", "image/jpeg"].includes(response.headers.get("Content-Type")?.split(";")[0])) throw new Error("The server returned an unsupported image format.");
        blob = await request.wait(response.blob());
        if (request.signal.aborted) return;
      } finally { request.finish(); }
      if (!blob.size || blob.size > 2097152) throw new Error("The image exceeds the supported size or is empty.");
      if (!liveView()) { expireView(); return; }
      if (!await cacheReadyImage(run.id, blob, false)) return;
      renderRuns();
      try { await saveImage(run.id, blob); if (liveView() && state.images.has(run.id)) state.images.get(run.id).saved = true; }
      catch { if (liveView() && state.images.has(run.id)) state.images.get(run.id).saveError = "Shown here, but not saved in this browser. Retry saving to keep it until this feed expires."; }
    } catch (error) {
      retrievalFailed = true;
      if (liveView()) {
        run.retrievalError = error.message;
        const current = state.runs.get(run.id);
        if (current) current.retrievalError = error.message;
      }
    } finally {
      state.loadingImages.delete(run.id);
      const visibleFailure = retrievalFailed && state.items.some(item => latestRunForItem(item.id)?.id === run.id);
      updateLoader(visibleFailure);
      pruneSavedImages();
      if (liveView()) renderRuns();
    }
  }
  function metric(list, label, value, suffix = "") {
    list.append(node("dt", "", label), node("dd", "", value === null || value === undefined ? "Unavailable" : `${value}${suffix}`));
  }
  function detailsFor(run) {
    const details = node("details", "run-details"); details.dataset.runDetails = run.id;
    details.append(node("summary", "", "Prompt & generation details"));
    const panel = node("div", "detail-panel");
    panel.append(node("h4", "", "Qwen visual prompt"), node("p", "detail-text", run.qwen_prompt || "Not yet available"), node("h4", "", "Final image prompt"), node("p", "detail-text", run.final_prompt || "Not yet available"));
    panel.append(node("h4", "", "Content filter"), node("p", "detail-text", run.used_fallback ? `Fallback used. Matched terms: ${(run.matched_terms || []).join(", ") || "Unavailable"}` : "No fallback recorded."));
    const preset = node("dl", "metric-list"); metric(preset, "Seed", run.seed); metric(preset, "Dimensions", run.width && run.height ? `${run.width} × ${run.height}` : null); metric(preset, "Run ID", run.id); panel.append(preset);
    for (const invocation of run.invocations || []) {
      panel.append(node("h4", "", `${invocation.stage === "qwen" ? "Qwen · Prompt" : "FLUX · Image"} invocation`));
      const values = node("dl", "metric-list");
      const model = invocation.model || {};
      metric(values, "Model", model.id); metric(values, "Revision", model.revision); metric(values, "Precision", model.precision); metric(values, "Runtime", model.runtime);
      if (invocation.stage === "qwen") {
        metric(values, "Input tokens", invocation.usage?.input_tokens); metric(values, "Output tokens", invocation.usage?.output_tokens); metric(values, "Total tokens", invocation.usage?.total_tokens);
      } else metric(values, "Images produced", invocation.usage?.image_count);
      metric(values, "Model inference", invocation.timings?.inference_ms, " ms"); metric(values, "Provider queue", invocation.delay_ms, " ms"); metric(values, "Provider execution", invocation.execution_ms, " ms");
      metric(values, "Inference attempts", invocation.counters?.inference_attempts); metric(values, "Inference retries", invocation.counters?.inference_retries); metric(values, "Dependency retries", invocation.counters?.dependency_retries);
      panel.append(values);
    }
    if (run.result_expires_at) {
      const expiry = typeof run.result_expires_at === "number" ? `${new Date(run.result_expires_at * 1000).toISOString().slice(0, 19).replace("T", " ")} UTC` : run.result_expires_at;
      panel.append(node("p", "detail-text", `Provider retrieval ${run.expiry_estimated ? "estimated expiry" : "expiry"}: ${expiry}. Browser copies last only until this feed page expires.`));
    }
    details.append(panel); return details;
  }
  function bindAction(button, callback) {
    button._pulseAction = callback;
    if (button._pulseActionBound) return;
    button._pulseActionBound = true;
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = true;
      try { await button._pulseAction(); } catch (error) { if (!state.expired) announce(error.message, true); }
      finally { if (button.isConnected) button.disabled = false; updateControls(); }
    });
  }
  function action(label, callback, mutation = false) {
    const button = node("button", "button button-quiet", label); button.type = "button";
    if (mutation) button.dataset.mutation = "";
    bindAction(button, callback); return button;
  }
  function nodeKey(element) {
    if (element.nodeType !== 1) return null;
    if (element.dataset.itemId) return `item:${element.dataset.itemId}`;
    if (element.dataset.runDetails) return `details:${element.dataset.runDetails}`;
    if (["BUTTON", "A"].includes(element.tagName)) return `${element.tagName}:${element.textContent.trim()}`;
    return `${element.tagName}:${String(element.className || "").split(/\s+/)[0]}`;
  }
  function reconcileNode(current, fresh) {
    if (current.nodeType !== fresh.nodeType || current.nodeName !== fresh.nodeName) return fresh;
    if (fresh.nodeType !== 1) { if (current.nodeValue !== fresh.nodeValue) current.nodeValue = fresh.nodeValue; return current; }
    for (const attribute of Array.from(current.attributes)) if (!fresh.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
    for (const attribute of Array.from(fresh.attributes)) if (current.getAttribute(attribute.name) !== attribute.value) current.setAttribute(attribute.name, attribute.value);
    if (fresh._pulseAction) bindAction(current, fresh._pulseAction);
    reconcileChildren(current, Array.from(fresh.childNodes));
    return current;
  }
  function reconcileChildren(parent, freshChildren) {
    // Tiny DOM doubles can use replaceChildren; browsers preserve keyed live nodes and focus.
    if (!parent.childNodes || !parent.insertBefore || !parent.removeChild) { parent.replaceChildren(...freshChildren); return; }
    if (freshChildren.every((child) => child.nodeType === 1)) {
      for (const child of Array.from(parent.childNodes)) if (child.nodeType === 3 && !child.nodeValue.trim()) parent.removeChild(child);
    }
    // Keep decoded image nodes and focused controls mounted during polling and save updates.
    const previous = Array.from(parent.childNodes);
    const used = new Set(); const retained = new Set();
    freshChildren.forEach((fresh, index) => {
      const key = nodeKey(fresh);
      const match = previous.find((candidate) => !used.has(candidate) && candidate.nodeType === fresh.nodeType
        && candidate.nodeName === fresh.nodeName && nodeKey(candidate) === key);
      const element = match ? reconcileNode(match, fresh) : fresh;
      if (match) used.add(match);
      retained.add(element);
      if (parent.childNodes[index] !== element) parent.insertBefore(element, parent.childNodes[index] || null);
    });
    for (const child of Array.from(parent.childNodes)) if (!retained.has(child)) parent.removeChild(child);
  }
  function latestRunForItem(itemId) {
    let latest = null;
    for (const run of state.runs.values()) {
      if (run.item_id === itemId && (!latest || (run.created || 0) >= (latest.created || 0))) latest = run;
    }
    return latest;
  }
  function renderRuns() {
    if (!$("item-list") || state.viewExpired || state.expired) return;
    const openDetails = new Set(Array.from(document.querySelectorAll("[data-run-details][open]"), (element) => element.dataset.runDetails));
    const cards = state.items.filter((item) => latestRunForItem(item.id)).map((item) => {
      const run = latestRunForItem(item.id);
      const card = node("article", "reader-card generation-card");
      card.dataset.itemId = item.id;
      const image = state.images.get(run.id);
      if (!boot.admin_mode && !image && (run.state === "failed" || state.unavailable.has(run.id) || run.retrievalError)) return unavailableNotice(item, run);
      card.dataset.runId = run.id;
      const area = node("div", "image-area");
      if (image) {
        const img = node("img"); img.decoding = "async"; img.src = image.url; img.alt = `AI-generated illustration for ${item.title}`;
        area.append(img);
      } else {
        const box = node("div", "image-state");
        const pending = !terminal.has(run.state);
        box.append(node("span", pending && run.state !== "submission_unknown" ? "spinner" : "state-symbol", pending && run.state !== "submission_unknown" ? "" : run.state === "completed" ? "◈" : "↗"));
        let title = statuses[run.state] || "Illustration in progress";
        let message = "A fresh perspective is on its way. Keep reading while we finish.";
        if (run.state === "completed") {
          title = state.unavailable.has(run.id) ? "Image unavailable here" : run.retrievalError ? "Couldn’t load the illustration" : "Loading your illustration";
          message = state.unavailable.has(run.id) ? "This image is no longer available in this browser. Choose Regenerate image to create another." : run.retrievalError || "Opening your existing image.";
        } else if (run.state === "failed") message = "You can still read the original story below.";
        else if (run.state === "submission_unknown") message = "We’re checking this request before trying anything else. You can keep reading.";
        box.append(node("strong", "", title), node("p", "", message)); area.append(box);
      }
      area.append(node("span", "image-label", "✳ AI-GENERATED ILLUSTRATION"));
      card.append(area);
      const body = node("div", "card-body");
      let details = null;
      const top = node("div", "card-topline reader-card-meta");
      const published = readerDate(item.published);
      if (published) {
        const time = node("time", "article-date", published); time.dateTime = item.published; top.append(time);
      }
      const url = safeSource(item.link);
      if (url) {
        const source = node("a", "article-source", item.display_source ?? new URL(url).hostname.replace(/^www\./, ""));
        source.href = new URL(url).origin; source.target = "_blank"; source.rel = "noopener noreferrer"; top.append(source);
      }
      if (boot.fake_mode) top.append(node("span", "synthetic-label", "SYNTHETIC DEMO"));
      body.append(top, node("h3", "", item.title));
      const excerpt = readerExcerpt(item.excerpt);
      if (excerpt) body.append(node("p", "article-excerpt", excerpt));
      body.append(sourceLink(item.link));
      const controls = node("div", "reader-image-controls");
      const status = node("div", "reader-image-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
      const actions = node("div", "card-actions");
      if (image) {
        if (!image.saved) status.append(node("p", "save-status warning", image.saveError || "Saving in this browser…"));
        if (!image.saved && !state.loadingImages.has(run.id)) actions.append(action("↻ Retry save", async () => {
          await saveImage(run.id, image.blob); image.saved = true; image.saveError = null; pruneSavedImages(); renderRuns();
        }));
      } else if (run.error) {
        const message = boot.admin_mode ? run.error.message : run.state === "submission_unknown" ? "This request is still being checked. The person who shared this demo can help if it stays here." : run.error.retryable ? "We couldn’t finish this image. You can retry the illustration below." : "This image couldn’t be created. Contact the person who shared this demo if you need help.";
        const error = node("div", "error-note", message);
        if (boot.admin_mode) error.append(node("small", "", `${run.error.code} · ${run.error.stage || "illustration"} · Run ${run.id}`));
        status.append(error);
      } else status.append(node("p", "save-status", state.unavailable.has(run.id) ? "No saved image was found in this browser." : run.state === "completed" ? "Checking for a saved image in this browser." : "We’ll save it in this browser until this feed expires."));
      if (run.state === "completed" && !image) {
        if (state.unavailable.has(run.id)) actions.append(action("Regenerate image ↗", () => retryRun(run), true));
        else if (run.retrievalError) actions.append(action("↻ Load image again", async () => { delete run.retrievalError; await recoverImage(run); }));
      }
      if (run.state === "failed" && run.error?.retryable) actions.append(action("↻ Retry illustration", () => retryRun(run), true));
      controls.append(status, actions); body.append(controls);
      if (boot.admin_mode) { details = detailsFor(run); details.open = openDetails.has(run.id); }
      card.append(body); if (details) card.append(details); return card;
    });
    reconcileChildren($("item-list"), cards);
    if (state.currentFeed && !cards.length) {
      const empty = node("p", "reader-no-stories muted", "No illustrations started for this page. ");
      const home = node("a", "source-link", "Load a new feed");
      home.href = `/${boot.admin_mode ? "?admin" : ""}`;
      empty.append(home, " to try again.");
      $("item-list").append(empty);
    }
    updateControls();
  }
  function acceptRuns(runs) {
    if (!liveView()) return;
    const itemIds = new Set(state.items.map((item) => item.id));
    const accepted = [];
    for (const run of runs || []) {
      if (!itemIds.has(run.item_id)) continue;
      const old = state.runs.get(run.id);
      if (old?.retrievalError) run.retrievalError = old.retrievalError;
      if (old?.retryKey) run.retryKey = old.retryKey;
      state.runs.set(run.id, run); accepted.push(run);
    }
    for (const run of accepted) if (run.state === "completed" && !run.retrievalError && latestRunForItem(run.item_id)?.id === run.id) recoverImage(run);
    renderRuns();
    pruneSavedImages();
  }
  function acceptBatch(batch) {
    if (!liveView()) return;
    acceptRuns(batch.runs);
    const active = batch.state === "active" || (batch.runs || []).some((run) => !terminal.has(run.state));
    state.batchId = active ? batch.id : null;
    const visible = (batch.runs || []).filter((run) => state.items.some((item) => item.id === run.item_id));
    const done = visible.filter((run) => terminal.has(run.state)).length;
    $("batch-progress").textContent = active ? visible.length ? `${done} of ${visible.length} illustrations ready. Keep reading.` : "Your other illustrations are still being prepared." : "";
    updateControls();
    schedulePoll();
  }
  function canPoll() { return !state.suspended && !document.hidden && window.navigator?.onLine !== false; }
  function schedulePoll(delay = 2500) {
    clearTimeout(state.pollTimer); state.pollTimer = null;
    if (liveView() && state.batchId && canPoll()) state.pollTimer = setTimeout(pollBatch, Math.max(delay, state.cooldownUntil - Date.now()));
  }
  async function pollBatch() {
    clearTimeout(state.pollTimer); state.pollTimer = null;
    if (!state.batchId || !liveView() || !canPoll()) return;
    if (Date.now() < state.cooldownUntil || state.polling) { schedulePoll(); return; }
    const batchId = state.batchId;
    state.polling = true;
    try {
      const batch = await api(scoped(`/api/batches/${encodeURIComponent(batchId)}`));
      if (state.batchId === batchId && liveView()) { state.pollFailures = 0; acceptBatch(batch); }
    }
    catch (error) {
      if (liveView() && state.batchId === batchId) {
        stopLoader();
        state.pollFailures++;
        $("batch-progress").textContent = "Connection interrupted. Checking your illustrations again shortly.";
        schedulePoll(Math.min(60000, 8000 * 2 ** Math.min(state.pollFailures - 1, 3)));
      }
    }
    finally { state.polling = false; }
  }
  async function retryRun(run) {
    if (state.submitting || !liveView()) return;
    if (state.batchId) { announce("Your other illustrations are still finishing. Please try this image again shortly.", true); return; }
    state.submitting = true; updateControls();
    try {
      // Keep an uncertain retry's identity across the reader's BFCache reload as well as in-place retries.
      const storageKey = retryStorageKey(run);
      const saved = window.sessionStorage.getItem(storageKey);
      if (saved !== null && !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(saved))
        throw new Error("The previous retry request could not be read safely. Keep this page open and refresh to check its result.");
      run.retryKey ||= saved || crypto.randomUUID();
      try {
        window.sessionStorage.setItem(storageKey, run.retryKey);
        if (window.sessionStorage.getItem(storageKey) !== run.retryKey) throw new Error("Storage unavailable");
      } catch { throw new Error("This browser could not save the retry request safely. Enable session storage before trying again."); }
      const batch = await api(scoped(`/api/runs/${encodeURIComponent(run.id)}/retry`), { method: "POST", body: { idempotency_key: run.retryKey } });
      if (!validRetryBatch(batch)) {
        const error = new Error("We couldn’t confirm this image request. Please try this image again to reconnect safely.");
        error.ambiguous = true; throw error;
      }
      if (!liveView()) return;
      clearRetryKey(run);
      acceptBatch(batch); announce("A fresh illustration is on its way.");
    } catch (error) {
      stopLoader();
      if (!error.ambiguous && error.status >= 400 && error.status < 500) clearRetryKey(run);
      throw error;
    }
    finally { state.submitting = false; updateControls(); }
  }
  function retryStorageKey(run) { return `pulsefeed:retry:${state.account.id}:${state.view.id}:${run.id}`; }
  function clearRetryKey(run) {
    try { window.sessionStorage.removeItem(retryStorageKey(run)); } catch { /* Retaining the same key remains safe if storage becomes unavailable. */ }
    delete run.retryKey;
    const current = state.runs.get(run.id); if (current) delete current.retryKey;
  }
  function validRetryBatch(batch) {
    const identifier = (value) => typeof value === "string" && value.length > 0 && value.length <= 200;
    return batch && identifier(batch.id) && ["active", "finished"].includes(batch.state)
      && Array.isArray(batch.runs) && batch.runs.length >= 1 && batch.runs.length <= 3
      && new Set(batch.runs.map((run) => run?.id)).size === batch.runs.length
      && batch.runs.every((run) => run && identifier(run.id) && identifier(run.item_id)
        && Object.hasOwn(statuses, run.state) && run.original && typeof run.original.title === "string"
        && typeof run.original.excerpt === "string" && typeof run.original.link === "string");
  }
  function validView(view) {
    return view && typeof view.id === "string" && view.id !== "new" && view.id.length > 0 && view.id.length <= 200
      && view.url === `/feeds/${encodeURIComponent(view.id)}` && Number.isFinite(view.created_at)
      && Number.isFinite(view.expires_at) && view.expires_at > view.created_at && view.expires_at - view.created_at <= 3600;
  }
  function validFeedResponse(data) {
    return validView(data?.view) && Number.isFinite(data.server_now)
      && data.server_now >= data.view.created_at && data.server_now < data.view.expires_at;
  }
  const rejectionMessages = {
    INVALID_FEED: "This URL isn’t a readable RSS or Atom feed. Check the feed URL and try again.",
    EMPTY_FEED: "This feed has no readable articles yet. Try another RSS or Atom feed.",
    UNSAFE_FEED: "That feed URL can’t be opened here. Try a public RSS or Atom feed.",
    FEED_TOO_LARGE: "This feed is too large to open. Try another RSS or Atom feed.",
    FEED_BUSY: "Feed readers are busy right now. Please try opening your feed again in a moment.",
    VIEW_LIMIT_REACHED: "You have reached the limit for active reading pages. Try again when an earlier page expires; each page lasts one hour."
  };
  function feedRejection(code) {
    if (["VIEW_LIMIT_REACHED", "FEED_BUSY"].includes(code)) announce(rejectionMessages[code], true);
    else urlError(rejectionMessages[code]);
  }
  const pendingLifetime = 10 * 60 * 1000;
  function pendingStorageKey() { return `pulsefeed:pending-feed:${boot.account.id}`; }
  function validPending(request) {
    return request && request.version === 1 && request.account_id === boot.account.id
      && typeof request.url === "string" && request.url.length > 0 && request.url.length <= 2048
      && (Boolean(safeSource(request.url)) || (boot.fake_mode && request.url === "demo"))
      && typeof request.key === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(request.key)
      && Number.isFinite(request.created_at) && request.created_at <= Date.now() + 5000
      && Date.now() - request.created_at < pendingLifetime
      && ["ready", "posting", "done", "rejected"].includes(request.status)
      && (request.status === "done" ? validView(request.view) && Object.keys(request.view).every(key => ["id", "url", "created_at", "expires_at"].includes(key)) : !Object.hasOwn(request, "view"))
      && (request.status === "rejected" ? typeof request.error_code === "string" && Object.hasOwn(rejectionMessages, request.error_code) : !Object.hasOwn(request, "error_code"))
      && Object.keys(request).every(key => ["version", "account_id", "url", "key", "created_at", "status", "view", "error_code"].includes(key));
  }
  function writePending(request) {
    if (!validPending(request)) throw new Error("This feed request has expired. Return home to open it again.");
    const serialized = JSON.stringify(request);
    try {
      window.sessionStorage.setItem(pendingStorageKey(), serialized);
      if (window.sessionStorage.getItem(pendingStorageKey()) !== serialized) throw new Error("Storage unavailable");
    } catch { throw new Error("This browser could not save the feed request safely. Enable session storage and try again; keep this page open if a request is already finishing."); }
  }
  function storedPending() {
    let raw, request;
    try { raw = window.sessionStorage.getItem(pendingStorageKey()); }
    catch { throw new Error("The feed request could not be read. Enable session storage, or return home to start again."); }
    if (raw === null) return null;
    try { request = JSON.parse(raw); } catch { /* Invalid records cannot authorize a request. */ }
    if (!validPending(request)) {
      try { window.sessionStorage.removeItem(pendingStorageKey()); } catch { /* Never clear unrelated preferences. */ }
      return null;
    }
    return request;
  }
  function readPending() {
    const request = storedPending();
    if (!request) throw new Error("This feed request is missing or expired. Return home to open a feed.");
    return request;
  }
  function consumeArrivedRequest() {
    if (!state.view || !state.account || state.expired) return;
    try {
      const key = pendingStorageKey();
      const request = JSON.parse(window.sessionStorage.getItem(key));
      if (validPending(request) && request.status === "done" && request.view.id === state.view.id) {
        window.sessionStorage.removeItem(key);
      }
    } catch { /* Storage denial must not block an authenticated reader. */ }
  }
  function restoreHomeRequest() {
    if (state.view || boot.pending_feed || !state.account || state.expired) return;
    try {
      const request = readPending();
      if ($("feed-url")) {
        $("feed-url").value = request.url;
        if (request.status === "rejected") feedRejection(request.error_code);
        else if (request.status === "posting") announce("This request may still be finishing. Click View RSS to reconnect safely with the same request.");
        else if (request.status === "done" && store.live(request.view)) announce("Your reading page is ready. Click View RSS to open it.");
      }
    } catch { /* Missing, expired, foreign or blocked storage never initiates a request. */ }
  }
  function returnHome() {
    state.navigating = true; stopLoader(); window.location.replace(`/${boot.admin_mode ? "?admin" : ""}`);
  }
  function pendingMessage(message, retry = false) {
    if ($("pending-status")) $("pending-status").textContent = message;
    if ($("pending-retry")) $("pending-retry").hidden = !retry;
    updateControls();
  }
  async function openPending(manual = false) {
    if (!boot.pending_feed || state.submitting || state.expired || state.suspended || state.navigating || !state.account || Date.now() < state.cooldownUntil) return;
    let request;
    try { request = readPending(); }
    catch (error) { stopLoader(); pendingMessage(error.message); return; }
    if ($("pending-feed-source")) $("pending-feed-source").textContent = request.url;
    if (request.status === "rejected") { returnHome(); return; }
    if (request.status === "done") {
      state.navigating = true; updateControls(); window.location.replace(viewTarget(request.view)); return;
    }
    if (request.status === "posting" && !manual) {
      stopLoader(); pendingMessage("This request may already be finishing. Choose Retry opening feed to reconnect safely with the same request.", true); return;
    }
    state.submitting = true; updateControls(); pendingMessage("Fetching stories… Please keep this page open.");
    try {
      request = {...request, status: "posting"}; writePending(request); // Persist before the only automatic POST.
      const data = await api("/api/view-feed", { method: "POST", body: { url: request.url, idempotency_key: request.key } });
      if (!validFeedResponse(data)) {
        const error = new Error("Your feed response was incomplete. Retry opening feed to reconnect safely.");
        error.ambiguous = true; throw error;
      }
      if (state.expired || state.suspended) return;
      const view = {id: data.view.id, url: data.view.url, created_at: data.view.created_at, expires_at: data.view.expires_at};
      writePending({...request, status: "done", view});
      state.navigating = true; pendingMessage("Opening your reading page…");
      window.location.replace(viewTarget(view));
    } catch (error) {
      stopLoader();
      if (!state.expired && !state.suspended && error.status >= 400 && error.status < 500 && Object.hasOwn(rejectionMessages, error.code)) {
        try {
          writePending({...request, status: "rejected", error_code: error.code}); returnHome();
        } catch {
          pendingMessage(`${rejectionMessages[error.code]} The browser could not restore it on the home form. Choose Load a new feed to try another URL.`);
        }
      } else if (!state.expired) pendingMessage(`${error.message} Retry opening feed uses the same request; you can also return home.`, true);
    } finally { state.submitting = false; updateControls(); }
  }
  async function viewFeed(event) {
    event.preventDefault();
    if (state.view || boot.pending_feed || state.submitting || state.navigating || state.expired || state.suspended || !state.account || Date.now() < state.cooldownUntil) return;
    const url = $("feed-url").value.trim(); if (!url) return;
    if (url.length > 2048 || (!safeSource(url) && !(boot.fake_mode && url === "demo"))) { urlError("Enter an HTTP(S) RSS feed URL."); return; }
    state.submitting = true; updateControls();
    $("feed-url").value = url; $("feed-url").removeAttribute("aria-invalid");
    announce("Checking feed…");
    let request;
    try {
      const previous = storedPending();
      if (previous?.url === url && previous.status === "done" && store.live(previous.view)) {
        state.navigating = true; announce("Opening your reading page…");
        window.location.assign(viewTarget(previous.view)); return;
      }
      request = previous?.url === url && ["ready", "posting"].includes(previous.status) ? previous
        : {version: 1, account_id: state.account.id, url, key: crypto.randomUUID(), created_at: Date.now()};
      request = {...request, status: "posting"};
      writePending(request); // Verified durable key before the only feed fetch/admission POST.
      const data = await api("/api/view-feed", { method: "POST", body: { url: request.url, idempotency_key: request.key } });
      if (!validFeedResponse(data)) {
        const error = new Error("Your feed response was incomplete. Click View RSS again to reconnect safely with the same request.");
        error.ambiguous = true; throw error;
      }
      if (state.expired || state.suspended) return;
      const view = {id: data.view.id, url: data.view.url, created_at: data.view.created_at, expires_at: data.view.expires_at};
      writePending({...request, status: "done", view});
      state.navigating = true;
      announce("Opening your reading page…"); window.location.assign(viewTarget(view));
    } catch (error) {
      if (state.expired || state.suspended) return;
      if (request && error.status >= 400 && error.status < 500 && Object.hasOwn(rejectionMessages, error.code)) {
        // A definitive feed-opening rejection did not admit work. A corrected explicit
        // submission can safely use a fresh key, even if this notice cannot be saved.
        try { writePending({...request, status: "rejected", error_code: error.code}); } catch { /* The visible form still reports the rejection. */ }
        feedRejection(error.code);
      } else announce(error.message, true);
    }
    finally { state.submitting = false; updateControls(); }
  }
  $("feed-form")?.addEventListener("submit", viewFeed);
  $("feed-url")?.addEventListener("input", () => {
    $("feed-url").removeAttribute("aria-invalid");
    const status = $("reader-status"); if (status) { status.textContent = ""; status.className = "reader-status small muted"; }
  });
  $("feed-url")?.addEventListener("invalid", () => urlError("Enter an HTTP(S) RSS feed URL."));
  $("pending-retry")?.addEventListener("click", () => { if (!state.expired && !state.submitting && !state.pendingChecking && !state.navigating && Date.now() >= state.cooldownUntil) return start(true); });
  document.addEventListener?.("visibilitychange", () => { store.checkExpired(); updateLoader(); if (document.hidden) { clearTimeout(state.pollTimer); state.pollTimer = null; } else if (state.batchId) schedulePoll(100); });
  window.addEventListener("offline", () => { clearTimeout(state.pollTimer); state.pollTimer = null; });
  window.addEventListener("online", () => { store.checkExpired(); if (state.batchId) schedulePoll(100); });
  window.addEventListener("focus", async () => {
    store.checkExpired();
    if (!state.account || state.expired || state.viewExpired || state.suspended) return;
    try {
      const session = await api("/api/session");
      if (session.account.id !== state.account.id || session.csrf !== state.csrf) {
        // Another tab may have exchanged a link for a different account.
        expireSession(); window.location.reload();
      }
    } catch { /* A 401 is handled by api; an offline focus does not erase the session. */ }
  });
  window.addEventListener("pagehide", () => {
    state.suspended = true; cancelRequests();
    clearTimeout(state.pollTimer); state.pollTimer = null;
    clearInterval(state.cooldownTimer); state.cooldownTimer = null;
    stopLoader(); cancelImageDecodes();
    for (const image of state.images.values()) URL.revokeObjectURL(image.url);
    state.images.clear();
  });
  // Revalidate the reader's expiring view after BFCache restore; pending POST keys survive in sessionStorage.
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });

  async function start(manualPending = false) {
    if (boot.pending_feed) { if (state.submitting || state.pendingChecking) return; state.pendingChecking = true; }
    updateControls();
    if (state.view) {
      if (!validView(state.view) || !store.live(state.view)) { expireView(); return; }
      store.watch(state.view, () => expireView());
      if (boot.initial_feed) showFeed(boot.initial_feed);
    }
    try {
      const session = await api("/api/session");
      if (session.account.id !== boot.account.id || session.csrf !== boot.csrf) { expireSession(); window.location.reload(); return; }
      state.account = session.account; state.csrf = session.csrf;
      if (!state.view && !boot.pending_feed) { $("feed-url")?.removeAttribute("data-initializing"); $("view-feed")?.removeAttribute("data-initializing"); }
      if (state.view) consumeArrivedRequest();
      if (boot.pending_feed) { state.pendingChecking = false; updateControls(); await openPending(manualPending); return; }
      restoreHomeRequest();
      state.storageReady = store.open().then(() => store.sweep()).catch((error) => { state.dbError = error.message; });
      updateControls();
      if (state.view && liveView()) {
        acceptRuns(Array.from(state.runs.values()));
        state.restoring = false; updateLoader();
        if (state.batchId) schedulePoll(100);
      }
      await state.storageReady;
      if (state.dbError && !state.expired && !state.viewExpired) announce(`${state.dbError} Images can still be viewed while this feed is available. Retry saving to keep browser copies until this feed expires.`, true);
    } catch (error) { state.restoring = false; state.pendingChecking = false; stopLoader();
      if (!state.expired && !state.viewExpired) {
        if (boot.pending_feed) pendingMessage(error.message, true);
        else announce(!state.account ? "Couldn’t verify your session. Refresh this page to try again." : error.message, true);
      }
    }
  }
  start();
})();
