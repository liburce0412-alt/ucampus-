/* Run only in the authorized U-campus WebView after CDP availability is confirmed. */
(() => {
  "use strict";
  if (window.__ucampusProbe) return { installed: true, reused: true };
  const rows = [], restores = [], hooked = [], missing = [], counts = {};
  let lastProgress = -Infinity;
  let dropped = 0;
  function emit(kind, data) {
    if (data.name === "UCPcmPlugin.writePcmData") { counts.pcm_chunks = (counts.pcm_chunks || 0) + 1; return; }
    if (data.name === "UCAudioPlugin.onStatus" && data.status === 5) {
      counts.playback_progress = (counts.playback_progress || 0) + 1;
      if (performance.now() - lastProgress < 1000) return;
      lastProgress = performance.now();
    }
    if (rows.length >= 1000) { rows.shift(); dropped++; }
    rows.push({ ms: performance.now(), kind, ...data });
  }
  function route(value) {
    try {
      const url = new URL(String(value), location.href);
      return { origin: url.origin, path: url.pathname.replace(/[A-Za-z0-9_-]{32,}/g, "[opaque]"), query_keys: [...url.searchParams.keys()] };
    } catch (_) { return { invalid_url: true }; }
  }
  function wrap(object, name, label, describe) {
    const original = object && object[name];
    if (typeof original !== "function") return;
    const wrapped = function (...args) {
      try { emit("bridge", { name: label + "." + name, ...describe(args) }); } catch (_) { }
      return Reflect.apply(original, this, args);
    };
    object[name] = wrapped;
    if (object[name] !== wrapped) return;
    hooked.push(label + "." + name);
    restores.push(() => { if (object[name] === wrapped) object[name] = original; });
  }
  const plugins = [
    ["ucaudio", "UCAudioPlugin", ["prepare", "play", "pause", "stop", "release"], "onStatus"],
    ["ucclio", "UCClioPlugin", ["start", "stop", "cancel"], "onStatus"],
    ["ucsoe", "UCSOEPlugin", ["start", "stop", "cancel", "result"], "onStatus"],
    ["ucpcm", "UCPcmPlugin", ["create", "preparePcm", "writePcmData", "start", "stop", "release"], "_onStatus"],
    ["uchttptask", "UCHttpTaskPlugin", ["backgroundRequest", "backgroundResponse", "backgroundCancel"], null],
  ];
  const map = window.cordova && window.cordova.define && window.cordova.define.moduleMap;
  for (const [slug, name, methods, statusName] of plugins) {
    const entry = map && map[`cordova-plugin-${slug}.${name}`];
    // Requiring a dormant Cordova module can initialize native state; only use already built exports.
    if (!entry || entry.factory || !entry.exports) { missing.push(name); continue; }
    const exported = entry.exports;
    const target = typeof exported === "function" ? exported.prototype : exported;
    for (const method of methods) {
      wrap(target, method, name, args => {
        const metadata = { argc: args.length };
        if (name === "UCAudioPlugin" && method === "prepare") metadata.media = route(args[0]);
        if (name === "UCPcmPlugin" && method === "create") metadata.format = args.slice(0, 3).map(x => typeof x === "number" ? x : null);
        // PCM bytes, request bodies, callback IDs, reference text and authentication are not collected.
        return metadata;
      });
    }
    if (statusName) wrap(exported, statusName, name, args => {
      const type = name === "UCPcmPlugin" ? args[0] : args[1];
      const value = name === "UCPcmPlugin" ? args[1] : args[2];
      return { status: typeof type === "number" ? type : null, numeric_value: typeof value === "number" ? value : null };
    });
  }
  let observer;
  if (typeof PerformanceObserver !== "undefined") {
    observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) emit("resource", { ...route(entry.name), initiator: entry.initiatorType, duration_ms: entry.duration });
    });
    try { observer.observe({ type: "resource", buffered: false }); }
    catch (_) { observer.disconnect(); }
  }
  window.__ucampusProbe = {
    drain() { const output = { events: rows.splice(0), dropped, counts: { ...counts } }; dropped = 0; for (const key of Object.keys(counts)) delete counts[key]; return output; },
    stop() { for (const restore of restores.reverse()) restore(); if (observer) observer.disconnect(); delete window.__ucampusProbe; return { stopped: true }; },
  };
  return { installed: true, hooked, missing, page: route(location.href), altered_requests: false };
})()
