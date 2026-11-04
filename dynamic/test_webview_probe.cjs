const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(__dirname + "/webview_probe.js", "utf8");
let called = 0;
function Audio() {}
Audio.prototype.prepare = function (url) { called++; return url; };
Audio.onStatus = function (id, type, value) { return value; };
const original = Audio.prototype.prepare;
const window = { cordova: { define: { moduleMap: {
  "cordova-plugin-ucaudio.UCAudioPlugin": { exports: Audio },
  "cordova-plugin-ucsoe.UCSOEPlugin": { factory() { throw Error("must not initialize unused plugin"); } },
} } } };
const context = vm.createContext({ window, URL, performance: { now: () => 100 }, location: { href: "https://example.test/lesson?token=private" } });
const result = vm.runInContext(source, context);
assert(result.hooked.includes("UCAudioPlugin.prepare"));
assert(result.missing.includes("UCSOEPlugin"));
const url = "https://user:secret@example.test/audio.mp3?token=private";
assert.equal(new Audio().prepare(url), url);
assert.equal(called, 1);
assert.equal(Audio.onStatus("private-id", 8, 100), 100);
const events = window.__ucampusProbe.drain();
assert.equal(events.events.length, 2);
assert(!JSON.stringify(events).includes("private"));
assert(!JSON.stringify(events).includes("secret"));
window.__ucampusProbe.stop();
assert.equal(Audio.prototype.prepare, original);
assert.equal(window.__ucampusProbe, undefined);
console.log("PASS: transparent calls, status observation, redaction, dormant-module skip, restoration");
