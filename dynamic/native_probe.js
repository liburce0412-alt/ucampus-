/* Optional Frida observation only, attach to the confirmed cn.unipus.cloud PID. */
"use strict";
const nativeHooks = [], javaHooks = [], installed = new Set();
let stopping = false;
const counters = {};
function emit(kind, data) { try { send({ ms: Date.now(), kind, ...data }); } catch (_) {} }
function route(value) {
  const match = /^(https?|wss?):\/\/(?:[^/@]*@)?([^/?#]+)([^?#]*)/.exec(String(value));
  return match ? { origin: match[1] + "://" + match[2], path: (match[3] || "/").replace(/[A-Za-z0-9_-]{32,}/g, "[opaque]") } : { unavailable: true };
}
const nativeTargets = {
  "libaiengine.so": ["aiengine_start", "aiengine_stop", "aiengine_cancel"],
  "libskegn.so": ["skegn_start", "skegn_stop", "skegn_cancel"],
  "libsoe.so": ["soe_init_session", "soe_set_data", "soe_get_result", "soe_release_session"],
};
const moduleObserver = Process.attachModuleObserver({
  onAdded(module) {
    if (stopping || !nativeTargets[module.name]) return;
    for (const name of nativeTargets[module.name]) {
      const address = module.findExportByName(name);
      const key = module.name + ":" + name + ":" + String(address);
      if (!address || installed.has(key)) continue;
      installed.add(key);
      nativeHooks.push(Interceptor.attach(address, {
        onEnter() {
          counters[name] = (counters[name] || 0) + 1;
          if (name !== "soe_set_data") emit("native_call", { library: module.name, name });
        },
      }));
      emit("native_hook_ready", { library: module.name, name });
    }
  },
});
const interval = setInterval(() => {
  if (Object.keys(counters).length) {
    emit("native_call_counts", { counts: { ...counters } });
    for (const key of Object.keys(counters)) delete counters[key];
  }
}, 1000);

function scanJava() {
  if (stopping) return;
  if (typeof Java === "undefined" || !Java.available) { emit("capability", { java: false }); return; }
  Java.perform(() => {
    const names = ["UCAudioPlugin", "UCClioPlugin", "UCSOEPlugin", "UCHttpTaskPlugin", "UCFooPlugin", "ucpcm.UCPcmPlugin"];
    for (const shortName of names) {
      const name = "cn.unipus.wbv.plugin." + shortName;
      try {
        const klass = Java.use(name);
        if (!klass.execute) continue;
        for (const overload of klass.execute.overloads) {
          if (overload.argumentTypes[0].className !== "java.lang.String") continue;
          const key = name + ".execute(" + overload.argumentTypes.map(t => t.className).join(",") + ")";
          if (installed.has(key)) continue;
          installed.add(key);
          const previous = overload.implementation;
          overload.implementation = function (...args) {
            const action = String(args[0]);
            if (/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(action)) emit("cordova_execute", { class: name, action, argc: args.length });
            return overload.call(this, ...args);
          };
          javaHooks.push(() => { overload.implementation = previous; });
          emit("java_hook_ready", { signature: key });
        }
      } catch (_) { emit("class_unavailable", { class: name }); }
    }
    try {
      const key = "okhttp3.OkHttpClient.newCall";
      if (!installed.has(key)) {
        const method = Java.use("okhttp3.OkHttpClient").newCall.overload("okhttp3.Request");
        const previous = method.implementation;
        method.implementation = function (request) {
          try { emit("http_request_metadata", { method: String(request.method()), ...route(request.url().toString()) }); } catch (_) {}
          return method.call(this, request);
        };
        installed.add(key);
        javaHooks.push(() => { method.implementation = previous; });
        emit("java_hook_ready", { signature: key });
      }
    } catch (_) { emit("class_unavailable", { class: "okhttp3.OkHttpClient" }); }
  });
}
rpc.exports = {
  rescan() { scanJava(); return { requested: true }; },
  status() { return { installed: [...installed], observation_only: true }; },
  stop() {
    stopping = true;
    clearInterval(interval);
    moduleObserver.detach();
    for (const hook of nativeHooks) hook.detach();
    if (typeof Java !== "undefined" && Java.available) Java.perform(() => { for (const restore of javaHooks.reverse()) restore(); });
    return { stop_requested: true };
  },
};
scanJava();
