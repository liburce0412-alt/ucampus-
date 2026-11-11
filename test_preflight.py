"""Offline checks for the real APK evidence parser and the prepared control client."""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from bridge_client import Client, BridgeError, OutcomeUnknown, compact, dispatch
from static_audit import decode_js_calls, safe_url


def node(ident="next", text="Next", clickable=True):
    return {"id": ident, "text": text, "description": "", "bounds": [0, 0, 40, 40], "enabled": True, "clickable": clickable}


def state(ident, nodes):
    return {"snapshot_id": ident, "revision": 1, "window": 10, "captured_elapsed_ms": 100, "truncated": False, "package": "cn.unipus.cloud", "nodes": nodes}


class PreparedControlTests(unittest.TestCase):
    def test_visible_wait_retries_only_transient_reads_and_never_input(self):
        c=Client(8794,display_id=7);c.click=Mock();c.last={'snapshot_id':'old'}
        c.observe=Mock(side_effect=[BridgeError('target_not_foreground'),{'snapshot_id':'fresh'}])
        with patch('bridge_client.time.sleep'):
            self.assertEqual(c.observe_visible()['snapshot_id'],'fresh')
        self.assertIsNone(c.last);self.assertEqual(c.observe.call_count,2);c.click.assert_not_called()
        for error in (BridgeError('device_locked'),OutcomeUnknown('target_not_foreground')):
            c.observe=Mock(side_effect=error)
            with self.assertRaises(type(error)):c.observe_visible()
            c.observe.assert_called_once();c.click.assert_not_called()
        c.observe=Mock(side_effect=BridgeError('target_not_foreground'))
        with patch('bridge_client.time.sleep'),patch('bridge_client.time.monotonic',side_effect=[0,0,3]):
            with self.assertRaisesRegex(BridgeError,'target_not_foreground'):c.observe_visible()
        self.assertEqual(c.observe.call_count,2);c.click.assert_not_called()

    def test_display_identity_cannot_silently_change(self):
        c = Client(8794, display_id=3)
        c.rpc = Mock(return_value=state('wrong', {'0':node()}))
        with self.assertRaisesRegex(BridgeError, 'different display'):
            c.observe()
        for invalid in (-1, True, '3'):
            with self.assertRaises(ValueError):Client(8794, display_id=invalid)

    def test_old_bridge_cannot_be_used_for_secondary_display(self):
        c = Client(8794, display_id=3)
        c._exchange = Mock(return_value={'protocol':1, 'target':'cn.unipus.cloud'})
        with patch('bridge_client.socket.create_connection', return_value=Mock()):
            with self.assertRaises(BridgeError):c.rpc('snapshot')
        c._exchange.assert_called_once_with('ping')

    def test_every_secondary_display_operation_carries_the_target(self):
        c = Client(8794, display_id=3)
        c.socket = Mock()
        c._exchange = Mock(return_value={})
        c.rpc('click', snapshot_id='s1', ref='0')
        c._exchange.assert_called_once_with('click', snapshot_id='s1', ref='0', display_id=3)

    def test_idle_connection_can_reopen_once_for_observation(self):
        c = Client(8794)
        failure = BridgeError("Bridge connection failed; service and ADB forward must already be ready.")
        c.rpc = Mock(side_effect=[failure, state("fresh", {"0": node()})])
        self.assertEqual(c.observe()["snapshot_id"], "fresh")
        self.assertEqual(c.rpc.call_count, 2)
        c.rpc = Mock(side_effect=failure)
        with self.assertRaises(BridgeError):
            c.observe()
        self.assertEqual(c.rpc.call_count, 2)

    def test_capture_drift_retries_only_read_and_is_bounded(self):
        c = Client(8794)
        c.rpc = Mock(side_effect=[BridgeError("ui_changed_during_capture"), state("s1", {"0": node()})])
        self.assertEqual(c.observe()["snapshot_id"], "s1")
        self.assertEqual([call.args[0] for call in c.rpc.call_args_list], ["snapshot", "snapshot"])
        c.rpc = Mock(side_effect=BridgeError("ui_changed_during_capture"))
        with self.assertRaisesRegex(BridgeError, "ui_changed_during_capture"):
            c.observe()
        self.assertEqual(c.rpc.call_count, 3)

    def test_accessible_description_selector_is_exact(self):
        c = Client(8794)
        c.nodes = {"0": {**node(), "description": "下一步"}, "1": node()}
        self.assertEqual(c.select({"description": "下一步"}), "0")
        with self.assertRaises(BridgeError):
            c.select({"description": "下一"})

    def test_live_delta_retains_nodes_and_parent_click_reference(self):
        c = Client(8794)
        initial = {"0": node("card", "", True), "0.1": node("label", "Question", False)}
        c.rpc = Mock(side_effect=[state("s1", initial), {"snapshot_id": "s2", "revision": 2, "window": 10, "captured_elapsed_ms": 200,
            "truncated": False, "package": "cn.unipus.cloud", "base": "s1", "removed": ["0.1"], "updated": {"0.2": node("done", "Done", False)}}])
        first = c.observe()
        self.assertEqual(first["nodes"]["0.1"]["click_ref"], "0")
        second = c.observe()
        self.assertEqual(second["removed"], ["0.1"])
        self.assertIn("0", c.nodes)
        self.assertIn("0.2", second["updated"])

    def test_ambiguous_selectors_are_rejected(self):
        c = Client(8794)
        c.nodes = {"0": node(), "1": node()}
        with self.assertRaisesRegex(BridgeError, "ambiguous"):
            c.select({"text": "Next"})

    def test_stale_reference_sends_no_click(self):
        c = Client(8794)
        c.rpc = Mock()
        c.last = {"snapshot_id": "current"}
        with self.assertRaises(BridgeError):
            c.click("old", "0")
        c.rpc.assert_not_called()

    def test_disconnect_during_click_never_retries(self):
        c = Client(8794)
        c.socket = Mock()
        c.socket.sendall.side_effect = OSError("lost")
        c.reader = io.BytesIO()
        sent = c.socket.sendall
        with self.assertRaises(OutcomeUnknown):
            c.rpc("click", snapshot_id="s1", ref="0")
        self.assertEqual(sent.call_count, 1)
        self.assertIsNone(c.socket)

    def test_batch_rejects_already_satisfied_postcondition(self):
        c = Client(8794)
        c.nodes = {"0": node(), "1": node("result", "Result")}
        c.last = {"snapshot_id": "s1", "window": 10}
        c.observe = Mock()
        c.click = Mock()
        with self.assertRaisesRegex(BridgeError, "already present"):
            c.batch("s1", [{"click": {"id": "next"}, "expect": {"id": "result"}}])
        c.click.assert_not_called()

    def test_entire_batch_is_validated_before_first_action(self):
        c = Client(8794)
        c.observe = Mock()
        with self.assertRaises(ValueError):
            c.batch("s1", [{"click": {"id": "next"}, "expect": {"id": "result"}}, {"click": {"id": "next"}}])
        c.observe.assert_not_called()

    def test_batch_stops_if_the_decision_page_changed(self):
        c = Client(8794)
        c.nodes = {"0": node(text="Old task")}
        c.last = {"snapshot_id": "s1", "window": 10}
        c.rpc = Mock(return_value=state("s2", {"0": node(text="Different task")}))
        c.click = Mock()
        with self.assertRaisesRegex(BridgeError, "initial page changed"):
            c.batch("s1", [{"click": {"id": "next"}, "expect": {"id": "result"}}])
        c.click.assert_not_called()

    def test_batch_accepts_disabled_to_enabled_transition(self):
        c = Client(8794)
        before = {"0": node(), "1": {**node("result", "Result"), "enabled": False}}
        after = {"1": node("result", "Result")}
        c.last = {"snapshot_id": "s1", "window": 10}
        c.nodes = dict(before)
        c.rpc = Mock(side_effect=[state("s2", before), {"dispatched": True}, state("s3", after), state("s4", after)])
        result = c.batch("s1", [{"click": {"id": "next"}, "expect": {"id": "result"}}])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(sum(call.args[0] == "click" for call in c.rpc.call_args_list), 1)

    def test_control_stdio_initializes_without_connecting_to_phone(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        run = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("bridge_client.py")), "--port", "1", "serve"],
                             input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
        results = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual([r["id"] for r in results], [1, 2])
        self.assertEqual(len(results[1]["result"]["tools"]), 3)
        self.assertEqual(run.stderr, "")

    def test_password_fields_are_redacted(self):
        secret = {**node(text="private"), "description": "private", "password": True}
        self.assertNotIn("private", str(compact({"0": secret})))

    def test_bad_mcp_arguments_never_connect(self):
        c = Client(8794)
        c.rpc = Mock()
        response = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ucampus_observe", "arguments": {"full": "true"}}}, c)
        self.assertTrue(response["result"]["isError"])
        c.rpc.assert_not_called()

    def test_null_arguments_and_callback_scope_are_preserved(self):
        source = '''UCClioPlugin.prototype.stop = function () {
            exec(this.done, this.error, "UCClioPlugin", "stop", null);
        };
        channel.subscribe(function() { exec(onMessage, undefined, "UCClioPlugin", "messageChannel", []); });'''
        parsed = decode_js_calls(source, "test.js")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["arguments_expression"], "null")
        self.assertEqual(parsed[0]["js_method"], "UCClioPlugin.stop")
        self.assertNotIn("js_method", parsed[1])

    def test_url_credentials_query_and_fragment_are_removed(self):
        self.assertEqual(safe_url("https://user:secret@example.org/play?token=secret#private"), "https://example.org/play?[redacted]")


if __name__ == "__main__":
    unittest.main()
