/* One-hour browser copies. Cleanup never touches theme, authentication, or other databases. */
(() => {
  "use strict";
  const HOUR = 3600000;
  class ImageStore {
    constructor(serverNow) {
      this.wallStart = Date.now();
      const monotonic = window.performance?.now?.();
      this.monotonicStart = Number.isFinite(monotonic) && monotonic >= 0 ? monotonic : null;
      const navigation = window.performance?.getEntriesByType?.("navigation")?.[0];
      // server_now belongs to the document, not the later deferred-script execution.
      // Include time since its request (conservative for server/transport latency), so
      // a slow document, stylesheet or script cannot give cached images extra life.
      const validStart = (value) => Number.isFinite(value) && value >= 0 && this.monotonicStart !== null && value <= this.monotonicStart;
      const requestStart = validStart(navigation?.requestStart) ? navigation.requestStart
        : validStart(navigation?.responseStart) ? navigation.responseStart : 0;
      const documentAge = this.monotonicStart === null ? 0 : Math.max(0, this.monotonicStart - requestStart);
      this.serverStart = Number.isFinite(serverNow) ? serverNow * 1000 + documentAge : this.wallStart;
      this.db = null; this.opening = null; this.sweeping = null; this.timer = null;
      this.nextRecordExpiry = Infinity; this.watchers = new Set(); this.writes = new Set();
      try {
        this.channel = window.BroadcastChannel ? new window.BroadcastChannel("pulsefeed-image-expiry") : null;
        if (this.channel) this.channel.onmessage = () => { this.maintain(); };
      } catch { this.channel = null; }
    }
    now() {
      const elapsed = Math.max(0, Date.now() - this.wallStart,
        this.monotonicStart === null ? 0 : window.performance.now() - this.monotonicStart);
      return this.serverStart + elapsed;
    }
    live(view) { return view && typeof view.id === "string" && Number.isFinite(view.expires_at) && this.now() < view.expires_at * 1000; }
    requireLive(view) { if (!this.live(view)) throw new Error("This feed’s one-hour window has ended."); }
    key(account, view, runId) { return `${account}:${view.id}:${runId}`; }
    open() {
      if (this.db) return Promise.resolve(this.db);
      if (this.opening) return this.opening;
      this.opening = new Promise((resolve, reject) => {
        if (!window.indexedDB) { reject(new Error("Browser image storage is unavailable.")); return; }
        let settled = false;
        const request = window.indexedDB.open("pulsefeed-images", 2);
        const fail = (message) => { settled = true; reject(new Error(message)); };
        request.onupgradeneeded = () => {
          const db = request.result;
          const store = db.objectStoreNames.contains("images") ? request.transaction.objectStore("images") : db.createObjectStore("images", { keyPath: "key" });
          if (!store.indexNames.contains("expiresAt")) store.createIndex("expiresAt", "expiresAt");
          // Legacy account:run copies keep only their original savedAt + one hour. They are never renewed or read by a new view.
          const cursor = store.openCursor();
          cursor.onsuccess = () => {
            const entry = cursor.result;
            if (!entry) return;
            const record = entry.value;
            if (!Number.isFinite(record.expiresAt)) {
              record.expiresAt = Number.isFinite(record.savedAt) ? record.savedAt + HOUR : 0;
              entry.update(record);
            }
            entry.continue();
          };
        };
        request.onblocked = () => fail("Close older PulseFeed tabs to unlock browser image storage.");
        request.onerror = () => fail("Browser image storage could not be opened.");
        request.onsuccess = () => {
          if (settled) { request.result.close(); return; }
          settled = true; this.db = request.result;
          this.db.onversionchange = () => { this.db?.close(); this.db = null; };
          resolve(this.db);
        };
      }).finally(() => { this.opening = null; });
      return this.opening;
    }
    watch(view, callback) {
      const watcher = { view, callback };
      this.watchers.add(watcher); this.checkExpired(); this.arm();
      return () => { this.watchers.delete(watcher); this.arm(); };
    }
    checkExpired() {
      for (const watcher of this.watchers) {
        if (!this.live(watcher.view)) { this.watchers.delete(watcher); watcher.callback(); }
      }
      for (const write of this.writes) if (!this.live(write.view)) { try { write.transaction.abort(); } catch { /* Already settled. */ } }
    }
    arm() {
      clearTimeout(this.timer); this.timer = null;
      const next = Math.min(this.nextRecordExpiry, ...Array.from(this.watchers, (watcher) => watcher.view.expires_at * 1000));
      if (Number.isFinite(next)) this.timer = setTimeout(() => { this.maintain(); }, Math.max(1, next - this.now()));
    }
    async sweep() {
      if (this.sweeping) return this.sweeping;
      this.sweeping = (async () => {
        const db = await this.open();
        let removed = 0, next = Infinity;
        await new Promise((resolve, reject) => {
          const transaction = db.transaction("images", "readwrite");
          const cursor = transaction.objectStore("images").index("expiresAt").openCursor();
          cursor.onsuccess = () => {
            const entry = cursor.result;
            if (!entry) return;
            if (entry.value.expiresAt <= this.now()) { entry.delete(); removed++; entry.continue(); }
            else next = entry.value.expiresAt; // Sorted index: the next record supplies the nearest expiry.
          };
          transaction.oncomplete = resolve;
          transaction.onerror = transaction.onabort = () => reject(new Error("Expired browser copies could not be cleared yet."));
        });
        this.nextRecordExpiry = next;
        if (removed) { try { this.channel?.postMessage({ type: "expired" }); } catch { /* Other tabs also sweep independently. */ } }
      })().finally(() => { this.sweeping = null; });
      return this.sweeping;
    }
    async maintain() {
      try {
        const preferences = window.localStorage;
        for (let index = preferences.length - 1; index >= 0; index--) {
          const key = preferences.key(index);
          if (key?.startsWith("pulsefeed:selected-feed:")) preferences.removeItem(key);
        }
      } catch { /* Image expiry still works when preference storage is blocked. */ }
      this.checkExpired();
      try { await this.sweep(); }
      catch { this.nextRecordExpiry = this.now() + 60000; }
      this.checkExpired(); this.arm();
    }
    async read(account, view, runId) {
      this.requireLive(view);
      const db = await this.open(); this.requireLive(view);
      return new Promise((resolve, reject) => {
        const transaction = db.transaction("images", "readonly");
        const request = transaction.objectStore("images").get(this.key(account, view, runId));
        request.onsuccess = () => {
          const record = request.result;
          resolve(this.live(view) && record?.accountId === account && record.viewId === view.id && Number.isFinite(record.expiresAt) && record.expiresAt > this.now() ? record.blob : null);
        };
        request.onerror = () => reject(new Error("The saved browser copy could not be read."));
      });
    }
    async save(account, view, runId, blob) {
      this.requireLive(view);
      const db = await this.open(); this.requireLive(view);
      const key = this.key(account, view, runId);
      await new Promise((resolve, reject) => {
        const transaction = db.transaction("images", "readwrite");
        const write = { transaction, view }; this.writes.add(write);
        // A late save inherits the page deadline; refreshing or retrying must never renew it.
        transaction.objectStore("images").put({ key, accountId: account, viewId: view.id, runId, blob,
          savedAt: this.now(), expiresAt: view.expires_at * 1000 });
        transaction.oncomplete = () => { this.writes.delete(write); resolve(); };
        transaction.onerror = transaction.onabort = () => { this.writes.delete(write); reject(new Error("Browser storage write failed or the feed expired.")); };
      });
      if (!this.live(view)) {
        await new Promise((resolve, reject) => {
          const transaction = db.transaction("images", "readwrite"); transaction.objectStore("images").delete(key);
          transaction.oncomplete = resolve; transaction.onerror = transaction.onabort = reject;
        });
        this.requireLive(view);
      }
      this.nextRecordExpiry = Math.min(this.nextRecordExpiry, view.expires_at * 1000); this.arm();
    }
  }
  window.PulseImageStore = ImageStore;
  const element = document.getElementById("storage-bootstrap");
  if (element) {
    const boot = JSON.parse(element.textContent);
    const store = new ImageStore(boot.server_now);
    window.pulseImages = store;
    store.maintain();
    window.addEventListener("focus", () => { store.maintain(); });
    window.addEventListener("pageshow", () => { store.maintain(); });
    document.addEventListener("visibilitychange", () => { store.maintain(); });
  }
})();
