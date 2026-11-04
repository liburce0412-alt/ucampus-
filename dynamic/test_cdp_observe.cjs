const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { safeRoute, localEndpoint, main } = require("./cdp_observe.cjs");

test("only explicit loopback forwards and redacted route metadata", () => {
  assert.throws(() => localEndpoint("http://example.org:8795", ["http:"]));
  assert.throws(() => localEndpoint("http://user:secret@127.0.0.1:8795", ["http:"]));
  const route = safeRoute("https://user:secret@example.org/media?token=private#private");
  assert.equal(route.path, "/media");
  assert.deepEqual(route.query_keys, ["token"]);
  assert(!JSON.stringify(route).includes("private"));
  assert(!JSON.stringify(route).includes("secret"));
});

test("mock CDP capture preserves metadata and restores its observer", async () => {
  const directory = fs.mkdtempSync(path.join(__dirname, ".test-cdp-"));
  const output = path.join(directory, "trace.jsonl");
  const savedFetch = global.fetch, savedWebSocket = global.WebSocket;
  const methods = [], expressions = [];
  class FakeSocket extends EventTarget {
    static OPEN = 1;
    constructor(url) { super(); this.readyState = 1; queueMicrotask(() => this.dispatchEvent(new Event("open"))); }
    send(raw) {
      const request = JSON.parse(raw);
      methods.push(request.method);
      let result = {};
      if (request.method === "Runtime.evaluate") {
        const expression = request.params.expression;
        expressions.push(expression);
        const value = expression.includes(".drain()") ? { events: [], dropped: 0 } : expression.includes(".stop()") ? { stopped: true } : { installed: true, hooked: [] };
        result = { result: { value } };
      }
      queueMicrotask(() => {
        this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ id: request.id, result }) }));
        if (request.method === "Network.enable") this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ method: "Network.requestWillBeSent", params: { requestId: "r1", type: "Media", request: { method: "GET", url: "https://example.org/a.mp3?token=private", headers: { Authorization: "secret" }, postData: "private-body" } } }) }));
      });
    }
    close() { this.readyState = 3; this.dispatchEvent(new Event("close")); }
  }
  global.WebSocket = FakeSocket;
  global.fetch = async () => ({ ok: true, json: async () => [{ id: "p1", type: "page", url: "https://example.org/lesson?auth=private", webSocketDebuggerUrl: "ws://127.0.0.1:8795/devtools/page/p1" }] });
  try {
    await main("http://127.0.0.1:8795", "p1", output, "5", "--bridge");
    const text = fs.readFileSync(output, "utf8");
    assert(text.includes('"kind":"request"'));
    assert(!text.includes("private"));
    assert(!text.includes("secret"));
    assert(methods.includes("Network.disable"));
    assert(expressions.some(value => value.includes(".stop()")));
  } finally {
    global.fetch = savedFetch;
    global.WebSocket = savedWebSocket;
    const actual = fs.realpathSync(directory);
    assert(actual.startsWith(fs.realpathSync(__dirname) + path.sep + ".test-cdp-"));
    fs.rmSync(actual, { recursive: true });
  }
});
