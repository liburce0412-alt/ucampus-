/* Observe one already-debuggable app WebView through an authorized ADB forward. */
const fs = require("node:fs");
const path = require("node:path");

function safeRoute(value) {
  try {
    const url = new URL(value);
    return { origin: url.origin, path: url.pathname.replace(/[A-Za-z0-9_-]{32,}/g, "[opaque]"), query_keys: [...url.searchParams.keys()] };
  } catch (_) { return { invalid_url: true }; }
}

function localEndpoint(value, protocols) {
  const url = new URL(value);
  if (!protocols.includes(url.protocol) || !["127.0.0.1", "localhost"].includes(url.hostname) || url.username || url.password || !url.port)
    throw Error("An explicit loopback endpoint is required");
  return url;
}

async function main(endpoint, targetId, output, duration = "30", inject = "") {
  const base = localEndpoint(endpoint, ["http:"]);
  const seconds = Number(duration);
  if (!Number.isFinite(seconds) || seconds < 5 || seconds > 60 || !targetId || !output || !["", "--bridge"].includes(inject))
    throw Error("Usage: node cdp_observe.cjs http://127.0.0.1:PORT TARGET_ID NEW_OUTPUT.jsonl [5..60 seconds] [--bridge]");
  const response = await fetch(new URL("/json/list", base), { redirect: "error", signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw Error("CDP discovery failed");
  const targets = await response.json();
  const target = targets.find(t => t.id === targetId && t.type === "page");
  if (!target) throw Error("Exact page target not found; confirm the app PID and target again");
  const address = localEndpoint(target.webSocketDebuggerUrl, ["ws:"]);
  if (address.port !== base.port) throw Error("CDP socket does not belong to the specified forward");
  const fd = fs.openSync(output, "wx");
  const record = row => fs.writeSync(fd, JSON.stringify({ at: new Date().toISOString(), ...row }) + "\n");
  let ws, timer, stopped = false, sequence = 0, observerOwned = false;
  const pending = new Map();
  function call(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timeout = setTimeout(() => { pending.delete(id); reject(Error("CDP command timed out: " + method)); }, 5000);
      pending.set(id, { resolve, reject, timeout });
      ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async function evaluate(expression) {
    const response = await call("Runtime.evaluate", { expression, returnByValue: true });
    if (response.exceptionDetails) throw Error("Observer evaluation failed");
    return response.result && response.result.value;
  }
  let finish;
  const stop = () => { stopped = true; if (finish) finish(); };
  process.once("SIGINT", stop);
  try {
    ws = new WebSocket(address);
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error("CDP connection timed out")), 5000);
      ws.addEventListener("open", () => { clearTimeout(timeout); resolve(); }, { once: true });
      ws.addEventListener("error", () => { clearTimeout(timeout); reject(Error("CDP connection failed")); }, { once: true });
    });
    ws.addEventListener("close", () => {
      for (const job of pending.values()) { clearTimeout(job.timeout); job.reject(Error("CDP disconnected")); }
      pending.clear();
      stop();
    });
    ws.addEventListener("message", event => {
      let message;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message.id) {
        const job = pending.get(message.id);
        if (job) { clearTimeout(job.timeout); pending.delete(message.id); message.error ? job.reject(Error("CDP protocol error")) : job.resolve(message.result); }
        return;
      }
      const p = message.params;
      if (!p) return;
      if (message.method === "Network.requestWillBeSent") record({ kind: "request", request: p.requestId, method: p.request.method, type: p.type, ...safeRoute(p.request.url) });
      if (message.method === "Network.responseReceived") record({ kind: "response", request: p.requestId, status: p.response.status, mime: p.response.mimeType, ...safeRoute(p.response.url) });
      if (message.method === "Network.loadingFailed") record({ kind: "request_failed", request: p.requestId, cancelled: !!p.canceled });
    });
    await call("Network.enable");
    record({ kind: "session_start", target: targetId, page: safeRoute(target.url), seconds, body_collection: false, headers_collection: false });
    if (inject === "--bridge") {
      const result = await evaluate(fs.readFileSync(path.join(__dirname, "webview_probe.js"), "utf8"));
      observerOwned = !!(result && result.installed && !result.reused);
      record({ kind: "bridge_probe", result });
      if (!observerOwned) throw Error("A previous observer is already active; stop it before a new capture");
    }
    const deadline = Date.now() + seconds * 1000;
    while (!stopped && Date.now() < deadline) {
      await new Promise(resolve => { finish = resolve; timer = setTimeout(resolve, Math.min(1000, deadline - Date.now())); });
      clearTimeout(timer);
      finish = undefined;
      if (observerOwned && !stopped) record({ kind: "bridge_events", result: await evaluate("window.__ucampusProbe ? window.__ucampusProbe.drain() : null") });
    }
  } finally {
    clearTimeout(timer);
    process.removeListener("SIGINT", stop);
    if (ws && ws.readyState === WebSocket.OPEN) {
      if (observerOwned) {
        try {
          record({ kind: "bridge_events", result: await evaluate("window.__ucampusProbe ? window.__ucampusProbe.drain() : null") });
          record({ kind: "probe_stop", result: await evaluate("window.__ucampusProbe ? window.__ucampusProbe.stop() : null") });
        } catch (_) { record({ kind: "probe_cleanup_unconfirmed", action: "Close or reload this WebView before another capture" }); }
      }
      try { await call("Network.disable"); } catch (_) {}
      ws.close();
    } else if (observerOwned) record({ kind: "probe_cleanup_unconfirmed", action: "Close or reload this WebView before another capture" });
    else if (ws) ws.close();
    record({ kind: "session_end" });
    fs.closeSync(fd);
  }
}

module.exports = { safeRoute, localEndpoint, main };
if (require.main === module) main(...process.argv.slice(2)).catch(error => { console.error(error.message); process.exitCode = 1; });
