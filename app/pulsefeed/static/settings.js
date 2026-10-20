/* Plain-text prompt editors; article values stay in a separate user message. */
(() => {
  "use strict";
  const field = document.getElementById("flux-prefix");
  if (!field) return;
  const marker = "{{qwen_prompt}}";
  const token = document.getElementById("qwen-prompt-token");
  const status = document.getElementById("flux-template-status");
  let tokenDropPending = false;
  let markEdited = () => {};
  function revealSection(control) {
    const section = control.closest?.("details");
    if (section) section.open = true;
  }
  // Native required/custom validation happens before submit and must reveal the control before browser focus.
  for (const id of ["qwen-settings-form", "flux-settings-form", "content-settings-form"])
    document.getElementById(id)?.addEventListener("invalid", (event) => revealSection(event.target), true);
  function tokenCount(value) { return value.split(marker).length - 1; }
  function staticLength(value) { return Array.from(value.split(marker).join("")).length; }
  function moveMarker(value, cursor, selected, markers = [selected]) {
    // Native carets are UTF-16 offsets. Snap outside a different token before inserting.
    for (const other of markers) {
      if (other === selected) continue;
      let position = value.indexOf(other);
      while (position !== -1) {
        if (cursor > position && cursor < position + other.length) cursor = position + other.length;
        position = value.indexOf(other, position + other.length);
      }
    }
    if (cursor > 0 && /[\uD800-\uDBFF]/.test(value[cursor - 1]) && /[\uDC00-\uDFFF]/.test(value[cursor] || "")) cursor++;
    let adjusted = cursor, index = value.indexOf(selected);
    while (index !== -1) {
      if (index < cursor) adjusted -= Math.min(selected.length, cursor - index);
      index = value.indexOf(selected, index + selected.length);
    }
    const plain = value.split(selected).join("");
    adjusted = Math.max(0, Math.min(adjusted, plain.length));
    return { value: plain.slice(0, adjusted) + selected + plain.slice(adjusted), cursor: adjusted + selected.length };
  }
  function moveToken(value, cursor) { return moveMarker(value, cursor, marker); }
  function render(message = "") {
    const value = field.value;
    const count = tokenCount(value);
    const length = staticLength(value);
    const error = count !== 1 ? "Include exactly one {{qwen_prompt}} marker. Add it if missing or remove duplicates."
      : length > 2000 ? "Use no more than 2,000 style characters around the Qwen prompt token." : "";
    field.setCustomValidity(error);
    field.setAttribute("aria-invalid", String(Boolean(error)));
    document.getElementById("flux-template-count").textContent = `${length.toLocaleString()} / 2,000 style characters`;
    const index = value.indexOf(marker);
    document.getElementById("flux-preview-before").textContent = index < 0 ? value : value.slice(0, index);
    document.getElementById("flux-preview-after").textContent = index < 0 ? "" : value.slice(index + marker.length);
    token.hidden = count !== 1;
    status.textContent = error || message || "The generated Qwen prompt will replace this token; text on both sides is preserved.";
    status.classList.toggle("invalid", Boolean(error));
  }
  token.addEventListener("click", () => {
    const index = field.value.indexOf(marker);
    if (index >= 0) { field.focus(); field.setSelectionRange(index, index + marker.length); }
  });
  token.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("text/plain", marker);
    event.dataTransfer.setData("application/x-pulsefeed-token", "qwen");
    event.dataTransfer.effectAllowed = "copyMove";
  });
  field.addEventListener("drop", (event) => {
    // Let the browser locate its native textarea caret and insert plain text.
    // The following input event removes the previous marker without touching surrounding text.
    tokenDropPending = event.dataTransfer.getData("application/x-pulsefeed-token") === "qwen";
  });
  field.addEventListener("input", () => {
    if (tokenDropPending) {
      tokenDropPending = false;
      const caret = field.selectionEnd ?? field.value.length;
      const inserted = field.value.slice(caret - marker.length, caret) === marker ? caret - marker.length : field.value.lastIndexOf(marker);
      if (inserted >= 0) {
        const result = moveToken(field.value, inserted);
        field.value = result.value;
        field.setSelectionRange(result.cursor, result.cursor);
      }
      render("Qwen prompt token moved. Preview updated.");
    } else render();
  });
  token.addEventListener("dragend", () => { tokenDropPending = false; });
  document.getElementById("flux-settings-form").addEventListener("submit", (event) => {
    render();
    if (!field.checkValidity()) { event.preventDefault(); event.stopImmediatePropagation(); revealSection(field); field.reportValidity(); }
  }, true);
  const articleDivider = "--- ARTICLE CONTEXT ---";
  function joinQwen(system, article) { return `${system}\n${articleDivider}\n${article}`; }
  function splitQwen(value) {
    const boundaries = Array.from(value.matchAll(/^--- ARTICLE CONTEXT ---\r?$/gm));
    if (boundaries.length !== 1) return null;
    const match = boundaries[0];
    let system = value.slice(0, match.index), article = value.slice(match.index + match[0].length);
    // Remove only the added framing newline. Role text is never trimmed.
    if (system.endsWith("\n")) system = system.slice(0, -1);
    if (article.startsWith("\n")) article = article.slice(1);
    return {system, article, articleStart: match.index + match[0].length + (value[match.index + match[0].length] === "\n" ? 1 : 0)};
  }
  function displayNewlines(value) { return value.replace(/\r\n?/g, "\n"); }
  function setupArticleEditor() {
    const editor = document.getElementById("qwen-prompt-editor");
    if (!editor) return;
    const systemField = document.getElementById("qwen-template"), articleField = document.getElementById("qwen-article-template");
    const seed = document.getElementById("qwen-editor-data");
    const source = seed?.textContent ? JSON.parse(seed.textContent) : {system: systemField.value, article: articleField.value};
    // JSON preserves original CRLF values that native textarea.value displays as LF.
    systemField.value = source.system; articleField.value = source.article;
    editor.value = joinQwen(displayNewlines(source.system), displayNewlines(source.article));
    const preview = document.getElementById("qwen-article-preview"), articleStatus = document.getElementById("qwen-article-status");
    const fields = [
      {marker: "{{article_title}}", label: "Article title", id: "article-title-token", add: "add-article-title", drag: "article-title"},
      {marker: "{{article_excerpt}}", label: "Article excerpt", id: "article-excerpt-token", add: "add-article-excerpt", drag: "article-excerpt"}
    ];
    const markers = fields.map(part => part.marker);
    for (const part of fields) part.chip = document.getElementById(part.id);
    let dropped = null;
    function renderArticle(message = "") {
      const parts = splitQwen(editor.value);
      const article = parts?.article || "";
      const systemValue = parts && displayNewlines(systemField.value) === parts.system ? systemField.value : parts?.system || "";
      const articleValue = parts && displayNewlines(articleField.value) === article ? articleField.value : article;
      const length = Array.from(markers.reduce((text, marker) => text.split(marker).join(""), articleValue)).length;
      const counts = fields.map(part => article.split(part.marker).length - 1);
      const unsupported = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\uD800-\uDFFF]/u.test(article);
      const error = !parts ? "Keep exactly one divider line: --- ARTICLE CONTEXT ---. Instructions stay above it; article context stays below."
        : markers.some(marker => parts.system.includes(marker)) ? "Article title and excerpt tokens must stay below the divider. Move them into the article context."
        : !parts.system.trim() ? "Add system instructions above the divider."
        : Array.from(systemValue).length > 4000 ? "Use no more than 4,000 characters above the divider."
        : unsupported ? "Remove unsupported control characters from the article context."
        : counts.some(count => count !== 1) ? "Include exactly one article title token and one article excerpt token below the divider. Add missing tokens or remove duplicates."
        : length > 2000 ? "Use no more than 2,000 characters around the article tokens." : "";
      editor.setCustomValidity(error); editor.setAttribute("aria-invalid", String(Boolean(error)));
      document.getElementById("qwen-article-count").textContent = `${parts ? Array.from(systemValue).length.toLocaleString() : "—"} / 4,000 instruction characters · ${length.toLocaleString()} / 2,000 article characters`;
      for (const part of fields) document.getElementById(part.add).hidden = editor.value.includes(part.marker);
      const fragments = []; let cursor = 0;
      const pattern = /\{\{article_(?:title|excerpt)\}\}/g;
      for (const match of article.matchAll(pattern)) {
        if (match.index > cursor) { const text = document.createElement("span"); text.textContent = article.slice(cursor, match.index); fragments.push(text); }
        const part = fields.find(part => part.marker === match[0]);
        const chip = counts[fields.indexOf(part)] === 1 ? part.chip : document.createElement("span");
        chip.className = "qwen-prompt-token prompt-preview-token"; chip.textContent = JSON.stringify(part.label);
        chip.title = "A JSON-quoted article value replaces this token"; fragments.push(chip);
        cursor = match.index + match[0].length;
      }
      if (cursor < article.length) { const text = document.createElement("span"); text.textContent = article.slice(cursor); fragments.push(text); }
      preview.replaceChildren(...fragments);
      articleStatus.textContent = error || message || "Both article tokens are ready.";
      articleStatus.classList.toggle("invalid", Boolean(error));
      if (!error) {
        // Keep saved role bytes until that role changes in the native LF-normalized editor.
        systemField.value = systemValue;
        articleField.value = articleValue;
      }
      return !error;
    }
    function edited(message = "") { renderArticle(message); markEdited(editor); }
    for (const part of fields) {
      const chip = part.chip;
      document.getElementById(part.add).addEventListener("click", () => {
        const parts = splitQwen(editor.value);
        if (!parts || editor.value.includes(part.marker)) { renderArticle(); editor.focus(); return; }
        const cursor = editor.selectionStart ?? editor.value.length;
        const target = cursor >= parts.articleStart ? cursor - parts.articleStart : parts.article.length;
        const result = moveMarker(parts.article, target, part.marker, markers);
        editor.value = editor.value.slice(0, parts.articleStart) + result.value;
        editor.focus(); editor.setSelectionRange(parts.articleStart + result.cursor, parts.articleStart + result.cursor);
        edited(`${part.label} token added.`);
      });
      chip.addEventListener("click", () => {
        const parts = splitQwen(editor.value); if (!parts) return;
        const index = parts.article.indexOf(part.marker);
        if (index >= 0) { editor.focus(); editor.setSelectionRange(parts.articleStart + index, parts.articleStart + index + part.marker.length); }
      });
      chip.addEventListener("dragstart", event => {
        event.dataTransfer.setData("text/plain", part.marker);
        event.dataTransfer.setData("application/x-pulsefeed-token", part.drag); event.dataTransfer.effectAllowed = "copyMove";
      });
      chip.addEventListener("dragend", () => { dropped = null; });
    }
    editor.addEventListener("drop", event => {
      dropped = fields.find(part => part.drag === event.dataTransfer.getData("application/x-pulsefeed-token")) || null;
    });
    editor.addEventListener("input", () => {
      let message = "";
      if (dropped) {
        const part = dropped; dropped = null;
        const caret = editor.selectionEnd ?? editor.value.length;
        const inserted = editor.value.slice(caret - part.marker.length, caret) === part.marker ? caret - part.marker.length : editor.value.lastIndexOf(part.marker);
        if (inserted >= 0) {
          // Undo the native copy before moving; this also restores a token/divider it landed inside.
          const original = editor.value.slice(0, inserted) + editor.value.slice(inserted + part.marker.length);
          const parts = splitQwen(original);
          if (!parts || inserted < parts.articleStart) {
            editor.value = original;
            message = "Article tokens can only move below the divider. The token was not moved.";
          } else {
            const result = moveMarker(parts.article, inserted - parts.articleStart, part.marker, markers);
            editor.value = original.slice(0, parts.articleStart) + result.value;
            editor.setSelectionRange(parts.articleStart + result.cursor, parts.articleStart + result.cursor);
          }
        }
      }
      edited(message);
    });
    document.getElementById("qwen-settings-form").addEventListener("submit", event => {
      if (!renderArticle()) { event.preventDefault(); event.stopImmediatePropagation(); revealSection(editor); editor.reportValidity(); }
    }, true);
    renderArticle();
  }
  function setupSectionSaves() {
    const bootstrap = document.getElementById("admin-bootstrap");
    if (!bootstrap?.textContent) return;
    const boot = JSON.parse(bootstrap.textContent);
    const sections = [
      {key: "qwen", fields: [["qwen_template", "qwen-template"], ["qwen_article_template", "qwen-article-template"]]},
      {key: "flux", fields: [["flux_prefix", "flux-prefix"]]},
      {key: "content", fields: [["fallback_prompt", "fallback-prompt"], ["blocked_terms", "blocked-terms"]]}
    ].map(section => ({...section, form: document.getElementById(`${section.key}-settings-form`), busy: false, until: 0, timer: null, request: null}));
    const unconfirmedSave = "The save could not be confirmed. Reload this page to check the stored settings before saving again.";
    let expired = false, suspended = false;
    function controls(section) {
      const blocked = expired || suspended || section.busy || Date.now() < section.until;
      section.form.querySelectorAll("input,textarea,button,select").forEach(control => { control.disabled = blocked; });
      section.form.setAttribute("aria-busy", String(section.busy));
    }
    function message(section, text, error = false) {
      const target = document.getElementById(`${section.key}-settings-feedback`);
      target.textContent = text; target.className = `settings-section-feedback small${error ? " error" : " muted"}`;
    }
    function resumeCooldown(section) {
      clearTimeout(section.timer); section.timer = null;
      controls(section);
      const remaining = section.until - Date.now();
      if (!expired && !suspended && remaining > 0)
        section.timer = setTimeout(() => { section.timer = null; resumeCooldown(section); }, remaining);
    }
    function cancelSave(section) {
      if (!section.request) return;
      const request = section.request;
      // Detach ownership before aborting: a late response must not update a restored page or a newer save.
      section.request = null; section.busy = false;
      request.abort();
      if (!expired) message(section, unconfirmedSave, true);
    }
    markEdited = control => {
      const section = sections.find(section => section.fields.some(([, id]) => document.getElementById(id) === control) || (section.key === "qwen" && control === document.getElementById("qwen-prompt-editor")));
      if (!expired && section?.savedBody && (section.fields.some(([key, id]) => document.getElementById(id).value !== section.savedBody[key])
          || (section.key === "qwen" && document.getElementById("qwen-prompt-editor").value !== displayNewlines(joinQwen(section.savedBody.qwen_template, section.savedBody.qwen_article_template)))))
        message(section, "Unsaved changes.");
    };
    function expire() {
      if (expired) return;
      expired = true; sections.forEach(section => { clearTimeout(section.timer); section.timer = null; cancelSave(section); controls(section); });
      const dashboard = document.getElementById("admin-dashboard"), access = document.getElementById("admin-access-expired");
      if (dashboard) dashboard.hidden = true;
      if (access) access.hidden = false;
      document.getElementById("usage-rows")?.replaceChildren();
      window.dispatchEvent?.(new Event("pulsefeed:admin-expired"));
    }
    window.addEventListener("pulsefeed:admin-expired", expire);
    window.addEventListener("pagehide", () => {
      suspended = true;
      sections.forEach(section => { clearTimeout(section.timer); section.timer = null; cancelSave(section); controls(section); });
    });
    window.addEventListener("pageshow", () => {
      suspended = false;
      sections.forEach(resumeCooldown);
    });
    for (const section of sections) {
      for (const [, id] of section.fields) document.getElementById(id).addEventListener("input", () => markEdited(document.getElementById(id)));
      section.form.addEventListener("submit", async event => {
        event.preventDefault();
        if (expired || suspended || section.busy || Date.now() < section.until) return;
        if (!section.form.checkValidity()) { revealSection(section.form); section.form.reportValidity(); return; }
        const body = Object.fromEntries(section.fields.map(([key, id]) => [key, document.getElementById(id).value]));
        section.savedBody = null;
        section.busy = true; controls(section); message(section, "Saving…");
        const controller = new AbortController();
        let rejectCancellation;
        const cancelled = new Promise((_, reject) => { rejectCancellation = reject; });
        const request = {timer: null, abort() {
          if (controller.signal.aborted) return;
          clearTimeout(request.timer);
          controller.abort();
          rejectCancellation(new Error(unconfirmedSave));
        }};
        section.request = request;
        // One deadline covers both headers and body. An aborted POST may already have committed; never replay it.
        request.timer = setTimeout(request.abort, 30000);
        try {
          let response;
          try { response = await Promise.race([fetch(`/api/admin/settings/${section.key}`, {method: "POST", credentials: "same-origin", cache: "no-store",
            headers: {Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": boot.csrf}, body: JSON.stringify(body), signal: controller.signal}), cancelled]); }
          catch { throw new Error(unconfirmedSave); }
          if (section.request !== request) return;
          if (controller.signal.aborted) throw new Error(unconfirmedSave);
          if (response.status === 401) { expire(); return; }
          let data;
          try { data = await Promise.race([response.json(), cancelled]); }
          catch {
            if (controller.signal.aborted) throw new Error(unconfirmedSave);
            if (response.ok) throw new Error("The save response could not be confirmed. Reload this page before saving again.");
            data = {};
          }
          if (section.request !== request) return;
          if (controller.signal.aborted) throw new Error(unconfirmedSave);
          if (!response.ok) {
            if (response.status === 403 && data.error?.code !== "CSRF_FAILED") { expire(); return; }
            let text = data.error?.message || "These settings could not be saved. Please try again.";
            if (response.status === 429) {
              const raw = response.headers.get("Retry-After");
              const seconds = Math.min(3600, Math.max(1, Number(raw) || Math.ceil((Date.parse(raw) - Date.now()) / 1000) || 60));
              section.until = Date.now() + seconds * 1000;
              resumeCooldown(section);
              text += ` Try again in ${seconds} seconds.`;
            }
            throw new Error(text);
          }
          if (!data || ![0, 1].includes(data.flux_template_version) || !["qwen_template", "qwen_article_template", "flux_prefix", "fallback_prompt", "blocked_terms"].every(key => typeof data[key] === "string")
              || !section.fields.every(([key]) => data[key] === body[key])) throw new Error("The saved settings could not be confirmed. Reload this page before saving again.");
          if (expired) return;
          if (section.key === "flux" && document.getElementById("flux-legacy-warning")) document.getElementById("flux-legacy-warning").hidden = true;
          // Other sections' returned values must never replace their unsaved drafts.
          section.savedBody = body;
          message(section, "Saved. Future runs will use these settings.");
        } catch (error) { if (!expired && section.request === request) message(section, error.message, true); }
        finally {
          clearTimeout(request.timer);
          if (section.request === request) { section.request = null; section.busy = false; controls(section); }
        }
      });
    }
  }
  setupArticleEditor();
  setupSectionSaves();
  render();
})();
