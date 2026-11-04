package local.ucampus.bridge;

import android.accessibilityservice.AccessibilityService;
import android.app.KeyguardManager;
import android.content.Intent;
import android.graphics.Rect;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.SystemClock;
import android.os.Build;
import android.util.SparseArray;
import android.system.Os;
import android.system.OsConstants;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;

public final class BridgeService extends AccessibilityService {
    private static final String TARGET = "cn.unipus.cloud";
    private static final int MAX_NODES = 1200;
    static volatile BridgeService current;
    private final AtomicLong revision = new AtomicLong();
    private final Object changes = new Object();
    private final String boot = UUID.randomUUID().toString();
    private volatile boolean running;
    private volatile LocalServerSocket listener;
    private volatile LocalSocket client;
    private volatile Thread worker;
    private long sequence;

    @Override public void onServiceConnected() {
        current = this;
        if (running) return;
        running = true;
        worker = new Thread(this::serve, "ucampus-control");
        worker.start();
    }

    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        revision.incrementAndGet();
        synchronized (changes) { changes.notifyAll(); }
    }

    @Override public void onInterrupt() { revision.incrementAndGet(); }

    private void stopServer() {
        running = false;
        worker = null;
        current = null;
        synchronized (changes) { changes.notifyAll(); }
        // UiAutomation can unbind us; shutdown wakes blocked reads and accept before rebinding.
        try { if (client != null) client.shutdownInput(); } catch (IOException ignored) { }
        try { if (client != null) client.close(); } catch (IOException ignored) { }
        try { if (listener != null) Os.shutdown(listener.getFileDescriptor(), OsConstants.SHUT_RDWR); } catch (Exception ignored) { }
        try { if (listener != null) listener.close(); } catch (IOException ignored) { }
    }

    @Override public boolean onUnbind(Intent intent) {
        stopServer();
        return super.onUnbind(intent);
    }

    @Override public void onDestroy() {
        stopServer();
        super.onDestroy();
    }

    private static final class Snapshot {
        String id;
        String signature;
        long captured;
        long revision;
        int window;
        int display;
        int visited;
        boolean truncated;
        JSONObject nodes = new JSONObject();
    }

    private static String str(CharSequence value) { return value == null ? "" : value.toString(); }

    private void walk(AccessibilityNodeInfo node, String ref, Snapshot state, int depth) throws Exception {
        if (depth > 64 || ++state.visited > MAX_NODES) { state.truncated = true; return; }
        if (Thread.currentThread() != worker) throw new IOException("service_disconnected");
        if (!TARGET.equals(str(node.getPackageName()))) return;
        if (node.isVisibleToUser()) {
            JSONObject row = new JSONObject();
            row.put("id", str(node.getViewIdResourceName()).replace(TARGET + ":id/", ""));
            row.put("class", str(node.getClassName()));
            row.put("text", node.isPassword() ? "[redacted]" : str(node.getText()));
            row.put("description", node.isPassword() ? "[redacted]" : str(node.getContentDescription()));
            row.put("enabled", node.isEnabled());
            row.put("clickable", node.isClickable());
            row.put("scrollable", node.isScrollable());
            row.put("checked", node.isChecked());
            row.put("selected", node.isSelected());
            row.put("password", node.isPassword());
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            row.put("bounds", new JSONArray(new int[]{bounds.left, bounds.top, bounds.right, bounds.bottom}));
            state.nodes.put(ref, row);
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            if (state.truncated) break;
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) {
                try { walk(child, ref + "." + i, state, depth + 1); }
                finally { child.recycle(); }
            }
        }
    }

    private static int requestedDisplay(JSONObject request) throws Exception {
        Object value = request.opt("display_id");
        if (value == null) return 0;
        if (!(value instanceof Integer) && !(value instanceof Long)) throw new IOException("invalid_display");
        long display = ((Number)value).longValue();
        if (display < 0 || display > Integer.MAX_VALUE) throw new IOException("invalid_display");
        return (int)display;
    }

    private JSONArray targetWindows() throws Exception {
        if (((KeyguardManager)getSystemService(KEYGUARD_SERVICE)).isKeyguardLocked()) throw new IOException("device_locked");
        if (Build.VERSION.SDK_INT < 30) throw new IOException("display_selection_unavailable");
        SparseArray<List<AccessibilityWindowInfo>> all = getWindowsOnAllDisplays();
        JSONArray result = new JSONArray();
        for (int i = 0; i < all.size(); i++) for (AccessibilityWindowInfo window : all.valueAt(i)) {
            try {
                AccessibilityNodeInfo root = window.getRoot();
                if (root != null) try {
                    if (TARGET.equals(str(root.getPackageName()))) result.put(new JSONObject()
                        .put("display_id", window.getDisplayId()).put("window", window.getId())
                        .put("focused", window.isFocused()).put("active", window.isActive()));
                } finally { root.recycle(); }
            } finally { window.recycle(); }
        }
        return result;
    }

    private AccessibilityNodeInfo targetRoot(int display) throws Exception {
        if (((KeyguardManager)getSystemService(KEYGUARD_SERVICE)).isKeyguardLocked()) throw new IOException("device_locked");
        AccessibilityNodeInfo root = null;
        if (display == 0) {
            root = getRootInActiveWindow();
            if (root != null && Build.VERSION.SDK_INT >= 30) {
                AccessibilityWindowInfo window = root.getWindow();
                boolean matches = window != null && window.getDisplayId() == 0;
                if (window != null) window.recycle();
                if (!matches) { root.recycle(); throw new IOException("active_window_on_other_display"); }
            }
        } else {
            if (Build.VERSION.SDK_INT < 30) throw new IOException("display_selection_unavailable");
            SparseArray<List<AccessibilityWindowInfo>> all = getWindowsOnAllDisplays();
            try {
                List<AccessibilityWindowInfo> windows = all.get(display);
                AccessibilityWindowInfo focused = null;
                if (windows != null) for (AccessibilityWindowInfo window : windows)
                    if (window.isFocused() && (focused == null || window.getLayer() > focused.getLayer())) focused = window;
                if (focused != null) root = focused.getRoot();
            } finally {
                for (int i = 0; i < all.size(); i++) for (AccessibilityWindowInfo window : all.valueAt(i)) window.recycle();
            }
        }
        if (root == null) throw new IOException("no_active_window");
        if (!TARGET.equals(str(root.getPackageName()))) {
            root.recycle();
            throw new IOException("target_not_foreground");
        }
        return root;
    }

    private Snapshot capture(int display) throws Exception {
        long before = revision.get();
        AccessibilityNodeInfo root = targetRoot(display);
        Snapshot snap = new Snapshot();
        try {
            snap.window = root.getWindowId();
            snap.display = display;
            walk(root, "0", snap, 0);
        } finally { root.recycle(); }
        snap.revision = revision.get();
        if (before != snap.revision) throw new IOException("ui_changed_during_capture");
        snap.captured = SystemClock.elapsedRealtime();
        snap.id = boot + ":" + (++sequence);
        snap.signature = snap.display + ":" + snap.window + ":" + snap.nodes.toString();
        if (snap.signature.length() > 2 * 1024 * 1024) throw new IOException("snapshot_too_large");
        return snap;
    }

    private JSONObject snapshotJson(Snapshot state, Snapshot base) throws Exception {
        JSONObject result = new JSONObject().put("snapshot_id", state.id).put("captured_elapsed_ms", state.captured)
            .put("revision", state.revision).put("window", state.window).put("display_id", state.display).put("package", TARGET).put("truncated", state.truncated);
        if (base == null || base.window != state.window || base.display != state.display) return result.put("nodes", state.nodes);
        JSONObject updated = new JSONObject();
        JSONArray removed = new JSONArray();
        for (Iterator<String> keys = state.nodes.keys(); keys.hasNext();) {
            String key = keys.next();
            if (!state.nodes.get(key).toString().equals(base.nodes.optString(key))) updated.put(key, state.nodes.get(key));
        }
        for (Iterator<String> keys = base.nodes.keys(); keys.hasNext();) {
            String key = keys.next();
            if (!state.nodes.has(key)) removed.put(key);
        }
        return result.put("base", base.id).put("updated", updated).put("removed", removed)
            .put("unchanged", updated.length() == 0 && removed.length() == 0);
    }

    private boolean click(Snapshot expected, JSONObject request) throws Exception {
        if (expected == null || !expected.id.equals(request.getString("snapshot_id"))) throw new IOException("unknown_snapshot");
        if (requestedDisplay(request) != expected.display) throw new IOException("display_changed");
        if (SystemClock.elapsedRealtime() - expected.captured > 30000) throw new IOException("stale_snapshot");
        Snapshot now = capture(expected.display);
        if (expected.truncated || now.truncated || !expected.signature.equals(now.signature)) throw new IOException("ui_changed");
        String ref = request.getString("ref");
        JSONObject row = expected.nodes.getJSONObject(ref);
        if (!row.getBoolean("enabled") || !row.getBoolean("clickable") || row.getBoolean("password")) throw new IOException("not_clickable");
        if (!ref.matches("0(?:\\.[0-9]{1,4}){0,64}")) throw new IOException("invalid_ref");
        AccessibilityNodeInfo node = targetRoot(expected.display);
        try {
            if (node.getWindowId() != expected.window) throw new IOException("window_changed");
            String[] parts = ref.split("\\.");
            for (int i = 1; i < parts.length; i++) {
                AccessibilityNodeInfo next = node.getChild(Integer.parseInt(parts[i]));
                if (next == null) throw new IOException("node_disappeared");
                node.recycle();
                node = next;
            }
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            if (!node.isVisibleToUser() || !node.isEnabled() || !node.isClickable() || node.isPassword()
                || !TARGET.equals(str(node.getPackageName())) || revision.get() != now.revision
                || !str(node.getViewIdResourceName()).replace(TARGET + ":id/", "").equals(row.getString("id"))
                || !str(node.getText()).equals(row.getString("text"))
                || !new JSONArray(new int[]{bounds.left, bounds.top, bounds.right, bounds.bottom}).toString().equals(row.getJSONArray("bounds").toString()))
                throw new IOException("node_changed");
            return node.performAction(AccessibilityNodeInfo.ACTION_CLICK);
        } finally { node.recycle(); }
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream data = new ByteArrayOutputStream();
        for (int b; (b = in.read()) != -1;) {
            if (b == '\n') return data.toString("UTF-8");
            if (data.size() >= 65536) throw new IOException("request_too_large");
            data.write(b);
        }
        return null;
    }

    private void serve() {
        LocalServerSocket server = null;
        try {
            server = new LocalServerSocket("ucampus_control_v1");
            listener = server;
            while (Thread.currentThread() == worker) {
                LocalSocket socket = server.accept();
                client = socket;
                try {
                    int uid = socket.getPeerCredentials().getUid();
                    if (uid != 2000 && uid != 0) continue;
                    socket.setSoTimeout(60000);
                    Snapshot last = null;
                    String line;
                    while (Thread.currentThread() == worker && (line = readLine(socket.getInputStream())) != null) {
                        JSONObject response = new JSONObject();
                        try {
                            JSONObject request = new JSONObject(line);
                            response.put("id", request.get("id"));
                            String method = request.getString("method");
                            if (method.equals("ping")) {
                                response.put("result", new JSONObject().put("protocol", 1).put("target", TARGET).put("boot", boot)
                                    .put("display_selection", Build.VERSION.SDK_INT >= 30));
                            } else if (method.equals("windows")) {
                                response.put("result", new JSONObject().put("windows", targetWindows()));
                            } else if (method.equals("snapshot") || method.equals("wait")) {
                                Snapshot base = last != null && last.id.equals(request.optString("since")) ? last : null;
                                if (method.equals("wait")) {
                                    long seen = request.getLong("revision");
                                    long timeout = request.optLong("timeout_ms", 1500);
                                    if (timeout < 0 || timeout > 5000) throw new IOException("invalid_timeout");
                                    long deadline = SystemClock.elapsedRealtime() + timeout;
                                    synchronized (changes) {
                                        while (running && revision.get() <= seen && SystemClock.elapsedRealtime() < deadline)
                                            changes.wait(Math.max(1, deadline - SystemClock.elapsedRealtime()));
                                    }
                                }
                                last = capture(requestedDisplay(request));
                                response.put("result", snapshotJson(last, base));
                            } else if (method.equals("click")) {
                                Snapshot expected = last;
                                last = null;
                                response.put("result", new JSONObject().put("dispatched", click(expected, request)).put("verified", false));
                            } else throw new IOException("unknown_method");
                        } catch (Exception error) {
                            response.put("error", error instanceof IOException ? error.getMessage() : "invalid_request_or_service_error");
                        }
                        socket.getOutputStream().write((response.toString() + "\n").getBytes(StandardCharsets.UTF_8));
                        socket.getOutputStream().flush();
                    }
                } catch (Exception error) {
                    android.util.Log.w("UCampusBridge", "Connection closed: " + error.getClass().getSimpleName());
                    // A lost response never replays a click; the client must inspect the current page.
                } finally { socket.close(); if (client == socket) client = null; }
            }
        } catch (Exception error) {
            if (Thread.currentThread() == worker) android.util.Log.e("UCampusBridge", "Control socket stopped", error);
        } finally {
            try { if (server != null) server.close(); } catch (IOException ignored) { }
            if (Thread.currentThread() == worker) running = false;
        }
    }
}
