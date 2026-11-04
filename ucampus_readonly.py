"""Read cached U-campus UI and verified research notes via CLI or stdio MCP."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent
MAX_BYTES = 4 * 1024 * 1024
PACKAGE = "cn.unipus.cloud"
PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_ui(root, name):
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root) or path.suffix.lower() != ".xml":
        raise ValueError("Only XML inside the configured UI directory is allowed")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("XML exceeds the 4 MiB limit")
    text = raw.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Oversized XML or DTD/entity declarations are not supported")
    tree = ET.fromstring(text)
    if tree.tag != "hierarchy":
        raise ValueError("Expected an Android UI hierarchy")
    nodes = list(tree.iter("node"))
    if not any(n.get("package") == PACKAGE for n in nodes):
        raise ValueError("This snapshot is not U-campus")
    elements = {}
    for index, node in enumerate(nodes):
        a = node.attrib
        if a.get("package") != PACKAGE:
            continue
        flags = [k for k in ("clickable", "long-clickable", "scrollable", "checkable", "checked", "selected", "focused") if a.get(k) == "true"]
        if a.get("enabled") == "false":
            flags.append("disabled")
        secret = a.get("password") == "true"
        if not any(a.get(k) for k in ("text", "content-desc", "hint")) and not flags and not secret:
            continue
        row = {}
        for source, target in (("text", "text"), ("content-desc", "description"), ("hint", "hint")):
            if a.get(source):
                row[target] = "[redacted]" if secret else a[source]
        if secret:
            flags.append("password")
        bounds = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", a.get("bounds", ""))
        if bounds:
            row["bounds"] = [int(v) for v in bounds.groups()]
        if flags:
            row["flags"] = flags
        if a.get("resource-id"):
            row["id"] = a["resource-id"].removeprefix(PACKAGE + ":id/")
        elements[str(index)] = row
    return {
        "source": path.name,
        "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "cached": True,
        "rotation": tree.get("rotation"),
        "viewport": nodes[0].get("bounds") if nodes else None,
        "elements": elements,
    }


def brief(root, xml="latest-ui.xml", previous=None):
    current = read_ui(root, xml)
    if previous is None:
        return current
    before = read_ui(root, previous)
    if (before["rotation"], before["viewport"]) != (current["rotation"], current["viewport"]):
        current["reset"] = "viewport_changed"
        return current
    old, new = before["elements"], current.pop("elements")
    updated = {ref: row for ref, row in new.items() if old.get(ref) != row}
    removed = [ref for ref in old if ref not in new]
    return {**current, "base": before["source"], "base_modified_utc": before["modified_utc"],
            "unchanged": not updated and not removed, "updated": updated, "removed": removed}


def lookup(query=""):
    if len(query) > 200:
        raise ValueError("Query is limited to 200 characters")
    data = json.loads((BASE / "findings.json").read_text(encoding="utf-8-sig"))
    hits = [item for item in data["findings"] if query.casefold() in encode(item).casefold()]
    return {"apk": data["apk"], "findings": hits, "runtime_verified": False}


TOOLS = [
    {"name": "ucampus_cached_ui", "description": "Read saved U-campus XML as text/button state, or return changes versus an explicit older XML. Cached evidence only; never captures or controls a phone. References are valid only in these snapshots.",
     "inputSchema": {"type": "object", "properties": {"xml": {"type": "string", "default": "latest-ui.xml"}, "previous": {"type": "string"}}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "ucampus_findings", "description": "Search locally verified APK/Mika research, evidence locations and unverified limitations. Does not call course APIs or expose login data.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "default": ""}}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(request, root):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return rpc_error(None, -32600, "Invalid request")
    if "id" not in request:
        return None
    request_id, method = request["id"], request["method"]
    if type(request_id) not in (str, int):
        return rpc_error(None, -32600, "Invalid request id")
    params = request.get("params", {})
    if not isinstance(params, dict):
        return rpc_error(request_id, -32602, "Parameters must be an object")
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if isinstance(version, str) and version in PROTOCOLS else "2025-11-25",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "ucampus-readonly", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        if params.get("cursor"):
            return rpc_error(request_id, -32602, "No continuation cursor exists")
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if not tool or not isinstance(args, dict) or any(k not in tool["inputSchema"]["properties"] or not isinstance(v, str) for k, v in args.items()):
            return rpc_error(request_id, -32602, "Unknown tool or invalid arguments")
        try:
            value = brief(root, **args) if name == "ucampus_cached_ui" else lookup(**args)
            result = {"content": [{"type": "text", "text": encode(value)}], "isError": False}
        except (ValueError, OSError, ET.ParseError) as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        return rpc_error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(root):
    # shortcut: bounded synchronous local reads only; use the MCP SDK if adding remote or long-running tools.
    while raw := sys.stdin.buffer.readline(MAX_BYTES + 1):
        if len(raw) > MAX_BYTES:
            print(encode(rpc_error(None, -32700, "Message too large")), flush=True)
            return
        try:
            result = dispatch(json.loads(raw), root)
        except (ValueError, UnicodeError):
            result = rpc_error(None, -32700, "Invalid JSON")
        if result is not None:
            print(encode(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-dir", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="stdio MCP; no network listener")
    ui = commands.add_parser("ui", help="summarize existing XML only")
    ui.add_argument("xml", nargs="?", default="latest-ui.xml")
    ui.add_argument("--previous")
    find = commands.add_parser("find", help="search static findings")
    find.add_argument("query", nargs="?", default="")
    args = parser.parse_args()
    root = args.ui_dir.resolve(strict=True)
    if not root.is_dir():
        parser.error("--ui-dir must be a directory")
    if args.command == "serve":
        serve(root)
    else:
        try:
            print(encode(brief(root, args.xml, args.previous) if args.command == "ui" else lookup(args.query)))
        except (ValueError, OSError, ET.ParseError) as exc:
            parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
