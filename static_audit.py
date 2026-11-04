"""Reproduce the offline audit of the exported APK; never connects to a device."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import shutil
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import zipfile

A = "{http://schemas.android.com/apk/res/android}"
RELEVANT = re.compile(r"utss|ulisten|listen|speak|record|dialogue|clio|chivox|soe|audio|pcm|wbv|听力|跟读|口语|听说|自由表达|录音", re.I)
URL = re.compile(rb"(?:https?|wss?)://[a-zA-Z0-9][^\s\x00-\x20\x7f-\xff<>\"'\\{}]{2,300}")
BRIDGE = re.compile(r"\bexec\s*\(\s*[^,]+,\s*[^,]+,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*(?:,\s*(\[[^;]*?\]|null|undefined|[\w.$]+))?\s*\)")
JS_TOKEN = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|//[^\n]*|/\*[\s\S]*?\*/|[{}]")


def function_end(text, start):
    depth = 0
    for t in JS_TOKEN.finditer(text, start):
        if t[0] == "{":
            depth += 1
        elif t[0] == "}":
            depth -= 1
            if depth == 0:
                return t.end()
    return start


def write(root, name, data):
    (root / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_url(value):
    try:
        p = urlsplit(value.rstrip("),;.]"))
        if not p.hostname:
            return None
        return urlunsplit((p.scheme, p.hostname, p.path, "[redacted]" if p.query else "", ""))
    except ValueError:
        return None


def decode_js_calls(text, source):
    methods = list(re.finditer(r"(\w+)\.prototype\.(\w+)\s*=\s*function\s*\(([^)]*)\)", text))
    result = []
    for m in BRIDGE.finditer(text):
        previous = [x for x in methods if x.start() < m.start() < function_end(text, x.end())]
        row = {"service": m[1], "action": m[2], "arguments_expression": m[3],
               "source": source, "line": text.count("\n", 0, m.start()) + 1,
               "runtime_verified": False}
        if previous:
            method = previous[-1]
            row.update(js_method=method[1] + "." + method[2], js_parameters=method[3])
        result.append(row)
    return result


def manifest_inventory(decoded):
    root = ET.parse(decoded / "AndroidManifest.xml").getroot()
    app = root.find("application")
    components = []
    for node in app:
        if node.tag not in {"activity", "activity-alias", "service", "receiver", "provider"}:
            continue
        filters = []
        for f in node.findall("intent-filter"):
            filters.append({"actions": [n.get(A + "name") for n in f.findall("action")],
                            "categories": [n.get(A + "name") for n in f.findall("category")],
                            "data": [{k.removeprefix(A): v for k, v in n.attrib.items()} for n in f.findall("data")]})
        components.append({"type": node.tag, **{k.removeprefix(A): v for k, v in node.attrib.items()},
                           "intent_filters": filters})
    return {"package": root.get("package"), "application": {k.removeprefix(A): v for k, v in app.attrib.items()},
            "components": components, "permissions": [n.get(A + "name") for n in root.findall("uses-permission")]}


def audit(apk, decoded, out, rabin):
    out.mkdir(parents=True, exist_ok=True)
    endpoints, calls, files, blobs, unparsed = [], [], [], [], []
    native_strings = []
    with zipfile.ZipFile(apk) as z:
        for f in z.infolist():
            name = f.filename
            files.append({"path": name, "size": f.file_size, "compressed": f.compress_size})
            scan = name.startswith("assets/") or name.startswith("lib/arm64-v8a/") or name.endswith(".dex")
            if not scan:
                continue
            b = z.read(name)
            for m in URL.finditer(b):
                url = safe_url(m[0].decode("ascii"))
                if url:
                    endpoints.append({"url": url, "source": name, "offset": m.start()})
            if name.startswith("assets/") and name.endswith((".js", ".html", ".xml", ".json", ".txt")):
                dest = (out / "assets" / name).resolve()
                if not dest.is_relative_to((out / "assets").resolve()):
                    raise ValueError("APK path outside extraction root")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b)
                if name.endswith(".js"):
                    text = b.decode("utf-8-sig")
                    calls.extend(decode_js_calls(text, name))
                    if "/plugins/" in name:
                        parsed_starts = {m.start() for m in BRIDGE.finditer(text)}
                        for m in re.finditer(r"\bexec\s*\(", text):
                            if m.start() not in parsed_starts:
                                unparsed.append({"source": name, "line": text.count("\n", 0, m.start()) + 1,
                                                 "excerpt": text[m.start():m.start()+180]})
            if b.startswith(b"SQLite format 3\x00"):
                db = sqlite3.connect(":memory:")
                try:
                    db.deserialize(b)
                    db.execute("PRAGMA query_only = ON")
                    tables = []
                    for table, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"):
                        q = '"' + table.replace('"', '""') + '"'
                        tables.append({"name": table, "schema": sql, "rows": db.execute("SELECT count(*) FROM " + q).fetchone()[0]})
                    write(out, "sqlite-schema.json", {"source": name, "tables": tables, "user_data": False})
                finally:
                    db.close()
            if name.startswith("assets/") and (name.endswith((".dat", ".ajm", ".Data", ".bin", ".res"))):
                sample = b[:65536]
                entropy = -sum((n / len(sample)) * math.log2(n / len(sample)) for n in Counter(sample).values()) if sample else 0
                markers = {magic.decode("ascii", errors="replace"): b.find(magic) for magic in (b"dex\n035\x00", b"dex\n039\x00", b"PK\x03\x04", b"SQLite format 3\x00")}
                blobs.append({"source": name, "size": len(b), "header16": b[:16].hex(), "first64k_entropy": round(entropy, 4), "marker_offsets": markers})
            if name.startswith("lib/arm64-v8a/") and (RELEVANT.search(name) or "aiengine" in name or "skegn" in name):
                for m in re.finditer(rb"[\x20-\x7e]{5,}", b):
                    s = m[0].decode("ascii")
                    if (s.startswith(("Java_", "JNI_", "aiengine_", "skegn_", "soe_")) or re.search(r"recordId|sampleRate|sampleBytes|audioType|coreType|refText|userId|duration|requestId|audio_format|sample_rate|recognition|recording", s)) and len(s) < 450:
                        native_strings.append({"source": name, "offset": m.start(), "text": s})

    write(out, "apk-files.json", files)
    write(out, "manifest.json", manifest_inventory(decoded))
    for path in sorted((decoded / "res").glob("values*/*.xml")):
        data = path.read_bytes()
        for m in URL.finditer(data):
            url = safe_url(m[0].decode("ascii"))
            if url:
                endpoints.append({"url": url, "source": path.relative_to(decoded).as_posix(), "line": data[:m.start()].count(b"\n") + 1})
    write(out, "endpoints.json", endpoints)
    write(out, "packed-blobs.json", blobs)
    write(out, "native-strings.json", native_strings)
    features = {}
    config = ET.parse(decoded / "res/xml/config.xml").getroot()
    for node in config.iter():
        if node.tag.rsplit("}", 1)[-1] == "feature":
            features[node.get("name")] = {p.get("name"): p.get("value") for p in node}
    for call in calls:
        call["android_registration"] = features.get(call["service"])
    write(out, "cordova-registration.json", features)
    write(out, "bridge-api.json", calls)
    write(out, "bridge-parser-coverage.json", {"parsed_literal_service_action_calls": len(calls),
        "unparsed_exec_text_occurrences": unparsed,
        "note": "Unparsed occurrences include comments, dynamic action names and complex callbacks; inspect before calling this a complete generic Cordova API inventory."})

    strings = {n.get("name"): "".join(n.itertext()) for n in ET.parse(decoded / "res/values/strings.xml").getroot() if n.tag == "string"}
    strings = {k: v for k, v in strings.items() if RELEVANT.search(k + " " + v)}
    write(out, "training-strings.json", strings)
    controls = []
    for path in sorted((decoded / "res").glob("layout*/*.xml")):
        if not RELEVANT.search(path.name):
            continue
        root = ET.parse(path).getroot()
        for node in root.iter():
            attrs = {k.removeprefix(A): v for k, v in node.attrib.items() if k.startswith(A) and k.removeprefix(A) in {"id", "text", "contentDescription", "clickable", "enabled", "visibility", "hint"}}
            if attrs:
                if attrs.get("text", "").startswith("@string/"):
                    attrs["resolved_text"] = strings.get(attrs["text"][8:])
                controls.append({"layout": path.relative_to(decoded).as_posix(), "class": node.tag, **attrs, "runtime_verified": False})
    write(out, "training-controls.json", controls)
    xmls = []
    for path in sorted((decoded / "res/xml").glob("*.xml")):
        text = path.read_text(encoding="utf-8")
        if "network" in path.name or "provider" in path.name or "path" in path.name or path.name == "config.xml":
            xmls.append({"source": path.relative_to(decoded).as_posix(), "xml": text})
    write(out, "configuration.json", xmls)

    natives, native_map = [], []
    for path in sorted((decoded / "lib/arm64-v8a").glob("*.so")):
        p = subprocess.run([str(rabin), "-j", "-I", "-E", "-i", "-l", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if p.returncode:
            raise RuntimeError(f"rabin2 failed for {path.name}: {p.stderr[:200]}")
        info = json.loads(p.stdout)
        exports = info.get("exports", [])
        native_map.append({"path": path.relative_to(decoded).as_posix(), "info": info.get("info"), "dependencies": info.get("libs"),
                           "export_count": len(exports), "jni_exports": [s for s in exports if s.get("name", "").startswith(("Java_", "JNI_"))]})
        if RELEVANT.search(path.name) or path.name in {"libaiengine.so", "libskegn.so", "libspeech-eval-sdk.so", "libtkplayer.so", "libjutils.so", "libqcloud_asr_realtime.so"}:
            write(out, path.name + ".json", info)
            natives.append({"path": path.relative_to(decoded).as_posix(), "evidence": path.name + ".json"})
    write(out, "native-library-map.json", native_map)
    resources = [decoded / "AndroidManifest.xml", decoded / "res/xml/config.xml", decoded / "res/xml/config_bak.xml", decoded / "res/xml/comm_boot_network_security_config.xml"]
    resources += list((decoded / "res/layout").glob("utss_*.xml")) + list((decoded / "res/layout").glob("wbv_*developer*.xml"))
    for source in resources:
        destination = out / "resources" / source.relative_to(decoded)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    result = {"apk": str(apk), "decoded": str(decoded), "files": len(files), "assets_scanned": sum(f["path"].startswith("assets/") for f in files),
              "bridge_calls": len(calls), "registered_bridge_services": len(features), "endpoint_occurrences": len(endpoints),
              "training_string_count": len(strings), "training_control_candidates": len(controls), "arm64_libraries_inventoried": len(native_map), "native_libraries": natives,
              "runtime_verified": False, "coverage_limit": "Encrypted payloads are inventoried but not decrypted; regex URLs are candidates, not proven API routes."}
    write(out, "summary.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "native_libraries"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("decoded", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--rabin", type=Path, required=True)
    args = parser.parse_args()
    audit(args.apk, args.decoded, args.out, args.rabin)
