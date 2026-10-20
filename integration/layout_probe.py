"""Local-only browser layout probe; never imported by the production app.

Run with the app venv and PYTHONPATH=app, using this module's factory instead
of the production factory. LAYOUT_PROBE_DELAY_MS delays async assets/reads.
The probe runs for up to600seconds, freezing3seconds after reader readiness
(or5seconds on home/settled pending; active pending continues until navigation); pagehide saves public geometry in the dedicated
sessionStorage key pulsefeed:layout-probe:geometry:v2 (last three document reports).
The hidden JSON script exposes browser-measured shifts without recording text,
credentials, URLs, images, or visitor content. Stop this server after testing.
"""
import asyncio
import os

from fastapi import Request
from fastapi.responses import Response
from pulsefeed.main import create_app


PROBE = r"""
(() => {
  const storageKey = 'pulsefeed:layout-probe:geometry:v2';
  const limits = {milliseconds: 600000, geometry: 64, shifts: 128, reveal: 96, nodes: 12};
  const result = {schema_version: 2, supported: false, cls: 0, shifts: [], first: null, last: null,
    samples: 0, firstScrollY: null, lastScrollY: null, geometryChanges: [], reveal: [],
    dropped: {geometry: 0, shifts: 0, reveal: 0}, page: 'other', finished: false, reason: null,
    appLoaded: false, sessionObserved: false, readyAt: null, elapsed: 0,
    phaseDurations: {holding: 0, revealing: 0, ready: 0},
    frameTiming: {frames: 0, maxInterval: 0, over50ms: 0}};
  const target = document.createElement('script');
  target.type = 'application/json'; target.id = 'layout-probe-result'; document.head.append(target);
  const history = document.createElement('script');
  history.type = 'application/json'; history.id = 'layout-probe-history';
  history.textContent = JSON.stringify({schema_version: 2, reports: []});
  try {
    const saved = sessionStorage.getItem(storageKey);
    if (saved && saved.length <= 1500000) {
      const previous = JSON.parse(saved);
      if (previous?.schema_version === 2 && Array.isArray(previous.reports))
        history.textContent = JSON.stringify({schema_version: 2, reports: previous.reports.slice(-3)});
    }
  } catch { /* No report history is preferable to interfering with the reader. */ }
  document.head.append(history);
  const started = performance.now();
  const navigation = performance.getEntriesByType('navigation')[0];
  result.navigation = ['navigate', 'reload', 'back_forward', 'prerender'].includes(navigation?.type) ? navigation.type : 'unknown';
  const round = value => Number.isFinite(value) ? Math.round(value * 1000) / 1000 : null;
  const rect = (value, documentCoordinates = false) => ({x: round(value.x + (documentCoordinates ? scrollX : 0)),
    y: round(value.y + (documentCoordinates ? scrollY : 0)), width: round(value.width), height: round(value.height)});
  const selectors = ['.reader-topbar', '.reader-hero', '#feed-form', '#feed-url', '#reader-status',
    '.reader-pending', '#pending-status', '#feed-content', '.reader-feed-header-card', '.reader-canvas',
    '#item-list', '#item-list .reader-card', '#item-list .image-area', '#item-list .card-body', '.page-footer'];
  const collections = {articles: '#item-list .reader-card', imageAreas: '#item-list .image-area',
    cardBodies: '#item-list .card-body', images: '#item-list .image-area img'};
  let frame = null, observer = null, frozen = false, previousGeometry = null, previousLifecycle = null;
  let lastPublish = 0, lastRevealRecord = 0, previousFrame = null, previousFrameRevealing = false;
  let previousPhase = null, phaseObservedAt = null, persisted = false;
  const capped = (array, value, maximum, key) => {
    if (array.length >= maximum) { array.splice(1, 1); result.dropped[key]++; }
    array.push(value);
  };
  const publish = () => { target.textContent = JSON.stringify(result); lastPublish = performance.now(); };
  const visible = node => Boolean(node && node.getClientRects().length);
  function sample() {
    const current = Object.fromEntries(selectors.map(selector => {
      const node = document.querySelector(selector);
      return [selector, visible(node) ? rect(node.getBoundingClientRect(), true) : null];
    }));
    current.all = Object.fromEntries(Object.entries(collections).map(([name, selector]) => [name,
      Array.from(document.querySelectorAll(selector)).slice(0, limits.nodes).map(node => rect(node.getBoundingClientRect(), true))]));
    current.viewport = {width: innerWidth, height: innerHeight, dpr: round(devicePixelRatio)};
    return current;
  }
  function lifecycle(time) {
    const list = document.querySelector('#item-list');
    const loader = document.querySelector('#fun-loader');
    const cards = Array.from(document.querySelectorAll('#item-list .reader-card')).slice(0, limits.nodes);
    const images = Array.from(document.querySelectorAll('#item-list .image-area img')).slice(0, limits.nodes);
    const spinner = loader?.querySelector('.fun-spinner');
    const allowedPhase = value => ['holding', 'revealing', 'ready', 'working', 'settling', 'idle'].includes(value) ? value : null;
    const duration = list?.style.getPropertyValue('--reveal-duration').trim() || '';
    return {time: round(time), phase: allowedPhase(list?.dataset.reveal), inert: Boolean(list?.inert),
      revealDuration: /^\d+(?:\.\d+)?(?:ms|s)$/.test(duration) ? duration : null,
      loaderPhase: allowedPhase(loader?.dataset.phase), loaderActive: loader?.dataset.active === 'true',
      loaderOpacity: visible(loader) ? round(Number(getComputedStyle(loader).opacity)) : 0,
      spinnerOpacity: visible(spinner) ? round(Number(getComputedStyle(spinner).opacity)) : 0,
      cards: cards.map(card => ({opacity: round(Number(getComputedStyle(card).opacity)),
        blurred: getComputedStyle(card).filter !== 'none'})),
      imageCount: images.length, imageComplete: images.filter(image => image.complete && image.naturalWidth > 0).length,
      imageFailed: images.filter(image => image.complete && !image.naturalWidth).length,
      imagesSettled: images.every(image => image.complete)};
  }
  function shiftEntries(entries) {
    for (const entry of entries) {
      if (!entry.hadRecentInput) result.cls += entry.value;
      capped(result.shifts, {value: entry.value, hadRecentInput: entry.hadRecentInput, time: round(entry.startTime),
        scroll: {x: round(scrollX), y: round(scrollY)}, sources: (entry.sources || []).slice(0, 5).map(source => ({
          element: source.node?.tagName || null,
          before: rect(source.previousRect, true), after: rect(source.currentRect, true)
        }))}, limits.shifts, 'shifts');
    }
  }
  if (typeof PerformanceObserver !== 'undefined' && PerformanceObserver.supportedEntryTypes?.includes('layout-shift')) {
    result.supported = true;
    observer = new PerformanceObserver(list => { if (!frozen) { shiftEntries(list.getEntries()); publish(); } });
    observer.observe({type: 'layout-shift', buffered: true});
  }
  document.addEventListener('load', event => {
    if (event.target?.tagName === 'SCRIPT' && (event.target.getAttribute('src') || '').split('?')[0].endsWith('/app.js')) result.appLoaded = true;
  }, true);
  function persist() {
    // This dedicated key contains only this report's public geometry, never page text or application storage.
    if (persisted) return;
    try {
      const previous = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      const reports = previous?.schema_version === 2 && Array.isArray(previous.reports) ? previous.reports.slice(-2) : [];
      reports.push(result);
      sessionStorage.setItem(storageKey, JSON.stringify({schema_version: 2, reports})); persisted = true;
    } catch { /* Reporting must never affect the app. */ }
  }
  function finish(reason) {
    if (frozen) return;
    if (observer) shiftEntries(observer.takeRecords());
    frozen = true; cancelAnimationFrame(frame); observer?.disconnect();
    result.finished = true; result.reason = reason; result.elapsed = round(performance.now() - started);
    publish(); persist();
  }
  function tick(time) {
    if (frozen) return;
    const current = sample();
    const hasReader = current['#feed-content'] || current['.reader-hero'] || current['.reader-pending'];
    const elapsed = time - started;
    result.page = current['.reader-pending'] ? 'pending' : current['#feed-content'] ? 'feed' : current['.reader-hero'] ? 'home' : 'other';
    result.elapsed = round(elapsed);
    if (hasReader) {
      result.first ||= current; result.last = current; result.samples++;
      result.firstScrollY ??= round(scrollY); result.lastScrollY = round(scrollY);
      const fingerprint = JSON.stringify(current);
      if (fingerprint !== previousGeometry) {
        const type = previousGeometry === null ? 'initial' : 'within-document';
        capped(result.geometryChanges, {time: round(time), type, scroll: {x: round(scrollX), y: round(scrollY)}, geometry: current}, limits.geometry, 'geometry');
        previousGeometry = fingerprint;
        result.readyAt = null;
      }
    }
    const state = lifecycle(time);
    if (previousPhase && phaseObservedAt !== null) result.phaseDurations[previousPhase] = round(result.phaseDurations[previousPhase] + time - phaseObservedAt);
    previousPhase = Object.hasOwn(result.phaseDurations, state.phase) ? state.phase : null; phaseObservedAt = time;
    const lifecycleKey = JSON.stringify({...state, time: 0});
    if (state.phase && (lifecycleKey !== previousLifecycle || time - lastRevealRecord >= 1000)) {
      // Animation opacity is sampled at most4Hz; phase changes are always retained.
      const last = result.reveal.at(-1);
      if (!last || last.phase !== state.phase || time - lastRevealRecord >= 250) {
        capped(result.reveal, state, limits.reveal, 'reveal'); lastRevealRecord = time; previousLifecycle = lifecycleKey;
      }
    }
    const revealing = state.phase === 'revealing' && !document.hidden;
    if (revealing && previousFrameRevealing && previousFrame !== null) {
      const interval = time - previousFrame;
      result.frameTiming.frames++; result.frameTiming.maxInterval = Math.max(result.frameTiming.maxInterval, round(interval));
      if (interval > 50) result.frameTiming.over50ms++;
    }
    previousFrame = time; previousFrameRevealing = revealing;
    if (!result.sessionObserved) result.sessionObserved = performance.getEntriesByType('resource').some(entry => {
      try { return new URL(entry.name).pathname === '/api/session'; } catch { return false; }
    });
    const bootstrapSettled = result.appLoaded && result.sessionObserved;
    // An early reveal can leave later cards generating. Observe those arrivals too,
    // rather than freezing the geometry report after only the first three images.
    const readerReady = result.page === 'feed' && bootstrapSettled && state.phase === 'ready'
      && !state.inert && !state.loaderActive && state.loaderOpacity <= .02 && state.imagesSettled
      && state.imageComplete === state.cards.length;
    const nonFeedReady = bootstrapSettled && (result.page === 'home' || result.page === 'pending' && !state.loaderActive);
    const stable = !document.hidden && (readerReady || nonFeedReady);
    if (stable) result.readyAt ??= time;
    else result.readyAt = null;
    if (elapsed >= limits.milliseconds) { finish('timeout'); return; }
    if (stable && time - result.readyAt >= (result.page === 'feed' ? 3000 : 5000)) { finish('settled'); return; }
    if (time - lastPublish >= 500) publish();
    frame = requestAnimationFrame(tick);
  }
  document.addEventListener('visibilitychange', () => { previousFrame = null; previousFrameRevealing = false; result.readyAt = null; });
  window.addEventListener('pagehide', () => {
    if (!frozen) {
      const current = sample(); result.first ||= current; result.last = current; result.lastScrollY = round(scrollY);
      finish('navigation');
    } else persist();
  });
  frame = requestAnimationFrame(tick); publish();
})();
"""


def create_probe():
    app = create_app()
    delay = min(3, max(0, float(os.getenv("LAYOUT_PROBE_DELAY_MS", "0")) / 1000))

    @app.get("/__layout_probe.js")
    async def probe_script():
        return Response(PROBE, media_type="application/javascript", headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def instrument(request: Request, call_next):
        path = request.url.path
        if delay and request.method == "GET" and (
            path.startswith("/api/") or path.endswith(("app.js", ".ttf", ".woff2"))
        ):
            await asyncio.sleep(delay)
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            body = b"".join([chunk async for chunk in response.body_iterator])
            body = body.replace(b"<head>", b'<head><script src="/__layout_probe.js"></script>', 1)
            headers = dict(response.headers)
            headers["content-length"] = str(len(body))
            return Response(body, status_code=response.status_code, headers=headers)
        return response

    return app
