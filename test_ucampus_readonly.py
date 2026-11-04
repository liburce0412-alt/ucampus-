import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import ucampus_readonly as app


def xml(text="Next", enabled="true", rotation="0", extra=""):
    return f'<hierarchy rotation="{rotation}"><node package="cn.unipus.cloud" bounds="[0,0][100,200]"><node package="cn.unipus.cloud" text="{text}" content-desc="advance" clickable="true" enabled="{enabled}" bounds="[10,20][60,70]" {extra}/></node></hierarchy>'


class ReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def write(self, name, content):
        (self.root / name).write_text(content, encoding="utf-8")

    def test_description_and_disabled_change_survive_delta(self):
        self.write("old.xml", xml())
        self.write("new.xml", xml(enabled="false"))
        result = app.brief(self.root, "new.xml", "old.xml")
        self.assertFalse(result["unchanged"])
        self.assertEqual(result["updated"]["1"]["description"], "advance")
        self.assertIn("disabled", result["updated"]["1"]["flags"])

    def test_delta_reconstructs_new_snapshot(self):
        self.write("old.xml", xml())
        self.write("new.xml", xml(text="Done", enabled="false"))
        old = app.read_ui(self.root, "old.xml")["elements"]
        delta = app.brief(self.root, "new.xml", "old.xml")
        for ref in delta["removed"]:
            old.pop(ref)
        old.update(delta["updated"])
        self.assertEqual(old, app.read_ui(self.root, "new.xml")["elements"])

    def test_removal_and_unchanged(self):
        self.write("old.xml", xml())
        self.assertTrue(app.brief(self.root, "old.xml", "old.xml")["unchanged"])
        self.write("new.xml", '<hierarchy><node package="cn.unipus.cloud" bounds="[0,0][100,200]"/></hierarchy>'.replace('<hierarchy>', '<hierarchy rotation="0">'))
        self.assertEqual(app.brief(self.root, "new.xml", "old.xml")["removed"], ["1"])

    def test_viewport_change_requires_full_snapshot(self):
        self.write("old.xml", xml())
        self.write("new.xml", xml(rotation="1"))
        self.assertIn("elements", app.brief(self.root, "new.xml", "old.xml"))

    def test_password_fields_are_redacted(self):
        self.write("secret.xml", xml(text="private-value", extra='password="true" hint="private-hint"'))
        result = app.encode(app.brief(self.root, "secret.xml"))
        self.assertNotIn("private-value", result)
        self.assertNotIn("private-hint", result)
        self.assertNotIn("advance", result)

    def test_boundaries_and_untrusted_xml(self):
        with tempfile.TemporaryDirectory() as outside:
            p = Path(outside) / "outside.xml"
            p.write_text(xml())
            with self.assertRaises(ValueError):
                app.read_ui(self.root, str(p))
        for content in ('<!DOCTYPE hierarchy><hierarchy/>', '<hierarchy><node package="other.app"/></hierarchy>'):
            self.write("bad.xml", content)
            with self.assertRaises(ValueError):
                app.read_ui(self.root, "bad.xml")
        (self.root / "bad.xml").write_bytes(('<!DOCTYPE hierarchy>' + xml()).encode("utf-16"))
        with self.assertRaises(ValueError):
            app.read_ui(self.root, "bad.xml")

    def test_rpc_rejects_writes_and_bad_arguments(self):
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "submit_answers", "arguments": {}}}
        self.assertEqual(app.dispatch(call, self.root)["error"]["code"], -32602)
        call["params"] = {"name": "ucampus_cached_ui", "arguments": {"xml": 3}}
        self.assertEqual(app.dispatch(call, self.root)["error"]["code"], -32602)

    def test_stdio_handshake_listing_call_and_notification(self):
        self.write("latest-ui.xml", xml())
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ucampus_cached_ui", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "ucampus_findings", "arguments": {"query": "training_deeplink"}}},
        ]
        run = subprocess.run([sys.executable, "-B", str(app.BASE / "ucampus_readonly.py"), "--ui-dir", str(self.root), "serve"], input="\n".join(app.encode(r) for r in requests)+"\n", text=True, encoding="utf-8", capture_output=True, timeout=10, check=True)
        results = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual([r["id"] for r in results], [1, 2, 3, 4])
        self.assertEqual(results[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(len(results[1]["result"]["tools"]), 2)
        self.assertTrue(json.loads(results[2]["result"]["content"][0]["text"])["cached"])
        self.assertFalse(results[3]["result"]["isError"])
        self.assertEqual(run.stderr, "")


if __name__ == "__main__":
    unittest.main()
