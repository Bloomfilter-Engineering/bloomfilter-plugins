"""Stdlib-only tests for the Devin collector's runtime-specific modules.

Run from the repository root:

    python3 -m unittest discover -s plugins/agent-miner-devin/tests -v

They exercise the ATIF transcript summary, the tool-call pairing state and an
end-to-end hook sequence against a local HTTP capture server, with HOME and
the Devin data dir redirected into a temp directory so nothing touches the
real user config.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import devin_tool_pairing  # noqa: E402
import devin_transcript  # noqa: E402


def _agent_step(step_id, message="", **metadata):
    metrics = metadata.pop("metrics", None)
    step_metadata = {"created_at": f"2026-09-09T12:00:0{step_id}Z", **metadata}
    if metrics is not None:
        step_metadata["metrics"] = metrics
    return {
        "step_id": step_id,
        "source": "agent",
        "message": message,
        "metadata": step_metadata,
    }


def _user_step(step_id, text="do the thing"):
    return {
        "step_id": step_id,
        "source": "user",
        "message": text,
        "metadata": {"is_user_input": True},
    }


SAMPLE_TRANSCRIPT = {
    "schema_version": "ATIF-v1.4",
    "session_id": "sess-1",
    "agent": {"name": "devin-cli", "version": "3.9.1", "model_name": "swe-1-6-fast"},
    "steps": [
        _user_step(1, "first prompt"),
        _agent_step(
            2,
            "old turn text",
            committed_acu_cost=0.01,
            generation_model="swe-1-6-fast",
            request_id="req-old",
            metrics={"input_tokens": 10, "output_tokens": 5},
        ),
        _user_step(3, "second prompt"),
        _agent_step(
            4,
            "",
            committed_acu_cost=0.02,
            generation_model="claude-sonnet-5",
            request_id="req-a",
            finish_reason="tool_use",
            metrics={
                "input_tokens": 1000,
                "output_tokens": 50,
                "cache_read_tokens": 400,
                "cache_creation_tokens": 20,
                "ttft_ms": 812,
            },
            tool_calls=[
                {"tool_call_id": "c1", "function_name": "exec", "arguments": {}}
            ],
        ),
        _agent_step(
            5,
            [{"type": "text", "text": "Final answer."}],
            committed_acu_cost=0.0175,
            generation_model="claude-sonnet-5",
            request_id="req-b",
            finish_reason="end_turn",
            metrics={"input_tokens": 1500, "output_tokens": 300, "ttft_ms": 400},
        ),
    ],
}


class SummarizeLatestTurnTests(unittest.TestCase):
    def test_trailing_turn_only(self):
        summary = devin_transcript.summarize_latest_turn(SAMPLE_TRANSCRIPT)

        self.assertEqual(len(summary["api_calls"]), 2)
        first, second = summary["api_calls"]
        self.assertEqual(first["input_tokens"], 1000)
        self.assertEqual(first["cache_read_tokens"], 400)
        self.assertEqual(first["cache_creation_tokens"], 20)
        self.assertEqual(first["model"], "claude-sonnet-5")
        self.assertEqual(first["response_id"], "req-a")
        self.assertEqual(first["stop_reason"], "tool_use")
        self.assertEqual(second["output_tokens"], 300)
        self.assertEqual(summary["time_to_first_token_ms"], 812)
        self.assertAlmostEqual(summary["acu_cost"], 0.0375)
        self.assertEqual(summary["agent_response"], "Final answer.")

    def test_nothing_after_last_user_step_returns_none(self):
        transcript = {"steps": [_user_step(1), _agent_step(2, "x"), _user_step(3)]}
        self.assertIsNone(devin_transcript.summarize_latest_turn(transcript))

    def test_model_falls_back_to_agent_model_name(self):
        transcript = {
            "agent": {"model_name": "gpt-5.5"},
            "steps": [_user_step(1), _agent_step(2, "x", metrics={"output_tokens": 1})],
        }
        summary = devin_transcript.summarize_latest_turn(transcript)
        self.assertEqual(summary["api_calls"][0]["model"], "gpt-5.5")

    def test_standard_atif_metrics_are_accepted(self):
        step = _agent_step(2, "x")
        step["metrics"] = {
            "prompt_tokens": 120,
            "completion_tokens": 7,
            "cached_tokens": 20,
        }
        summary = devin_transcript.summarize_latest_turn(
            {"steps": [_user_step(1), step]}
        )
        call = summary["api_calls"][0]
        self.assertEqual(
            (call["input_tokens"], call["output_tokens"], call["cache_read_tokens"]),
            (100, 7, 20),
        )

    def test_hostile_values_do_not_raise(self):
        step = _agent_step(
            2, {"not": "text"}, committed_acu_cost="lots", metrics={"input_tokens": "9"}
        )
        summary = devin_transcript.summarize_latest_turn(
            {"steps": [_user_step(1), step, "junk"]}
        )
        self.assertEqual(summary, {"api_calls": []})

    def test_load_transcript_rejects_non_object_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.json")
            Path(path).write_text("[1,2]")
            self.assertIsNone(devin_transcript.load_transcript(path))
            self.assertIsNone(
                devin_transcript.load_transcript(os.path.join(tmp, "missing.json"))
            )

    def test_transcript_path_refuses_traversal(self):
        self.assertEqual(devin_transcript.transcript_path_for("../etc"), "")
        self.assertEqual(devin_transcript.transcript_path_for(""), "")


class ToolPairingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_env = dict(os.environ)
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        os.environ["HOME"] = self.tmp.name
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._old_env)))

    def test_fifo_pairing_per_tool_name(self):
        first = devin_tool_pairing.claim_tool_call_id("s1", "exec")
        second = devin_tool_pairing.claim_tool_call_id("s1", "exec")
        other = devin_tool_pairing.claim_tool_call_id("s1", "edit")

        self.assertEqual(
            devin_tool_pairing.resolve_tool_call_id("s1", "exec"), (first, True)
        )
        self.assertEqual(
            devin_tool_pairing.resolve_tool_call_id("s1", "edit"), (other, True)
        )
        self.assertEqual(
            devin_tool_pairing.resolve_tool_call_id("s1", "exec"), (second, True)
        )

    def test_out_of_order_completion_pairs_on_input_digest(self):
        # Two concurrent `exec` calls; the second finishes first. The input
        # digest picks the right parked id instead of the oldest one.
        slow = devin_tool_pairing.claim_tool_call_id(
            "s6", "exec", {"command": "pytest"}
        )
        fast = devin_tool_pairing.claim_tool_call_id("s6", "exec", {"command": "ruff"})

        self.assertEqual(
            devin_tool_pairing.resolve_tool_call_id("s6", "exec", {"command": "ruff"}),
            (fast, True),
        )
        self.assertEqual(
            devin_tool_pairing.resolve_tool_call_id(
                "s6", "exec", {"command": "pytest"}
            ),
            (slow, True),
        )

    def test_unknown_input_falls_back_to_oldest(self):
        oldest = devin_tool_pairing.claim_tool_call_id("s7", "exec", {"command": "a"})
        devin_tool_pairing.claim_tool_call_id("s7", "exec", {"command": "b"})
        self.assertEqual(
            devin_tool_pairing.resolve_tool_call_id("s7", "exec", {"command": "zzz"}),
            (oldest, True),
        )

    def test_unpaired_end_gets_fresh_id(self):
        call_id, paired = devin_tool_pairing.resolve_tool_call_id("s2", "exec")
        self.assertTrue(call_id.startswith("devin-"))
        self.assertFalse(paired)

    def test_clear_and_sweep(self):
        devin_tool_pairing.claim_tool_call_id("s3", "exec")
        devin_tool_pairing.clear_tool_pairing("s3")
        self.assertEqual(
            devin_tool_pairing.resolve_tool_call_id("s3", "exec")[1], False
        )
        devin_tool_pairing.claim_tool_call_id("s4", "exec")
        self.assertEqual(
            devin_tool_pairing.sweep_stale_tool_state(max_age_seconds=-1), 1
        )

    def test_pending_queue_is_bounded(self):
        for _ in range(devin_tool_pairing.MAX_PENDING_PER_TOOL + 5):
            devin_tool_pairing.claim_tool_call_id("s5", "exec")
        state_path = os.path.join(
            self.tmp.name, "bloomfilter", "batches", "s5.tools.json"
        )
        state = json.loads(Path(state_path).read_text())
        self.assertEqual(len(state["exec"]), devin_tool_pairing.MAX_PENDING_PER_TOOL)
        self.assertIn("id", state["exec"][0])


class _Capture(http.server.BaseHTTPRequestHandler):
    bodies: list[dict] = []
    headers_seen: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        _Capture.bodies.append(json.loads(self.rfile.read(length)))
        _Capture.headers_seen.append(
            {key.lower(): value for key, value in self.headers.items()}
        )
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # silence
        pass


class EndToEndHookTests(unittest.TestCase):
    """Drive collect_hook.py as Devin would and inspect the POSTed batch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _Capture.bodies = []
        _Capture.headers_seen = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.project_dir = os.path.join(self.tmp.name, "project")
        os.makedirs(self.project_dir)
        data_dir = os.path.join(self.tmp.name, "devin-data")
        os.makedirs(os.path.join(data_dir, "transcripts"))
        self.session_id = "sess-e2e"
        Path(data_dir, "transcripts", f"{self.session_id}.json").write_text(
            json.dumps({**SAMPLE_TRANSCRIPT, "session_id": self.session_id})
        )
        self.env = {
            **os.environ,
            "HOME": self.tmp.name,
            "XDG_CONFIG_HOME": self.tmp.name,
            "BLOOMFILTER_API_KEY": "test-key",
            "BLOOMFILTER_URL": f"http://127.0.0.1:{self.server.server_port}",
            "BLOOMFILTER_DEVIN_DATA_DIR": data_dir,
            "DEVIN_PROJECT_DIR": self.project_dir,
            "DEVIN_PLUGIN_ROOT": str(PLUGIN_DIR),
        }

    def _fire(self, event, payload):
        payload = {"hook_event_name": event, "session_id": self.session_id, **payload}
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "collect_hook.py"), event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def test_turn_is_batched_paired_and_uploaded_on_stop(self):
        self._fire("SessionStart", {"source": "startup"})
        self._fire("UserPromptSubmit", {"prompt_id": "p1", "prompt": "second prompt"})
        self._fire(
            "PreToolUse",
            {"prompt_id": "p1", "tool_name": "exec", "tool_input": {"command": "ls"}},
        )
        self._fire(
            "PostToolUse",
            {
                "prompt_id": "p1",
                "tool_name": "exec",
                "tool_input": {"command": "ls"},
                "tool_response": {"success": True, "output": "a\n", "error": None},
            },
        )
        self._fire(
            "Stop",
            {
                "prompt_id": "p1",
                "stop_hook_active": False,
                "last_assistant_message": "Done.",
            },
        )

        self.assertEqual(len(_Capture.bodies), 1)
        body = _Capture.bodies[0]
        self.assertEqual(body["source"], "devin")
        self.assertEqual(body["session_id"], self.session_id)
        self.assertEqual(_Capture.headers_seen[0].get("x-mcp-token"), "test-key")
        self.assertEqual(_Capture.headers_seen[0].get("x-plugin-source"), "devin")

        by_event = {}
        for hook in body["hooks"]:
            by_event.setdefault(hook["hook_event_name"], []).append(hook)
        self.assertEqual(
            [hook["hook_event_name"] for hook in body["hooks"]],
            ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"],
        )
        for hook in body["hooks"]:
            self.assertEqual(hook["cwd"], self.project_dir)
        self.assertIn("git_branch", by_event["SessionStart"][0])
        self.assertEqual(by_event["SessionStart"][0]["payload"]["cli_version"], "3.9.1")

        pre = by_event["PreToolUse"][0]["payload"]
        post = by_event["PostToolUse"][0]["payload"]
        self.assertTrue(pre["tool_call_id"].startswith("devin-"))
        self.assertEqual(pre["tool_call_id"], post["tool_call_id"])
        self.assertNotIn("tool_call_unpaired", post)

        stop = by_event["Stop"][0]
        self.assertEqual(stop["agent_response"], "Done.")
        self.assertEqual(len(stop["transcript_summary"]["api_calls"]), 2)
        self.assertEqual(stop["transcript_summary"]["time_to_first_token_ms"], 812)
        self.assertAlmostEqual(stop["transcript_summary"]["acu_cost"], 0.0375)

        # Drained on 2xx: the batch file is empty and the pairing file is gone.
        batch_dir = os.path.join(self.tmp.name, "bloomfilter", "batches")
        self.assertEqual(Path(batch_dir, f"{self.session_id}.jsonl").read_text(), "")
        self.assertFalse(
            os.path.exists(os.path.join(batch_dir, f"{self.session_id}.tools.json"))
        )

    def test_failed_tool_response_is_filed_as_post_tool_use_failure(self):
        self._fire("SessionStart", {"source": "startup"})
        self._fire("UserPromptSubmit", {"prompt_id": "p3", "prompt": "run it"})
        self._fire(
            "PreToolUse",
            {"prompt_id": "p3", "tool_name": "exec", "tool_input": {"command": "foo"}},
        )
        self._fire(
            "PostToolUse",
            {
                "prompt_id": "p3",
                "tool_name": "exec",
                "tool_input": {"command": "foo"},
                "tool_response": {
                    "success": False,
                    "output": "",
                    "error": "command not found: foo",
                },
            },
        )
        self._fire("Stop", {"prompt_id": "p3", "last_assistant_message": "It failed."})

        names = [hook["hook_event_name"] for hook in _Capture.bodies[0]["hooks"]]
        self.assertIn("PostToolUseFailure", names)
        self.assertNotIn("PostToolUse", names)
        failure = next(
            hook
            for hook in _Capture.bodies[0]["hooks"]
            if hook["hook_event_name"] == "PostToolUseFailure"
        )
        # The raw payload keeps Devin's own event name; only the envelope is renamed.
        self.assertEqual(failure["payload"]["hook_event_name"], "PostToolUse")
        pre = next(
            hook
            for hook in _Capture.bodies[0]["hooks"]
            if hook["hook_event_name"] == "PreToolUse"
        )
        self.assertEqual(
            pre["payload"]["tool_call_id"], failure["payload"]["tool_call_id"]
        )

    def test_write_start_hook_records_whether_the_file_existed(self):
        existing = os.path.join(self.project_dir, "existing.py")
        Path(existing).write_text("x = 1\n")
        self._fire("SessionStart", {"source": "startup"})
        self._fire("UserPromptSubmit", {"prompt_id": "p4", "prompt": "write"})
        self._fire(
            "PreToolUse",
            {
                "prompt_id": "p4",
                "tool_name": "write",
                "tool_input": {"file_path": existing, "content": "x = 2\n"},
            },
        )
        self._fire(
            "PreToolUse",
            {
                "prompt_id": "p4",
                "tool_name": "write",
                "tool_input": {
                    "file_path": os.path.join(self.project_dir, "new.py"),
                    "content": "y = 1\n",
                },
            },
        )
        self._fire("Stop", {"prompt_id": "p4", "last_assistant_message": "done"})

        pres = [
            hook["payload"]["tool_input"]
            for hook in _Capture.bodies[0]["hooks"]
            if hook["hook_event_name"] == "PreToolUse"
        ]
        self.assertEqual(pres[0]["bloomfilter_file_existed"], True)
        self.assertEqual(pres[1]["bloomfilter_file_existed"], False)
        self.assertFalse(os.path.exists(os.path.join(self.project_dir, "new.py")))

    def test_payload_from_another_runtime_is_refused(self):
        # Claude Code and Codex put transcript_path in every payload; Devin never
        # does. Such a payload must not be filed as a Devin session.
        self._fire("SessionStart", {"source": "startup"})
        self._fire(
            "UserPromptSubmit",
            {"prompt_id": "p5", "prompt": "hi", "transcript_path": "/tmp/x.jsonl"},
        )
        self._fire("Stop", {"prompt_id": "p5", "last_assistant_message": "yo"})

        names = [hook["hook_event_name"] for hook in _Capture.bodies[0]["hooks"]]
        self.assertEqual(names, ["SessionStart", "Stop"])

    def test_user_prompt_submit_carries_backfill_summary(self):
        self._fire("SessionStart", {"source": "startup"})
        self._fire("UserPromptSubmit", {"prompt_id": "p2", "prompt": "third prompt"})
        self._fire("Stop", {"prompt_id": "p2", "last_assistant_message": "ok"})

        prompt_hook = next(
            hook
            for hook in _Capture.bodies[0]["hooks"]
            if hook["hook_event_name"] == "UserPromptSubmit"
        )
        # The transcript's trailing turn is still the previous turn here, so
        # its api_calls ride along for the API's zero-token backfill.
        self.assertEqual(len(prompt_hook["transcript_summary"]["api_calls"]), 2)
        self.assertNotIn("agent_response", prompt_hook)

    def test_hook_never_exits_nonzero_or_prints_decisions(self):
        completed = self._fire(
            "PostToolUse", {"tool_name": "exec", "tool_response": {}}
        )
        self.assertEqual(completed.stdout.strip(), "")
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "collect_hook.py"), "Stop"],
            input="not json",
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
