"""Persistent local control client. Requires an explicitly installed/enabled test bridge."""
import argparse
import json
import math
import socket
import statistics
import sys
import time
from pathlib import Path
from ucampus_readonly import encode, PROTOCOLS, rpc_error

MAX_RESPONSE = 4 * 1024 * 1024
TARGET = "cn.unipus.cloud"


class BridgeError(RuntimeError):
    pass


class OutcomeUnknown(BridgeError):
    pass


def compact(nodes):
    result = {}
    for ref, row in nodes.items():
        if not (row.get("text") or row.get("description") or row.get("clickable") or row.get("scrollable")):
            continue
        item = {k: row[k] for k in ("id", "text", "description", "bounds") if row.get(k)}
        if row.get("password"):
            for field in ("text", "description"):
                if field in item:
                    item[field] = "[redacted]"
        item["flags"] = [k for k in ("clickable", "scrollable", "checked", "selected", "password") if row.get(k)]
        if not row.get("enabled"):
            item["flags"].append("disabled")
        parent = ref
        while parent and not nodes.get(parent, {}).get("clickable"):
            parent = parent.rpartition(".")[0]
        if parent:
            item["click_ref"] = parent
        result[ref] = item
    return result


class Client:
    def __init__(self, port, display_id=0):
        if not 1 <= port <= 65535:
            raise ValueError("Invalid loopback port")
        if type(display_id) is not int or not 0 <= display_id <= 2147483647:
            raise ValueError("Invalid explicit display id")
        self.display_id = display_id
        self.port, self.socket, self.reader = port, None, None
        self.serial = 0
        self.nodes, self.last, self.last_view = {}, None, {}

    def close(self):
        if self.reader:
            self.reader.close()
        if self.socket:
            self.socket.close()
        self.socket = self.reader = None
        self.nodes, self.last, self.last_view = {}, None, {}

    def _exchange(self, method, **params):
        self.serial += 1
        request = {"id": self.serial, "method": method, **params}
        payload = (encode(request) + "\n").encode()
        if len(payload) > 65536:
            raise BridgeError("Request too large")
        self.socket.sendall(payload)
        raw = self.reader.readline(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
            raise ValueError("Lost or oversized response")
        response = json.loads(raw)
        if not isinstance(response, dict) or response.get("id") != self.serial:
            raise ValueError("Mismatched response")
        if "error" in response:
            raise BridgeError(response["error"])
        if not isinstance(response.get("result"), dict):
            raise ValueError("Invalid response result")
        return response["result"]

    def rpc(self, method, **params):
        try:
            if self.socket is None:
                self.socket = socket.create_connection(("127.0.0.1", self.port), timeout=8)
                self.reader = self.socket.makefile("rb")
                hello = self._exchange("ping")
                if hello.get("protocol") != 1 or hello.get("target") != TARGET:
                    raise ValueError("Wrong bridge protocol or target")
                if self.display_id and not hello.get("display_selection"):
                    raise ValueError("This bridge cannot select a secondary display")
            if self.display_id:
                params = {**params, "display_id": self.display_id}
            return self._exchange(method, **params)
        except BridgeError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            self.close()
            if method == "click":
                raise OutcomeUnknown("Click outcome unknown; inspect a fresh screen before deciding. Never automatically retry.") from exc
            raise BridgeError("Bridge connection failed; service and ADB forward must already be ready.") from exc

    def observe(self, wait_ms=0, full=False):
        if type(wait_ms) is not int or not 0 <= wait_ms <= 5000 or type(full) is not bool:
            raise ValueError("wait_ms must be 0..5000 and full must be boolean")
        before = self.last
        args = {} if full or not before else {"since": before["snapshot_id"]}
        method = "snapshot"
        if wait_ms and before:
            method = "wait"
            args.update(revision=before["revision"], timeout_ms=wait_ms)
        for attempt in range(3):
            try:
                data = self.rpc(method, **args)
                break
            except BridgeError as error:
                if attempt == 0 and str(error).startswith("Bridge connection failed;"):
                    # A timed-out idle session is safe to reopen for a read; clicks never retry.
                    continue
                if str(error) != "ui_changed_during_capture" or attempt == 2:
                    raise
                time.sleep(.04)
        if data.get("package") != TARGET or not isinstance(data.get("snapshot_id"), str):
            raise BridgeError("Invalid snapshot identity")
        if data.get("display_id", 0) != self.display_id:
            raise BridgeError("Snapshot belongs to a different display")
        if "nodes" in data:
            self.nodes = data["nodes"]
        elif before and data.get("base") == before["snapshot_id"]:
            for ref in data["removed"]:
                self.nodes.pop(ref, None)
            self.nodes.update(data["updated"])
        else:
            self.last = None
            raise BridgeError("Delta base missing; request a full snapshot")
        view = compact(self.nodes)
        result = {k: data[k] for k in ("snapshot_id", "revision", "window", "captured_elapsed_ms", "truncated")}
        if "display_id" in data:
            result["display_id"] = data["display_id"]
        if "nodes" in data or full:
            result["nodes"] = view
        else:
            result.update(base=before["snapshot_id"], updated={k: v for k, v in view.items() if self.last_view.get(k) != v},
                          removed=[k for k in self.last_view if k not in view])
            result["unchanged"] = not result["updated"] and not result["removed"]
        self.last, self.last_view = data, view
        return result

    def select(self, selector, missing_ok=False):
        if not isinstance(selector, dict) or not selector or set(selector) - {"id", "text", "description"} or any(not isinstance(v, str) or not v for v in selector.values()):
            raise ValueError("Selector needs exact id, text and/or description")
        expected = {k: v.removeprefix(TARGET + ":id/") if k == "id" else v for k, v in selector.items()}
        matches = [ref for ref, row in self.nodes.items() if all(row.get(k) == v for k, v in expected.items())]
        if not matches and missing_ok:
            return None
        if len(matches) != 1:
            raise BridgeError("Selector is missing or ambiguous")
        return matches[0]

    def click(self, snapshot_id, ref):
        if not self.last or snapshot_id != self.last["snapshot_id"] or self.last.get("truncated"):
            raise BridgeError("Fresh complete snapshot required")
        if ref not in self.nodes:
            raise BridgeError("Unknown node reference")
        try:
            return self.rpc("click", snapshot_id=snapshot_id, ref=ref)
        finally:
            self.last = None

    def batch(self, snapshot_id, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise ValueError("Use 1..8 explicit guarded steps")
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"click", "expect"}:
                raise ValueError("Each step requires click and expect selectors")
            for selector in step.values():
                if not isinstance(selector, dict) or not selector or set(selector) - {"id", "text", "description"} or any(not isinstance(v, str) or not v for v in selector.values()):
                    raise ValueError("Invalid exact selector")
        if not self.last or snapshot_id != self.last["snapshot_id"] or self.last.get("truncated"):
            raise BridgeError("Batch needs the live snapshot used to decide the route")
        initial_nodes, initial_window = dict(self.nodes), self.last["window"]
        completed = 0
        for step in steps:
            self.observe(full=True)
            if completed == 0 and (self.nodes != initial_nodes or self.last["window"] != initial_window):
                raise BridgeError("Batch initial page changed; inspect before deciding the route again")
            ref = self.select(step["click"])
            previous_target = self.select(step["expect"], missing_ok=True)
            if previous_target is not None and self.nodes[previous_target].get("enabled"):
                raise BridgeError(f"Batch stopped after {completed} steps: postcondition already present")
            row = self.nodes[ref]
            if not row.get("clickable") or not row.get("enabled"):
                raise BridgeError(f"Batch stopped after {completed} steps: target not clickable")
            result = self.click(self.last["snapshot_id"], ref)
            if not result.get("dispatched"):
                raise BridgeError(f"Batch stopped after {completed} steps: click not dispatched")
            deadline = time.monotonic() + 3
            while True:
                self.observe(wait_ms=150)
                try:
                    target = self.select(step["expect"])
                    if self.nodes[target].get("enabled"):
                        break
                except BridgeError:
                    pass
                if time.monotonic() >= deadline:
                    return {"completed": completed, "stopped": "postcondition_not_observed", "action_may_have_completed": True,
                            "next": "Inspect current state; do not replay the batch", "snapshot": self.observe(full=True)}
            completed += 1
        return {"completed": completed, "snapshot": self.observe(full=True)}


TOOLS = [
    {"name": "ucampus_observe", "description": "Read live U-campus controls via an already authorized local bridge; reuse the connection and return changes. wait_ms waits up to 5000 ms for a UI event, not for task completion.",
     "inputSchema": {"type": "object", "properties": {"wait_ms": {"type": "integer", "minimum": 0, "maximum": 5000}, "full": {"type": "boolean"}}, "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "ucampus_click", "description": "Click one enabled node from a fresh live snapshot. Rejects stale/changed/foreign screens. dispatched does not prove completion. Never retries a click.",
     "inputSchema": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "ref": {"type": "string"}}, "required": ["snapshot_id", "ref"], "additionalProperties": False}, "annotations": {"readOnlyHint": False}},
    {"name": "ucampus_batch", "description": "Execute 1..8 explicitly requested clicks with exact id/text/description selectors and a required observed postcondition per step. Stops on mismatch; never replays. Use only for an already verified authorized route.",
     "inputSchema": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "steps": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object", "properties": {"click": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "description": {"type": "string"}}, "additionalProperties": False}, "expect": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "description": {"type": "string"}}, "additionalProperties": False}}, "required": ["click", "expect"], "additionalProperties": False}}}, "required": ["snapshot_id", "steps"], "additionalProperties": False}, "annotations": {"readOnlyHint": False}},
]


def dispatch(request, client):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return rpc_error(None, -32600, "Invalid request")
    if "id" not in request:
        return None
    ident, method = request["id"], request["method"]
    if type(ident) not in (str, int):
        return rpc_error(None, -32600, "Invalid request id")
    params = request.get("params", {})
    if not isinstance(params, dict):
        return rpc_error(ident, -32602, "Parameters must be an object")
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if isinstance(version, str) and version in PROTOCOLS else "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "ucampus-control-test", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        functions = {"ucampus_observe": client.observe, "ucampus_click": client.click, "ucampus_batch": client.batch}
        if name not in functions or not isinstance(args, dict):
            return rpc_error(ident, -32602, "Unknown tool or invalid arguments")
        try:
            result = {"content": [{"type": "text", "text": encode(functions[name](**args))}], "isError": False}
        except (BridgeError, ValueError, TypeError, KeyError) as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        return rpc_error(ident, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--display-id", type=int, default=0)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("observe")
    commands.add_parser("displays")
    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--count", type=int, default=20)
    benchmark.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = Client(args.port, args.display_id)
    try:
        if args.command == "serve":
            while raw := sys.stdin.buffer.readline(MAX_RESPONSE + 1):
                if len(raw) > MAX_RESPONSE:
                    print(encode(rpc_error(None, -32700, "Request too large")), flush=True)
                    break
                try:
                    response = dispatch(json.loads(raw), client)
                except (ValueError, UnicodeError):
                    response = rpc_error(None, -32700, "Invalid JSON")
                if response is not None:
                    print(encode(response), flush=True)
        elif args.command == "observe":
            print(encode(client.observe(full=True)))
        elif args.command == "displays":
            print(encode(client.rpc("windows")))
        else:
            if not 3 <= args.count <= 100:
                parser.error("count must be 3..100")
            samples = []
            for i in range(args.count + 1):
                start = time.perf_counter()
                observation = client.observe()
                samples.append({"ms": round((time.perf_counter() - start) * 1000, 3), "utf8_bytes": len(encode(observation).encode()), "node_count": len(client.nodes), "truncated": observation["truncated"]})
            times = sorted(row["ms"] for row in samples[1:])
            report = {"kind": "live_accessibility_read_only", "cold": samples[0], "steady": samples[1:], "p50_ms": statistics.median(times), "p95_ms": times[math.ceil(len(times) * .95) - 1], "ui_actions": 0, "token_savings_measured": False}
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(encode(report))
    finally:
        client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
