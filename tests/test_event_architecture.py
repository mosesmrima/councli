from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml

from councli.agents import AgentRunResult, AgentRunner, cancel_active_agent_processes
from councli.cli import (
    broadcast_runner,
    decide_shared_vote,
    implementation_runner,
    native_session_runner,
    parse_turn_trailer,
    record_run_canceled,
    render_peer_context,
    shared_turn_runner,
    supports_native_session,
)
from councli.config import DEFAULT_CONFIG, AgentConfig, project_config_path, trust_project_config
from councli.council import decide_council, decide_review, empty_review, empty_vote, next_executor, parse_review, run_blackboard_council
from councli.events import EventLedger, read_events
from councli.gitops import create_worktree, diff
from councli.schema import load_schema, validate_json_schema_subset


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def state_phases(run_dir: Path) -> set[str]:
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    return set((state.get("phases") or {}).keys())


def process_is_active(pid: int) -> bool:
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            state = proc_stat.read_text(encoding="utf-8").split()[2]
            return state != "Z"
        except (OSError, IndexError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def fake_agent_script() -> str:
    return (
        "import json, pathlib, re, sys\n"
        "name = sys.argv[1]\n"
        "scenario = {}\n"
        "if len(sys.argv) == 4:\n"
        "    scenario = json.loads(pathlib.Path(sys.argv[2]).read_text())\n"
        "    prompt = sys.argv[3]\n"
        "else:\n"
        "    prompt = sys.argv[2]\n"
        "packet_match = re.search(r'PACKET_FILE=([^ ]+)', prompt)\n"
        "output_match = re.search(r'OUTPUT_FILE=([^ ]+)', prompt)\n"
        "if packet_match and output_match:\n"
        "    packet = pathlib.Path(packet_match.group(1))\n"
        "    out = pathlib.Path(output_match.group(1))\n"
        "    text = packet.read_text()\n"
        "    phase = re.search(r'^Phase: ([a-z]+)$', text, re.M).group(1)\n"
        "    attempt_match = re.search(r'Attempt: ([0-9]+)', text)\n"
        "    attempt = int(attempt_match.group(1)) if attempt_match else 1\n"
        "    out.parent.mkdir(parents=True, exist_ok=True)\n"
        "    if phase == 'vote':\n"
        "        executor = scenario.get('executor_votes', {}).get(name, 'alpha')\n"
        "        body = json.dumps({'preferred_plan': 'plan:alpha:1', 'preferred_executor': executor, 'confidence': 0.95, 'blocking_concerns': [], 'reason': name + ' vote'})\n"
        "    elif phase == 'review':\n"
        "        verdicts = scenario.get('review_verdicts', {}).get(name, ['approve'])\n"
        "        verdict = verdicts[min(attempt - 1, len(verdicts) - 1)]\n"
        "        if verdict == 'garbage':\n"
        "            body = 'not json'\n"
        "        else:\n"
        "            concerns = ['missing test'] if verdict in ('request_changes', 'replace') else []\n"
        "            body = json.dumps({'verdict': verdict, 'confidence': 0.95, 'blocking_concerns': concerns, 'reason': name + ' ' + verdict})\n"
        "    else:\n"
        "        body = phase.upper() + ' from ' + name\n"
        "    out.write_text(body)\n"
        "    print('wrote ' + str(out))\n"
        "elif 'COUNCLI_SHARED_TURN=1' in prompt:\n"
        "    packet_pointer = re.search(r'COUNCLI_PACKET_FILE=([^\\n]+)', prompt)\n"
        "    if packet_pointer:\n"
        "        prompt = pathlib.Path(packet_pointer.group(1).strip()).read_text()\n"
        "    shared_failure = scenario.get('shared_failures', {}).get(name)\n"
        "    if shared_failure:\n"
        "        print(shared_failure, file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "    intent_match = re.search(r'COUNCLI_INTENT=([^\\n]+)', prompt)\n"
        "    intent = intent_match.group(1) if intent_match else 'chat'\n"
        "    round_match = re.search(r'^Round: ([0-9]+)$', prompt, re.M)\n"
        "    round_no = int(round_match.group(1)) if round_match else 1\n"
        "    print(name + ' shared ' + intent + ' response round ' + str(round_no))\n"
        "    print('COUNCLI_TRAILER')\n"
        "    print('continue: false')\n"
        "    print('recommend: none')\n"
        "    print('summary: ' + name + ' handled ' + intent)\n"
        "    if intent == 'vote':\n"
        "        print('vote: option-' + name)\n"
        "        print('confidence: 0.9')\n"
        "else:\n"
        "    path = pathlib.Path('README.md')\n"
        "    suffix = ' addressed missing test' if 'missing test' in prompt else ''\n"
        "    path.write_text(path.read_text() + 'implemented by ' + name + suffix + '\\n')\n"
        "    pathlib.Path('implemented.txt').write_text('implemented by ' + name + '\\n')\n"
        "    print('implemented by ' + name)\n"
    )


def fake_default_cli_script() -> str:
    return (
        f"#!{PYTHON}\n"
        "import json, os, pathlib, re, sys\n"
        "tool = pathlib.Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "log_path = pathlib.Path(os.environ['COUNCLI_FAKE_AGENT_LOG'])\n"
        "def log(kind, **extra):\n"
        "    log_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    with log_path.open('a', encoding='utf-8') as handle:\n"
        "        handle.write(json.dumps({'tool': tool, 'kind': kind, 'args': args, **extra}, sort_keys=True) + '\\n')\n"
        "def fail(message):\n"
        "    log('error', message=message)\n"
        "    print(message, file=sys.stderr)\n"
        "    raise SystemExit(42)\n"
        "if args == ['--version']:\n"
        "    log('version')\n"
        "    print(f'{tool} 9.9.9')\n"
        "    raise SystemExit(0)\n"
        "readiness = {\n"
        "    'codex': ['doctor'],\n"
        "    'claude': ['auth', 'status'],\n"
        "    'agy': ['models'],\n"
        "    'codewhale': ['doctor'],\n"
        "    'kimi': ['doctor'],\n"
        "}\n"
        "if args == readiness.get(tool):\n"
        "    log('readiness')\n"
        "    print('ok')\n"
        "    raise SystemExit(0)\n"
        "prompt = None\n"
        "if tool == 'codex':\n"
        "    if not (args and args[0] == 'exec' and '--sandbox' in args and 'read-only' in args and '--skip-git-repo-check' in args):\n"
        "        fail('bad codex argv')\n"
        "    prompt = args[-1]\n"
        "elif tool == 'claude':\n"
        "    if '--permission-mode' not in args or 'plan' not in args or '-p' not in args:\n"
        "        fail('bad claude argv')\n"
        "    prompt = args[args.index('-p') + 1]\n"
        "elif tool == 'agy':\n"
        "    if '--sandbox' not in args or '--print' not in args:\n"
        "        fail('bad agy argv')\n"
        "    prompt = args[args.index('--print') + 1]\n"
        "elif tool == 'codewhale':\n"
        "    if len(args) != 2 or args[0] != 'exec':\n"
        "        fail('bad codewhale argv')\n"
        "    prompt = args[1]\n"
        "elif tool == 'kimi':\n"
        "    if len(args) != 2 or args[0] != '--prompt' or '--yolo' in args:\n"
        "        fail('bad kimi argv')\n"
        "    prompt = args[1]\n"
        "else:\n"
        "    fail('unknown tool')\n"
        "packet_match = re.search(r'COUNCLI_PACKET_FILE=([^\\n]+)', prompt or '')\n"
        "if packet_match:\n"
        "    packet = pathlib.Path(packet_match.group(1).strip()).read_text(encoding='utf-8')\n"
        "    if 'COUNCLI_SHARED_TURN=1' not in prompt or f'COUNCLI_PARTICIPANT={tool}' not in packet:\n"
        "        fail('bad shared packet')\n"
        "    prompt_kind = 'packet'\n"
        "else:\n"
        "    prompt_kind = 'synthesis'\n"
        "log('prompt', prompt_kind=prompt_kind)\n"
        "print(f'{tool} handled {prompt_kind}')\n"
        "print('COUNCLI_TRAILER')\n"
        "print('continue: false')\n"
        "print('recommend: none')\n"
        "print(f'summary: {tool} ok')\n"
    )


class EventArchitectureTests(unittest.TestCase):
    def set_state_home(self, path: Path) -> None:
        previous = os.environ.get("COUNCLI_STATE_HOME")
        os.environ["COUNCLI_STATE_HOME"] = str(path)

        def restore() -> None:
            if previous is None:
                os.environ.pop("COUNCLI_STATE_HOME", None)
            else:
                os.environ["COUNCLI_STATE_HOME"] = previous

        self.addCleanup(restore)

    def prepare_fake_repo(
        self,
        tmp: str,
        *,
        scenario: dict | None = None,
        agents: tuple[str, ...] = ("alpha", "beta"),
        max_rounds: int = 2,
    ) -> tuple[Path, Path]:
        self.set_state_home(Path(tmp) / "state")
        root = Path(tmp) / "repo"
        root.mkdir()
        subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        (root / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
        script = root / "fake_agent.py"
        script.write_text(fake_agent_script(), encoding="utf-8")
        scenario_path = root / "scenario.json"
        scenario_path.write_text(json.dumps(scenario or {}), encoding="utf-8")
        config_dir = root / ".councli"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "agents": {
                        name: {
                            "enabled": True,
                            "backend": "exec",
                            "binary": PYTHON,
                            "command": [PYTHON, str(script), name, str(scenario_path), "{prompt}"],
                            "broadcast_command": [PYTHON, str(script), name, str(scenario_path), "{prompt}"],
                            "timeout_seconds": 60,
                        }
                        for name in agents
                    },
                    "consensus": {"max_rounds": max_rounds, "min_confidence": 0.55},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        trust_project_config(root, reason="test", repair_identity=True)
        return root, scenario_path

    def experimental_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["COUNCLI_EXPERIMENTAL"] = "1"
        return env

    def run_fake_councli(self, root: Path, *extra_args: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        proc = subprocess.run(
            [
                PYTHON,
                "-m",
                "councli",
                "run",
                "add implemented file",
                "-C",
                str(root),
                "--allow-dirty",
                *extra_args,
            ],
            cwd=REPO_ROOT,
            env=self.experimental_env(),
            text=True,
            capture_output=True,
            check=False,
        )
        latest = max((root / ".councli" / "runs").iterdir(), key=lambda path: path.name)
        return proc, latest

    def test_packaged_protocol_schemas_validate_basic_shapes(self) -> None:
        response_schema = load_schema("response")
        errors = validate_json_schema_subset(
            {
                "schema_version": "councli.response.v1",
                "id": "resp_test",
                "request_id": "turn_test",
                "kind": "participant.response",
                "participant": "alpha",
                "intent": "chat",
                "round": 1,
                "status": "ok",
                "body_ref": "shared/chat.round1/alpha.md",
                "summary": "ok",
                "continue": False,
                "recommend": "none",
                "vote": None,
                "failure_class": "",
                "exit_code": 0,
                "duration_seconds": 0.1,
                "command": ["fake", "{prompt}"],
            },
            response_schema,
        )
        self.assertEqual(errors, [])

        invalid = validate_json_schema_subset({"schema_version": "wrong"}, response_schema)
        self.assertTrue(any("schema_version" in error for error in invalid))

    def test_event_ledger_renders_state_and_blackboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            ledger = EventLedger(run_dir, run_id="run")
            ledger.append("run.started", payload={"task": "build a tiny app"})
            ledger.append("participant.joined", participant="codex", payload={"reason": "available"})
            content_ref = ledger.write_blob("propose", "codex", "PLAN\n- keep it simple")
            ledger.append(
                "response.received",
                phase="propose",
                participant="codex",
                refs={"content": content_ref},
            )
            ledger.append("turn.canceled", status="canceled", payload={"rounds": 1})
            ledger.render()

            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            blackboard = (run_dir / "blackboard.md").read_text(encoding="utf-8")

            self.assertEqual(state["task"], "build a tiny app")
            self.assertIn("codex", state["participants"])
            self.assertEqual(state["phases"]["propose"]["codex"]["content"], "PLAN\n- keep it simple")
            self.assertEqual(state["run_canceled"]["rounds"], 1)
            self.assertIn("PLAN\n- keep it simple", blackboard)
            self.assertIn("## Run Canceled", blackboard)

    def test_metrics_reports_events_sidecar_durations_and_artifact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            run_dir = root / ".councli" / "runs" / "metrics-run"
            sidecar_path = run_dir / "shared" / "chat.round1" / "alpha.response.json"
            sidecar_path.parent.mkdir(parents=True)
            sidecar_path.write_text(
                json.dumps({"duration_seconds": 1.25, "failure_class": ""}),
                encoding="utf-8",
            )
            raw_log = root / ".councli" / "session-recordings" / "alpha.raw.log"
            raw_log.parent.mkdir(parents=True)
            raw_log.write_text("terminal output\n", encoding="utf-8")
            ledger = EventLedger(run_dir, run_id="metrics-run")
            ledger.append("run.started", payload={"task": "hello", "mode": "shared_turn", "intent": "chat"})
            ledger.append(
                "response.received",
                phase="chat.round1",
                participant="alpha",
                status="ok",
                refs={"sidecar": "shared/chat.round1/alpha.response.json"},
                payload={"mode": "shared_turn", "intent": "chat", "failure_class": ""},
            )
            ledger.append(
                "response.received",
                phase="chat.round1",
                participant="beta",
                status="failed",
                payload={"mode": "shared_turn", "intent": "chat", "failure_class": "auth_required"},
            )
            ledger.append("run.completed", payload={"mode": "shared_turn", "intent": "chat"})
            ledger.render()
            metrics_path = Path(tmp) / "metrics.prom"

            proc = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "councli",
                    "metrics",
                    "-C",
                    str(root),
                    "--json",
                    "--openmetrics-output",
                    str(metrics_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["schema_version"], "councli.metrics.v1")
            self.assertEqual(data["runs"]["total"], 1)
            self.assertIn({"intent": "chat", "status": "completed", "count": 1}, data["turns_total"])
            alpha = next(item for item in data["participant_calls"] if item["participant"] == "alpha")
            beta = next(item for item in data["participant_calls"] if item["participant"] == "beta")
            self.assertEqual(alpha["count"], 1)
            self.assertEqual(alpha["duration_seconds_total"], 1.25)
            self.assertEqual(beta["failure_class"], "auth_required")
            self.assertGreater(data["artifact_bytes"]["by_class"]["run"], 0)
            self.assertGreater(data["artifact_bytes"]["by_class"]["raw-log"], 0)
            metrics_text = metrics_path.read_text(encoding="utf-8")
            self.assertIn('councli_turns_total{intent="chat",status="completed"} 1', metrics_text)
            self.assertIn('councli_participant_calls_total{participant="beta",status="failed",failure_class="auth_required"} 1', metrics_text)

    def test_record_run_canceled_renders_canceled_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            ledger = EventLedger(run_dir, run_id="run")
            ledger.append("run.started", payload={"task": "implement cancellation"})
            ledger.render()

            record_run_canceled(
                run_dir=run_dir,
                task="implement cancellation",
                phase="implementation",
                stopped_processes=2,
                executor="alpha",
                attempt=1,
                worktree="/tmp/worktree",
            )

            events = read_events(run_dir)
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            blackboard = (run_dir / "blackboard.md").read_text(encoding="utf-8")
            self.assertEqual(events[-1]["type"], "run.canceled")
            self.assertEqual(events[-1]["status"], "canceled")
            self.assertEqual(state["run_canceled"]["phase"], "implementation")
            self.assertEqual(state["run_canceled"]["executor"], "alpha")
            self.assertEqual(state["run_canceled"]["stopped_processes"], 2)
            self.assertIn("## Run Canceled", blackboard)

    def test_event_ledger_uses_run_lock_for_cross_process_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            script = (
                "from pathlib import Path\n"
                "import sys\n"
                "from councli.events import EventLedger\n"
                "ledger = EventLedger(Path(sys.argv[1]), run_id='run')\n"
                "for i in range(50):\n"
                "    ledger.append('test.event', participant=sys.argv[2], payload={'i': i})\n"
            )
            procs = [
                subprocess.Popen(
                    [PYTHON, "-c", script, str(run_dir), name],
                    cwd=REPO_ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for name in ("alpha", "beta")
            ]
            outputs = [proc.communicate(timeout=30) for proc in procs]
            for proc, output in zip(procs, outputs, strict=True):
                self.assertEqual(proc.returncode, 0, output[0] + output[1])

            events = read_events(run_dir)
            seqs = [event["seq"] for event in events]
            self.assertEqual(len(events), 100)
            self.assertEqual(seqs, list(range(100)))
            self.assertTrue((run_dir / "run.lock").exists())

    def test_exec_timeout_terminates_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "child.pid"
            script = root / "spawn_child.py"
            script.write_text(
                "\n".join(
                    [
                        "import pathlib, subprocess, sys, time",
                        "pid_file = pathlib.Path(sys.argv[1])",
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                        "pid_file.write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(60)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            runner = AgentRunner(
                "slow",
                AgentConfig(
                    backend="exec",
                    binary=PYTHON,
                    command=[PYTHON, str(script), str(pid_file)],
                    timeout_seconds=1,
                ),
            )

            result = runner.run("ignored", cwd=root)

            self.assertFalse(result.ok)
            self.assertEqual(result.failure_class, "timeout")
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())
            deadline = time.monotonic() + 5
            while process_is_active(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_is_active(child_pid), f"child process {child_pid} survived timeout cleanup")

    def test_cancel_active_agent_processes_terminates_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "child.pid"
            script = root / "spawn_child.py"
            script.write_text(
                "\n".join(
                    [
                        "import pathlib, subprocess, sys, time",
                        "pid_file = pathlib.Path(sys.argv[1])",
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                        "pid_file.write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(60)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            runner = AgentRunner(
                "slow",
                AgentConfig(
                    backend="exec",
                    binary=PYTHON,
                    command=[PYTHON, str(script), str(pid_file)],
                    timeout_seconds=60,
                ),
            )
            result_holder: dict[str, AgentRunResult] = {}

            def run_agent() -> None:
                result_holder["result"] = runner.run("ignored", cwd=root)

            thread = threading.Thread(target=run_agent)
            thread.start()
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists())

            stopped = cancel_active_agent_processes()

            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(stopped, 1)
            result = result_holder["result"]
            self.assertFalse(result.ok)
            self.assertEqual(result.failure_class, "canceled")
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())
            deadline = time.monotonic() + 5
            while process_is_active(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_is_active(child_pid), f"child process {child_pid} survived cancellation cleanup")

    def test_unavailable_agent_run_preserves_readiness_failure_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = AgentRunner(
                "not-ready",
                AgentConfig(
                    backend="exec",
                    binary=PYTHON,
                    command=[PYTHON, "-c", "print('should not run')", "{prompt}"],
                    readiness_command=[PYTHON, "-c", "print('No model configured'); raise SystemExit(1)"],
                ),
            )

            result = runner.run("ignored", cwd=root)

            self.assertFalse(result.ok)
            self.assertTrue(result.skipped)
            self.assertEqual(result.failure_class, "model_unconfigured")
            self.assertIn("readiness probe failed", result.error)

    def test_council_dry_run_writes_event_spine_and_plan_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".councli" / "runs" / "dry-council"
            runners = {
                "codex": AgentRunner(
                    "codex",
                    AgentConfig(
                        backend="exec",
                        binary=PYTHON,
                        command=[PYTHON, "-c", "print('unused')"],
                    ),
                ),
                "claude": AgentRunner(
                    "claude",
                    AgentConfig(
                        backend="exec",
                        binary=PYTHON,
                        command=[PYTHON, "-c", "print('unused')"],
                    ),
                ),
            }

            result = run_blackboard_council(
                task="design a CLI",
                runners=runners,
                root=root,
                run_dir=run_dir,
                dry_run=True,
            )

            events = read_events(run_dir)
            event_types = [event["type"] for event in events]
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))

            self.assertTrue(result.decision["approved"])
            self.assertEqual(result.decision["selected_plan"], "plan:codex:1")
            self.assertIn("run.started", event_types)
            self.assertIn("participant.joined", event_types)
            self.assertIn("plan.candidate.created", event_types)
            self.assertIn("ballot.submitted", event_types)
            self.assertIn("decision.finalized", event_types)
            self.assertIn("plan:codex:1", state["plans"])
            self.assertTrue((run_dir / "events.jsonl").exists())
            self.assertTrue((run_dir / "blackboard.md").exists())

    def test_single_participant_exec_backend_is_unreviewed_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".councli" / "runs" / "exec-council"
            script = (
                "import json, re, sys, pathlib\n"
                "prompt = sys.argv[1]\n"
                "packet = pathlib.Path(re.search(r'PACKET_FILE=([^ ]+)', prompt).group(1))\n"
                "out = pathlib.Path(re.search(r'OUTPUT_FILE=([^ ]+)', prompt).group(1))\n"
                "text = packet.read_text()\n"
                "phase = re.search(r'Phase: ([a-z]+)', text).group(1)\n"
                "out.parent.mkdir(parents=True, exist_ok=True)\n"
                "if phase == 'vote':\n"
                "    body = json.dumps({'preferred_plan': 'plan:fake:1', 'preferred_executor': 'fake', 'confidence': 0.9, 'blocking_concerns': [], 'reason': 'packet test'})\n"
                "else:\n"
                "    body = phase.upper() + ' from packet test'\n"
                "out.write_text(body)\n"
                "print('wrote ' + str(out))\n"
            )
            runners = {
                "fake": AgentRunner(
                    "fake",
                    AgentConfig(
                        backend="exec",
                        binary=PYTHON,
                        command=[PYTHON, "-c", script, "{prompt}"],
                    ),
                ),
            }

            result = run_blackboard_council(
                task="verify packet delivery",
                runners=runners,
                root=root,
                run_dir=run_dir,
                dry_run=False,
            )

            events = read_events(run_dir)
            event_types = [event["type"] for event in events]
            packet_files = list((run_dir / "packets" / "fake").glob("*.md"))

            self.assertFalse(result.decision["approved"])
            self.assertEqual(result.decision["status"], "unreviewed_recommendation")
            self.assertEqual(result.decision["selected_plan"], "plan:fake:1")
            self.assertIn("view.sent", event_types)
            self.assertIn("run.completed", event_types)
            self.assertGreaterEqual(len(packet_files), 4)
            self.assertTrue((run_dir / "incoming" / "vote" / "fake.json").exists())

    def test_council_exec_backend_approves_two_participant_packet_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".councli" / "runs" / "two-exec-council"
            script = (
                "import json, re, sys, pathlib\n"
                "prompt = sys.argv[1]\n"
                "packet = pathlib.Path(re.search(r'PACKET_FILE=([^ ]+)', prompt).group(1))\n"
                "out = pathlib.Path(re.search(r'OUTPUT_FILE=([^ ]+)', prompt).group(1))\n"
                "text = packet.read_text()\n"
                "participant = re.search(r'^Participant: (.+)$', text, re.M).group(1).strip()\n"
                "phase = re.search(r'^Phase: ([a-z]+)$', text, re.M).group(1)\n"
                "participants = re.search(r'^Participants: (.+)$', text, re.M).group(1).split(', ')\n"
                "out.parent.mkdir(parents=True, exist_ok=True)\n"
                "if phase == 'vote':\n"
                "    body = json.dumps({'preferred_plan': 'plan:alpha:1', 'preferred_executor': participants[0], 'confidence': 0.9, 'blocking_concerns': [], 'reason': participant + ' vote'})\n"
                "else:\n"
                "    body = phase.upper() + ' from ' + participant\n"
                "out.write_text(body)\n"
                "print('wrote ' + str(out))\n"
            )
            runners = {
                name: AgentRunner(
                    name,
                    AgentConfig(
                        backend="exec",
                        binary=PYTHON,
                        command=[PYTHON, "-c", script, "{prompt}"],
                    ),
                )
                for name in ("alpha", "beta")
            }

            result = run_blackboard_council(
                task="verify concurrent packet delivery",
                runners=runners,
                root=root,
                run_dir=run_dir,
                dry_run=False,
            )

            events = read_events(run_dir)
            event_types = [event["type"] for event in events]

            self.assertTrue(result.decision["approved"])
            self.assertEqual(result.decision["status"], "approved")
            self.assertEqual(result.decision["selected_plan"], "plan:alpha:1")
            self.assertEqual(result.decision["selected_executor"], "alpha")
            self.assertIn("orient", state_phases(run_dir))
            self.assertEqual(event_types.count("view.sent"), 10)
            self.assertIn("run.completed", event_types)

    def test_vote_parse_failure_is_abstention_not_veto(self) -> None:
        votes = {
            "alpha": {
                "schema_version": "councli.vote.v1",
                "kind": "council.vote",
                "valid": True,
                "preferred_plan": "plan:alpha:1",
                "preferred_executor": "alpha",
                "confidence": 0.9,
                "blocking_concerns": [],
                "reason": "valid",
            },
            "beta": {
                "schema_version": "councli.vote.v1",
                "kind": "council.vote",
                "valid": True,
                "preferred_plan": "plan:alpha:1",
                "preferred_executor": "alpha",
                "confidence": 0.9,
                "blocking_concerns": [],
                "reason": "valid",
            },
            "gamma": empty_vote("invalid JSON vote"),
        }

        decision = decide_council(votes, ["alpha", "beta", "gamma"], ["plan:alpha:1", "plan:beta:1", "plan:gamma:1"])

        self.assertTrue(decision["approved"])
        self.assertEqual(decision["schema_version"], "councli.decision.v1")
        self.assertEqual(decision["kind"], "council.decision")
        self.assertEqual(decision["status"], "approved")
        self.assertEqual(decision["blocking_concerns"], [])
        self.assertEqual(decision["abstentions"], {"gamma": "invalid JSON vote"})

    def test_tmux_executor_requires_prompt_capable_exec_command(self) -> None:
        runner = AgentRunner(
            "interactive_only",
            AgentConfig(
                backend="tmux",
                binary=PYTHON,
                command=[PYTHON],
            ),
        )

        with self.assertRaises(ValueError):
            implementation_runner(runner)

    def test_broadcast_runner_falls_back_to_prompt_capable_command(self) -> None:
        runner = AgentRunner(
            "yolo",
            AgentConfig(
                backend="exec",
                binary=PYTHON,
                command=[PYTHON, "-c", "print('could edit')", "{prompt}"],
            ),
        )

        self.assertEqual(broadcast_runner(runner).config.command, runner.config.command)

        unsafe_fallback = AgentRunner(
            "unsafe",
            AgentConfig(
                backend="exec",
                binary=PYTHON,
                command=[PYTHON, "-c", "print('allowed fallback')", "{prompt}"],
                command_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "full_permission"],
            ),
        )
        with self.assertRaisesRegex(ValueError, "policy_denied"):
            broadcast_runner(unsafe_fallback)

        explicit_policy = AgentRunner(
            "explicit",
            unsafe_fallback.config.model_copy(update={"broadcast_policy": "allow_full_permission"}),
        )
        self.assertEqual(broadcast_runner(explicit_policy).config.command, explicit_policy.config.command)

    def test_shared_turn_runner_denies_unsafe_read_only_command_by_default(self) -> None:
        unsafe = AgentRunner(
            "unsafe",
            AgentConfig(
                backend="exec",
                binary=PYTHON,
                command=[PYTHON, "-c", "print('could edit')", "{prompt}"],
                command_capabilities=["reads_workspace", "writes_workspace", "runs_tools", "full_permission"],
            ),
        )
        with self.assertRaisesRegex(ValueError, "policy_denied"):
            shared_turn_runner(unsafe, intent_name="chat")

        explicit_policy = AgentRunner(
            "explicit",
            unsafe.config.model_copy(update={"read_only_policy": "allow_full_permission"}),
        )
        self.assertEqual(shared_turn_runner(explicit_policy, intent_name="chat").config.command, explicit_policy.config.command)

    def test_review_parser_and_decision_helpers(self) -> None:
        def valid_review(verdict: str, *, confidence: float = 0.9, concerns: list[str] | None = None) -> dict:
            return {
                "schema_version": "councli.review.v1",
                "kind": "review.verdict",
                "valid": True,
                "verdict": verdict,
                "confidence": confidence,
                "blocking_concerns": concerns or [],
                "abstained": False,
            }

        parsed = parse_review('prefix {"verdict":"approve","confidence":"high","blocking_concerns":[],"reason":"ok"} suffix')
        self.assertEqual(parsed["verdict"], "approve")
        self.assertFalse(parsed["abstained"])
        self.assertGreater(parsed["confidence"], 0.8)

        bad = parse_review("not json")
        self.assertTrue(bad["abstained"])
        self.assertEqual(bad["blocking_concerns"], [])

        accepted = decide_review(
            {
                "a": valid_review("approve"),
                "b": valid_review("approve"),
                "c": parse_review("bad"),
            },
            ["a", "b", "c"],
            attempt=1,
        )
        self.assertEqual(accepted["verdict"], "accepted")

        replace = decide_review(
            {
                "a": valid_review("replace"),
                "b": valid_review("replace"),
            },
            ["a", "b"],
            attempt=1,
        )
        self.assertEqual(replace["verdict"], "replace")

        revise = decide_review(
            {
                "a": valid_review("approve", concerns=["missing test"]),
                "b": valid_review("request_changes"),
            },
            ["a", "b"],
            attempt=1,
        )
        self.assertEqual(revise["verdict"], "revise")
        self.assertEqual(next_executor({"alpha": 3, "beta": 2, "gamma": 0}, exclude={"alpha"}), "beta")
        self.assertEqual(next_executor({"alpha": 3, "beta": 0}, exclude={"alpha"}, participants=["alpha", "beta"]), "beta")
        self.assertIsNone(next_executor({"alpha": 3}, exclude={"alpha"}, participants=["alpha"]))

        one_good_one_bad = decide_review(
            {
                "a": valid_review("approve"),
                "b": parse_review("bad"),
            },
            ["a", "b"],
            attempt=1,
        )
        self.assertEqual(one_good_one_bad["verdict"], "accepted")

    def test_confidence_threshold_excludes_low_confidence_votes_and_reviews(self) -> None:
        votes = {
            "alpha": {
                "schema_version": "councli.vote.v1",
                "kind": "council.vote",
                "valid": True,
                "preferred_plan": "plan:alpha:1",
                "preferred_executor": "alpha",
                "confidence": 0.4,
                "blocking_concerns": [],
                "reason": "weak",
            },
            "beta": {
                "schema_version": "councli.vote.v1",
                "kind": "council.vote",
                "valid": True,
                "preferred_plan": "plan:alpha:1",
                "preferred_executor": "alpha",
                "confidence": 0.4,
                "blocking_concerns": [],
                "reason": "weak",
            },
        }

        decision = decide_council(votes, ["alpha", "beta"], ["plan:alpha:1", "plan:beta:1"], min_confidence=0.55)
        self.assertFalse(decision["approved"])
        self.assertEqual(decision["status"], "low_confidence")
        self.assertEqual(decision["plan_votes"]["plan:alpha:1"], 0)
        self.assertEqual(decision["low_confidence_votes"], {"alpha": 0.4, "beta": 0.4})

        review_decision = decide_review(
            {
                "alpha": {
                    "schema_version": "councli.review.v1",
                    "kind": "review.verdict",
                    "valid": True,
                    "verdict": "approve",
                    "confidence": 0.4,
                    "blocking_concerns": [],
                    "abstained": False,
                },
                "beta": {
                    "schema_version": "councli.review.v1",
                    "kind": "review.verdict",
                    "valid": True,
                    "verdict": "approve",
                    "confidence": 0.4,
                    "blocking_concerns": [],
                    "abstained": False,
                },
            },
            ["alpha", "beta"],
            attempt=1,
            min_confidence=0.55,
        )
        self.assertFalse(review_decision["approved"])
        self.assertEqual(review_decision["schema_version"], "councli.decision.v1")
        self.assertEqual(review_decision["kind"], "review.decision")
        self.assertEqual(review_decision["verdict"], "needs_user")
        self.assertEqual(review_decision["counts"]["approve"], 0)
        self.assertEqual(review_decision["low_confidence_reviews"], {"alpha": 0.4, "beta": 0.4})

    def test_schema_less_votes_and_reviews_do_not_drive_decisions(self) -> None:
        decision = decide_council(
            {
                "alpha": {
                    "preferred_plan": "plan:alpha:1",
                    "preferred_executor": "alpha",
                    "confidence": 1.0,
                    "blocking_concerns": [],
                    "abstained": False,
                },
                "beta": empty_vote("invalid JSON vote"),
            },
            ["alpha", "beta"],
            ["plan:alpha:1", "plan:beta:1"],
        )
        self.assertFalse(decision["approved"])
        self.assertEqual(decision["abstentions"]["alpha"], "invalid vote schema")

        review_decision = decide_review(
            {
                "alpha": {
                    "verdict": "approve",
                    "confidence": 1.0,
                    "blocking_concerns": [],
                    "abstained": False,
                },
                "beta": empty_review("invalid JSON review"),
            },
            ["alpha", "beta"],
            attempt=1,
        )
        self.assertFalse(review_decision["approved"])
        self.assertEqual(review_decision["abstentions"]["alpha"], "invalid review schema")

    def test_projection_includes_implementation_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            ledger = EventLedger(run_dir, run_id="run")
            ledger.append("run.started", payload={"task": "implement"})
            ledger.append(
                "implementation.started",
                participant="alpha",
                payload={"attempt": 1, "executor": "alpha", "selected_plan": "plan:alpha:1", "worktree": "/tmp/wt", "branch": "b"},
            )
            diff_ref = ledger.write_blob("implementation", "diff", "diff --git a/file b/file", suffix="patch")
            result_ref = ledger.write_blob("implementation", "result", "ok")
            ledger.append(
                "implementation.diff_submitted",
                participant="alpha",
                refs={"diff": diff_ref, "result": result_ref},
                payload={"attempt": 1, "executor": "alpha", "ok": True, "branch": "b"},
            )
            ledger.append(
                "review.submitted",
                phase="review",
                participant="beta",
                payload={"attempt": 1, "review": {"verdict": "approve", "blocking_concerns": [], "abstained": False}},
            )
            ledger.append("review.finalized", phase="review", payload={"attempt": 1, "verdict": "accepted"})
            ledger.append("run.completed", payload={"implemented": True, "status": "accepted"})
            ledger.render()

            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            blackboard = (run_dir / "blackboard.md").read_text(encoding="utf-8")

            self.assertEqual(state["implementation"]["attempts"][0]["diff_ref"], diff_ref)
            self.assertEqual(state["reviews"]["1"]["beta"]["verdict"], "approve")
            self.assertEqual(state["review_decision"]["verdict"], "accepted")
            self.assertEqual(state["run_completed"]["status"], "accepted")
            self.assertIn("## Implementation", blackboard)
            self.assertIn("## Review Decision", blackboard)

    def test_cli_run_executes_and_reviews_with_fake_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc, latest = self.run_fake_councli(root)

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            events = read_events(latest)
            event_types = [event["type"] for event in events]
            state = json.loads((latest / "state.json").read_text(encoding="utf-8"))
            diff_text = (latest / "implementation" / "diff.patch").read_text(encoding="utf-8")

            self.assertIn("implementation.diff_submitted", event_types)
            self.assertIn("review.submitted", event_types)
            self.assertIn("review.finalized", event_types)
            self.assertEqual(state["review_decision"]["verdict"], "accepted")
            self.assertTrue(state["run_completed"]["implemented"])
            self.assertIn("implemented by alpha", diff_text)

    def test_cli_run_replaces_executor_with_zero_vote_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(
                tmp,
                scenario={"review_verdicts": {"beta": ["replace"]}},
            )
            proc, latest = self.run_fake_councli(root)

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            state = json.loads((latest / "state.json").read_text(encoding="utf-8"))
            attempts = state["implementation"]["attempts"]

            self.assertEqual([attempt["executor"] for attempt in attempts], ["alpha", "beta"])
            self.assertEqual(state["review_decision"]["verdict"], "accepted")
            self.assertTrue(state["run_completed"]["implemented"])
            self.assertIn("implemented by beta", (latest / "implementation" / "diff.patch").read_text(encoding="utf-8"))

    def test_cli_run_revises_same_executor_then_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(
                tmp,
                scenario={"review_verdicts": {"beta": ["request_changes", "approve"]}},
            )
            proc, latest = self.run_fake_councli(root)

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            state = json.loads((latest / "state.json").read_text(encoding="utf-8"))
            attempts = state["implementation"]["attempts"]
            diff_text = (latest / "implementation" / "diff.patch").read_text(encoding="utf-8")

            self.assertEqual([attempt["executor"] for attempt in attempts], ["alpha", "alpha"])
            self.assertEqual(state["review_decision"]["verdict"], "accepted")
            self.assertTrue(state["run_completed"]["implemented"])
            self.assertIn("addressed missing test", diff_text)

    def test_cli_run_rounds_exhausted_after_repeated_review_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(
                tmp,
                scenario={"review_verdicts": {"beta": ["request_changes", "request_changes"]}},
                max_rounds=2,
            )
            proc, latest = self.run_fake_councli(root)

            self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
            state = json.loads((latest / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(len(state["implementation"]["attempts"]), 2)
            self.assertFalse(state["run_completed"]["implemented"])
            self.assertEqual(state["run_completed"]["status"], "rounds_exhausted")

    def test_cli_run_single_participant_force_is_unreviewed_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp, agents=("alpha",))
            proc, latest = self.run_fake_councli(root, "--force")

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            state = json.loads((latest / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(len(state["implementation"]["attempts"]), 1)
            self.assertEqual(state["review_decision"]["verdict"], "unreviewed_implementation")
            self.assertTrue(state["run_completed"]["implemented"])

    def test_cli_run_malformed_review_abstains_without_blocking_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(
                tmp,
                scenario={"review_verdicts": {"beta": ["approve"], "gamma": ["garbage"]}},
                agents=("alpha", "beta", "gamma"),
            )
            proc, latest = self.run_fake_councli(root)

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            state = json.loads((latest / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(state["review_decision"]["verdict"], "accepted")
            self.assertEqual(state["review_decision"]["abstentions"], {"gamma": "could not find JSON review"})
            self.assertTrue(state["run_completed"]["implemented"])

    def test_orient_is_not_registered_as_plan_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".councli" / "runs" / "dry-council"
            runners = {
                name: AgentRunner(
                    name,
                    AgentConfig(
                        backend="exec",
                        binary=PYTHON,
                        command=[PYTHON, "-c", "print('unused')"],
                    ),
                )
                for name in ("alpha", "beta")
            }

            run_blackboard_council(
                task="check orient",
                runners=runners,
                root=root,
                run_dir=run_dir,
                dry_run=True,
            )
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))

            self.assertIn("orient", state["phases"])
            self.assertEqual(set(state["plans"]), {"plan:alpha:1", "plan:beta:1"})

    def test_stdout_only_phase_response_does_not_count_as_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".councli" / "runs" / "missing-output"
            script = (
                "import json\n"
                "print(json.dumps({'preferred_plan': 'plan:alpha:1', 'preferred_executor': 'alpha', 'confidence': 1.0, 'blocking_concerns': [], 'reason': 'stdout only'}))\n"
            )
            runners = {
                "alpha": AgentRunner(
                    "alpha",
                    AgentConfig(
                        backend="exec",
                        binary=PYTHON,
                        command=[PYTHON, "-c", script, "{prompt}"],
                    ),
                )
            }

            result = run_blackboard_council(
                task="stdout must not be accepted",
                runners=runners,
                root=root,
                run_dir=run_dir,
                dry_run=False,
            )
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))

            self.assertFalse(result.decision["approved"])
            self.assertEqual(state["phases"]["orient"]["alpha"]["status"], "failed")
            self.assertIn("missing required output file", state["phases"]["orient"]["alpha"]["error"])
            self.assertTrue(state["votes"]["alpha"]["abstained"])
            self.assertIn("missing required output file", state["votes"]["alpha"]["error"])

    def test_worktree_diff_includes_committed_executor_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            worktree = create_worktree(root, run_name="committed-change", executor="alpha")
            (worktree.path / "README.md").write_text("initial\ncommitted by executor\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=worktree.path, check=True)
            subprocess.run(
                ["git", "commit", "-m", "executor committed change"],
                cwd=worktree.path,
                text=True,
                capture_output=True,
                check=True,
            )

            diff_text = diff(worktree.path, base_ref=worktree.base_ref)

            self.assertIn("committed by executor", diff_text)
            self.assertIn("README.md", diff_text)

    def test_cli_status_and_show_latest_expose_resume_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc, latest = self.run_fake_councli(root)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            status = subprocess.run(
                [PYTHON, "-m", "councli", "status", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            shown = subprocess.run(
                [PYTHON, "-m", "councli", "show", "latest", "-C", str(root), "--blackboard"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            self.assertEqual(shown.returncode, 0, shown.stderr + shown.stdout)
            self.assertIn(latest.name, status.stdout)
            self.assertIn("add implemented file", status.stdout)
            self.assertIn(f"Run: {latest.name}", shown.stdout)
            self.assertIn("Blackboard:", shown.stdout)
            self.assertIn("## Task", shown.stdout)

    def test_cli_apply_dry_run_and_apply_accepted_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            subprocess.run(["git", "add", "fake_agent.py", "scenario.json"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add fake agent"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            )
            proc, latest = self.run_fake_councli(root)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            dry = subprocess.run(
                [PYTHON, "-m", "councli", "apply", "latest", "-C", str(root), "--dry-run"],
                cwd=REPO_ROOT,
                env=self.experimental_env(),
                text=True,
                capture_output=True,
                check=False,
            )
            applied = subprocess.run(
                [PYTHON, "-m", "councli", "apply", "latest", "-C", str(root)],
                cwd=REPO_ROOT,
                env=self.experimental_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
            self.assertIn("Patch applies cleanly", dry.stdout)
            self.assertEqual(applied.returncode, 0, applied.stderr + applied.stdout)
            self.assertIn("Applied", applied.stdout)
            self.assertIn("implemented by alpha", (root / "README.md").read_text(encoding="utf-8"))
            self.assertEqual((root / "implemented.txt").read_text(encoding="utf-8"), "implemented by alpha\n")

            state = json.loads((latest / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["implementation"]["applied"]["root"], str(root))

    def test_cli_apply_rejects_invalid_decision_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            subprocess.run(["git", "add", "fake_agent.py", "scenario.json"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add fake agent"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            )
            proc, latest = self.run_fake_councli(root)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            state_path = latest / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["decision"].pop("schema_version", None)
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            rejected = subprocess.run(
                [PYTHON, "-m", "councli", "apply", "latest", "-C", str(root), "--dry-run"],
                cwd=REPO_ROOT,
                env=self.experimental_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
            self.assertIn("invalid decision metadata", rejected.stdout + rejected.stderr)
            self.assertIn("council decision has invalid schema_version", rejected.stdout + rejected.stderr)

    def test_cli_chat_runs_interactive_dry_council_and_local_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "chat", "-C", str(root), "--dry-run"],
                cwd=REPO_ROOT,
                input="/status\n/not-a-command\nmake a tiny plan\n//literal slash task\n/show latest\n/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("councli interactive", proc.stdout)
            self.assertIn("Unknown councli command", proc.stdout)
            self.assertIn("Responses", proc.stdout)
            self.assertIn("Councli", proc.stdout)
            self.assertIn("Run:", proc.stdout)
            self.assertIn("/literal slash task", proc.stdout)
            tasks = [
                json.loads((path / "state.json").read_text(encoding="utf-8"))["task"]
                for path in sorted((root / ".councli" / "runs").iterdir())
                if path.is_dir()
            ]
            self.assertIn("make a tiny plan", tasks)
            self.assertIn("/literal slash task", tasks)

    def test_bare_cli_opens_control_plane_and_streams_visible_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "-C", str(root)],
                cwd=REPO_ROOT,
                input="make a visible plan\n/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("councli interactive", proc.stdout)
            self.assertIn("[ board ]", proc.stdout)
            self.assertIn("Task: make a visible plan", proc.stdout)
            self.assertIn("Asking:", proc.stdout)
            self.assertIn("chat.round1:alpha start", proc.stdout)
            self.assertIn("synthesis:alpha start", proc.stdout)
            self.assertIn("Responses", proc.stdout)
            self.assertIn("Councli", proc.stdout)

    def test_council_without_task_falls_back_to_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "council", "-C", str(root)],
                cwd=REPO_ROOT,
                input="/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("No council task supplied", proc.stdout)
            self.assertIn("councli interactive", proc.stdout)

    def test_public_help_hides_deferred_workflow_and_session_helpers(self) -> None:
        main_help = subprocess.run(
            [PYTHON, "-m", "councli", "--help"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        sessions_help = subprocess.run(
            [PYTHON, "-m", "councli", "sessions", "--help"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(main_help.returncode, 0, main_help.stdout + main_help.stderr)
        self.assertEqual(sessions_help.returncode, 0, sessions_help.stdout + sessions_help.stderr)
        self.assertIn("council", main_help.stdout)
        self.assertIsNone(re.search(r"│\s+reason\s+", main_help.stdout))
        self.assertIsNone(re.search(r"│\s+apply\s+", main_help.stdout))
        self.assertIsNone(re.search(r"│\s+run\s+", main_help.stdout))
        self.assertIn("attach", sessions_help.stdout)
        for hidden in ("import", "resume", "send", "ask", "relay"):
            self.assertNotIn(hidden, sessions_help.stdout)

    def test_hidden_prototypes_require_experimental_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            for args in (
                ["run", "noop", "-C", str(root), "--allow-dirty"],
                ["apply", "latest", "-C", str(root), "--dry-run"],
                ["sessions", "import", "alpha", "-C", str(root)],
            ):
                proc = subprocess.run(
                    [PYTHON, "-m", "councli", *args],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("COUNCLI_EXPERIMENTAL=1", proc.stdout + proc.stderr)

    def test_deliberate_slash_command_uses_shared_turn_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "chat", "-C", str(root), "--dry-run"],
                cwd=REPO_ROOT,
                input="/deliberate pick a simple architecture\n/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("Intent: deliberate", proc.stdout)
            self.assertIn("Round 1", proc.stdout)
            self.assertIn("Round 2", proc.stdout)
            self.assertNotIn("ORIENT", proc.stdout)

    def test_shared_turn_writes_packets_sidecars_and_peer_visible_round_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "chat", "-C", str(root)],
                cwd=REPO_ROOT,
                input="/deliberate compare adapter designs\n/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            run_dir = [path for path in sorted((root / ".councli" / "runs").iterdir()) if path.name.endswith("-deliberate")][-1]
            request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
            participants = json.loads((run_dir / "participants.json").read_text(encoding="utf-8"))
            events = read_events(run_dir)
            self.assertEqual(request["schema_version"], "councli.request.v1")
            self.assertEqual(request["kind"], "turn.request")
            self.assertEqual(request["intent"], "deliberate")
            self.assertEqual(participants["schema_version"], "councli.participants.v1")
            self.assertEqual(participants["kind"], "participant.snapshot")
            self.assertEqual(events[0]["schema_version"], "councli.event.v1")
            blackboard = (run_dir / "blackboard.md").read_text(encoding="utf-8")
            self.assertIn("## Deliberate Round 1", blackboard)
            self.assertIn("## Deliberate Round 2", blackboard)
            self.assertIn("## Synthesis", blackboard)
            self.assertNotIn("## Orient", blackboard)

            alpha_sidecar = json.loads((run_dir / "shared" / "deliberate.round1" / "alpha.response.json").read_text(encoding="utf-8"))
            self.assertEqual(alpha_sidecar["schema_version"], "councli.response.v1")
            self.assertEqual(alpha_sidecar["participant"], "alpha")
            self.assertEqual(alpha_sidecar["intent"], "deliberate")
            self.assertIn("COUNCLI_PACKET_FILE=", "\n".join(alpha_sidecar["command"]))
            self.assertLess(len("\n".join(alpha_sidecar["command"])), 1000)

            packet_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted((run_dir / "packets" / "alpha").glob("*.md"))
                if "Round: 2" in path.read_text(encoding="utf-8")
            )
            self.assertIn("alpha shared deliberate response round 1", packet_text)
            self.assertIn("beta shared deliberate response round 1", packet_text)
            synthesis = json.loads((run_dir / "synthesis" / "synthesis.response.json").read_text(encoding="utf-8"))
            self.assertEqual(synthesis["kind"], "synthesis.response")
            self.assertEqual(synthesis["source_participants"], ["alpha", "beta"])

            verified = subprocess.run(
                [PYTHON, "-m", "councli", "verify", run_dir.name, "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            self.assertIn("Verify ok", verified.stdout)

            (run_dir / "state.json").unlink()
            (run_dir / "blackboard.md").unlink()
            recovered = subprocess.run(
                [PYTHON, "-m", "councli", "recover", run_dir.name, "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertIn("Recover ok", recovered.stdout)
            self.assertTrue((run_dir / "state.json").exists())
            self.assertTrue((run_dir / "blackboard.md").exists())
            reverified = subprocess.run(
                [PYTHON, "-m", "councli", "verify", run_dir.name, "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(reverified.returncode, 0, reverified.stdout + reverified.stderr)

            (run_dir / "shared" / "deliberate.round1" / "alpha.response.json").unlink()
            corrupted = subprocess.run(
                [PYTHON, "-m", "councli", "verify", run_dir.name, "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(corrupted.returncode, 2, corrupted.stdout + corrupted.stderr)
            report = json.loads(corrupted.stdout)
            self.assertFalse(report["ok"])
            self.assertTrue(any("sidecar missing" in error or "ref sidecar missing" in error for error in report["errors"]))

    def test_verify_rejects_response_sidecar_schema_type_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "chat", "-C", str(root)],
                cwd=REPO_ROOT,
                input="hello\n/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            run_dir = [path for path in sorted((root / ".councli" / "runs").iterdir()) if path.name.endswith("-chat")][-1]
            sidecar_path = run_dir / "shared" / "chat.round1" / "alpha.response.json"
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar["round"] = "one"
            sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            verified = subprocess.run(
                [PYTHON, "-m", "councli", "verify", run_dir.name, "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 2, verified.stdout + verified.stderr)
            report = json.loads(verified.stdout)
            self.assertFalse(report["ok"])
            self.assertTrue(any("violates response.schema.json" in error and "$.round" in error for error in report["errors"]))

    def test_recover_rebuilds_from_valid_event_prefix_when_log_tail_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "chat", "-C", str(root)],
                cwd=REPO_ROOT,
                input="hello\n/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            run_dir = [path for path in sorted((root / ".councli" / "runs").iterdir()) if path.name.endswith("-chat")][-1]
            (run_dir / "events.jsonl").write_text(
                (run_dir / "events.jsonl").read_text(encoding="utf-8") + '{"truncated":',
                encoding="utf-8",
            )
            (run_dir / "state.json").unlink()
            (run_dir / "blackboard.md").unlink()

            recovered = subprocess.run(
                [PYTHON, "-m", "councli", "recover", run_dir.name, "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            report = json.loads(recovered.stdout)
            self.assertTrue(report["ok"])
            self.assertTrue(report["recovered"])
            self.assertIsNotNone(report["event_log_issue"])
            self.assertTrue(any("malformed tail" in warning for warning in report["warnings"]))
            recovery = json.loads((run_dir / "recovery" / "malformed-events.json").read_text(encoding="utf-8"))
            self.assertEqual(recovery["schema_version"], "councli.recovery.v1")
            self.assertEqual(recovery["kind"], "event_log.malformed_tail")
            self.assertTrue((run_dir / "state.json").exists())
            self.assertTrue((run_dir / "blackboard.md").exists())

    def test_chat_degrades_repeated_auth_failures_for_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(
                tmp,
                scenario={"shared_failures": {"beta": "authentication required"}},
            )
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "chat", "-C", str(root)],
                cwd=REPO_ROOT,
                input="first question\nsecond question\n/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            runs = [path for path in sorted((root / ".councli" / "runs").iterdir()) if "-chat" in path.name]
            self.assertEqual(len(runs), 2)
            first_beta = json.loads((runs[0] / "shared" / "chat.round1" / "beta.response.json").read_text(encoding="utf-8"))
            second_beta = json.loads((runs[1] / "shared" / "chat.round1" / "beta.response.json").read_text(encoding="utf-8"))
            self.assertEqual(first_beta["failure_class"], "auth_required")
            self.assertEqual(second_beta["status"], "skipped")
            self.assertIn("degraded for this session", (runs[1] / "blackboard.md").read_text(encoding="utf-8"))

    def test_parse_turn_trailer_strips_metadata(self) -> None:
        body, trailer = parse_turn_trailer(
            "Body answer\n\nCOUNCLI_TRAILER\ncontinue: true\nrecommend: vote\nsummary: key point\nvote: sqlite\nconfidence: 0.8\n"
        )

        self.assertEqual(body, "Body answer")
        self.assertTrue(trailer["continue"])
        self.assertEqual(trailer["recommend"], "vote")
        self.assertEqual(trailer["summary"], "key point")
        self.assertEqual(trailer["vote"], "sqlite")
        self.assertEqual(trailer["confidence"], 0.8)

    def test_render_peer_context_applies_explicit_context_budget(self) -> None:
        def ok_result(name: str, body: str) -> dict[str, AgentRunResult]:
            return {
                "result": AgentRunResult(
                    name=name,
                    ok=True,
                    skipped=False,
                    exit_code=0,
                    output=body,
                    error="",
                    command=["fake"],
                )
            }

        rounds = [
            {"alpha": ok_result("alpha", "round-one-tail")},
            {
                "alpha": ok_result("alpha", "a" * 80),
                "beta": ok_result("beta", "b" * 80),
            },
            {
                "alpha": ok_result("alpha", "c" * 80),
                "beta": ok_result("beta", "d" * 80),
            },
        ]

        context = render_peer_context(
            rounds,
            latest_rounds=2,
            per_participant_limit=40,
            total_limit=260,
            overflow_ref="/tmp/run/blackboard.md",
        )

        self.assertNotIn("Round 1", context)
        self.assertIn("## Round 2", context)
        self.assertIn("[truncated by councli after 40 characters]", context)
        self.assertIn("context truncated by councli after 260 characters", context)
        self.assertIn("/tmp/run/blackboard.md", context)
        self.assertNotIn("round-one-tail", context)

    def test_render_peer_context_can_summarize_or_omit_failures(self) -> None:
        failed = AgentRunResult(
            name="beta",
            ok=False,
            skipped=False,
            exit_code=1,
            output="",
            error="very long authentication details",
            command=["fake"],
            failure_class="auth_required",
        )
        rounds = [{"beta": {"result": failed}}]

        summary_context = render_peer_context(rounds, include_failures="summary")
        self.assertIn("auth_required: very long authentication details", summary_context)

        omitted_context = render_peer_context(rounds, include_failures="omit")
        self.assertNotIn("beta", omitted_context)
        self.assertNotIn("authentication", omitted_context)

    def test_vote_decision_rejects_invalid_sidecar_even_with_trailer_vote(self) -> None:
        result = AgentRunResult(
            name="alpha",
            ok=True,
            skipped=False,
            exit_code=0,
            output="answer",
            error="",
            command=["fake"],
        )
        sidecar = {
            "schema_version": "councli.response.v1",
            "kind": "participant.response",
            "participant": "alpha",
            "intent": "vote",
            "status": "ok",
            "vote": {"value": "", "confidence": 0.9, "valid": False},
        }

        decision = decide_shared_vote(
            {
                "alpha": {
                    "result": result,
                    "trailer": {"vote": "sqlite", "confidence": 0.9},
                    "sidecar": sidecar,
                }
            },
            ["alpha"],
        )

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["schema_version"], "councli.decision.v1")
        self.assertEqual(decision["kind"], "vote.decision")
        self.assertEqual(decision["votes"], {})
        self.assertEqual(decision["abstentions"], {"alpha": "sidecar vote is not valid"})

    def test_cli_chat_supports_dry_attach_and_broadcast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "chat", "-C", str(root), "--dry-run"],
                cwd=REPO_ROOT,
                input="/assistant alpha\n/broadcast compare options\n/quit\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("would attach to alpha", proc.stdout)
            self.assertIn("Broadcast:", proc.stdout)
            self.assertIn("broadcast:alpha start", proc.stdout)
            runs = sorted((root / ".councli" / "runs").iterdir())
            self.assertTrue(any(path.name.endswith("-broadcast") for path in runs))
            latest = [path for path in runs if path.name.endswith("-broadcast")][-1]
            self.assertTrue((latest / "brief.md").exists())
            self.assertTrue((latest / "broadcast" / "alpha.md").exists())
            state = json.loads((latest / "state.json").read_text(encoding="utf-8"))
            payload = state["phases"]["broadcast"]["alpha"]["refs"]
            self.assertIn("content", payload)

    def test_exec_backend_with_start_command_supports_native_session(self) -> None:
        runner = AgentRunner(
            "alpha",
            AgentConfig(
                backend="exec",
                binary=PYTHON,
                command=[PYTHON, "-c", "print('headless')", "{prompt}"],
                start_command=[PYTHON, "-c", "print('native')"],
            ),
        )

        self.assertTrue(supports_native_session(runner))
        native_runner = native_session_runner(runner)
        self.assertEqual(native_runner.config.backend, "tmux")
        self.assertEqual(native_runner.config.command, [PYTHON, "-c", "print('native')"])

    def test_kimi_headless_default_does_not_combine_prompt_with_yolo_or_yolo_start(self) -> None:
        kimi = DEFAULT_CONFIG.agents["kimi"]

        self.assertEqual(kimi.command, ["kimi", "--prompt", "{prompt}"])
        self.assertEqual(kimi.start_command, ["kimi"])

    def test_default_agents_include_safe_readiness_probes(self) -> None:
        self.assertEqual(DEFAULT_CONFIG.agents["codex"].readiness_command, ["codex", "doctor"])
        self.assertEqual(DEFAULT_CONFIG.agents["claude"].readiness_command, ["claude", "auth", "status"])
        self.assertEqual(DEFAULT_CONFIG.agents["agy"].readiness_command, ["agy", "models"])
        self.assertEqual(DEFAULT_CONFIG.agents["codewhale"].readiness_command, ["codewhale", "doctor"])
        self.assertEqual(DEFAULT_CONFIG.agents["kimi"].readiness_command, ["kimi", "doctor"])

    def test_task_brief_records_native_session_registry(self) -> None:
        from councli.native import reconcile_session_registry, upsert_session, write_task_brief

        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            upsert_session(
                root,
                agent="codex",
                session_name="councli-codex",
                backend="tmux",
                cwd=root,
                command=["codex"],
                raw_capture=root / ".councli" / "session-recordings" / "codex.raw.log",
            )

            brief = write_task_brief(root, task="review the design", task_id="task-1")

            text = brief.path.read_text(encoding="utf-8")
            self.assertIn("review the design", text)
            self.assertIn("codex: session=councli-codex", text)

            registry = reconcile_session_registry(root, {})
            self.assertEqual(registry["sessions"]["codex"]["status"], "stale")

    def test_project_scoped_session_names_are_stable_and_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "a"
            root_b = Path(tmp) / "b"
            root_a.mkdir()
            root_b.mkdir()
            runner = AgentRunner("codex", AgentConfig(backend="tmux", binary=PYTHON, command=[PYTHON]))

            first = runner.session_name_for(root_a)
            second = runner.session_name_for(root_b)
            alt = runner.session_name_for(root_a, instance="alt")

            self.assertNotEqual(first, second)
            self.assertNotEqual(first, alt)
            self.assertTrue(first.startswith("councli-"))

    def test_cli_brief_command_creates_manual_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "brief", "manual architecture note", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("Brief:", proc.stdout)
            briefs = list((root / ".councli" / "tasks").glob("manual-*/brief.md"))
            self.assertEqual(len(briefs), 1)
            self.assertIn("manual architecture note", briefs[0].read_text(encoding="utf-8"))

    def test_sessions_import_preserves_native_session_id(self) -> None:
        from councli.native import upsert_session

        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            upsert_session(
                root,
                agent="alpha",
                session_name="councli-alpha",
                backend="exec",
                cwd=root,
                command=[PYTHON],
                native_session_id="native-123",
            )
            upsert_session(
                root,
                agent="alpha",
                session_name="councli-alpha",
                backend="exec",
                cwd=root,
                command=[PYTHON],
            )

            registry = json.loads((root / ".councli" / "sessions" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["sessions"]["alpha"]["native_session_id"], "native-123")

    def test_project_config_executable_changes_require_retrust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["command"] = [PYTHON, "-c", "print('changed')", "{prompt}"]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            blocked = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("trusted agent fields changed", blocked.stdout + blocked.stderr)

            trusted = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(trusted.returncode, 0, trusted.stdout + trusted.stderr)

            doctor = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)

            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["submit_keys"] = ["Enter", "Enter"]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            blocked_transport = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(blocked_transport.returncode, 0)
            self.assertIn("trusted agent fields changed", blocked_transport.stdout + blocked_transport.stderr)

    def test_prompt_placeholder_must_be_standalone_argv_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["command"] = [PYTHON, "-c", "print('x')", "--prompt={prompt}"]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            trusted = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(trusted.returncode, 0)
            self.assertIn("{prompt} must be a standalone argv token", trusted.stdout + trusted.stderr)

    def test_binary_path_drift_requires_retrust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            bin_a = Path(tmp) / "bin-a"
            bin_b = Path(tmp) / "bin-b"
            bin_a.mkdir()
            bin_b.mkdir()
            for index, directory in enumerate((bin_a, bin_b), start=1):
                tool = directory / "fake-agent"
                tool.write_text(f"#!/bin/sh\necho fake agent {index}\n", encoding="utf-8")
                tool.chmod(0o755)

            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["binary"] = "fake-agent"
            raw["agents"]["alpha"]["command"] = ["fake-agent", "{prompt}"]
            raw["agents"]["alpha"]["broadcast_command"] = ["fake-agent", "{prompt}"]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            env_a = os.environ.copy()
            env_a["PATH"] = f"{bin_a}{os.pathsep}{env_a.get('PATH', '')}"
            trusted_a = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env_a,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(trusted_a.returncode, 0, trusted_a.stdout + trusted_a.stderr)

            env_b = os.environ.copy()
            env_b["PATH"] = f"{bin_b}{os.pathsep}{env_b.get('PATH', '')}"
            blocked = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env_b,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("Trusted assistant binary path changed", blocked.stdout + blocked.stderr)
            self.assertIn("alpha", blocked.stdout + blocked.stderr)

            trusted_b = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env_b,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(trusted_b.returncode, 0, trusted_b.stdout + trusted_b.stderr)

            doctor = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env_b,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)

    def test_binary_hash_drift_requires_retrust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tool = bin_dir / "fake-agent"
            tool.write_text("#!/bin/sh\necho fake agent before\n", encoding="utf-8")
            tool.chmod(0o755)

            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["binary"] = "fake-agent"
            raw["agents"]["alpha"]["command"] = ["fake-agent", "{prompt}"]
            raw["agents"]["alpha"]["broadcast_command"] = ["fake-agent", "{prompt}"]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
            trusted = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(trusted.returncode, 0, trusted.stdout + trusted.stderr)

            tool.write_text("#!/bin/sh\necho fake agent after\n", encoding="utf-8")
            tool.chmod(0o755)

            blocked = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("Trusted assistant binary content changed", blocked.stdout + blocked.stderr)
            self.assertIn("alpha", blocked.stdout + blocked.stderr)

            retrusted = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(retrusted.returncode, 0, retrusted.stdout + retrusted.stderr)

    def test_trust_pin_records_binary_version_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["version_command"] = [PYTHON, "--version"]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            trust_path, _ = trust_project_config(root, reason="test", repair_identity=True)

            trust = json.loads(trust_path.read_text(encoding="utf-8"))
            alpha = trust["config"]["binaries"]["alpha"]
            self.assertEqual(alpha["version_status"], "ok")
            self.assertIn("Python", alpha["version"])
            self.assertEqual(alpha["version_command"], [PYTHON, "--version"])

    def test_security_json_reports_trust_and_versions_without_running_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["version_command"] = [PYTHON, "--version"]
            raw["agents"]["alpha"]["start_capabilities"] = [
                "reads_workspace",
                "writes_workspace",
                "runs_tools",
                "full_permission",
            ]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            trust_project_config(root, reason="test", repair_identity=True)

            proc = subprocess.run(
                [PYTHON, "-m", "councli", "security", "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["trust"]["status"], "trusted")
            self.assertTrue(data["trust"]["config_trusted"])
            alpha = next(agent for agent in data["agents"] if agent["agent"] == "alpha")
            self.assertEqual(alpha["trust_status"], "ok")
            self.assertEqual(alpha["current"]["version_status"], "ok")
            self.assertIn("Python", alpha["current"]["version"])
            self.assertIn("native_start", alpha["elevated_surfaces"])

    def test_security_json_reports_binary_drift_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            bin_one = Path(tmp) / "bin-one"
            bin_two = Path(tmp) / "bin-two"
            bin_one.mkdir()
            bin_two.mkdir()
            for directory, version in ((bin_one, "one"), (bin_two, "two")):
                tool = directory / "fake-agent"
                tool.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = \"--version\" ]; then echo fake-agent "
                    + version
                    + "; exit 0; fi\n"
                    "echo fake agent "
                    + version
                    + "\n",
                    encoding="utf-8",
                )
                tool.chmod(0o755)

            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["binary"] = "fake-agent"
            raw["agents"]["alpha"]["command"] = ["fake-agent", "{prompt}"]
            raw["agents"]["alpha"]["broadcast_command"] = ["fake-agent", "{prompt}"]
            raw["agents"]["alpha"]["version_command"] = ["fake-agent", "--version"]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            trust_env = os.environ.copy()
            trust_env["PATH"] = f"{bin_one}{os.pathsep}{trust_env.get('PATH', '')}"
            trusted = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                env=trust_env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(trusted.returncode, 0, trusted.stdout + trusted.stderr)

            drift_env = os.environ.copy()
            drift_env["PATH"] = f"{bin_two}{os.pathsep}{drift_env.get('PATH', '')}"
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "security", "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                env=drift_env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["trust"]["status"], "trusted_with_binary_drift")
            self.assertIn("alpha", data["trust"]["binary_drift_agents"])
            alpha = next(agent for agent in data["agents"] if agent["agent"] == "alpha")
            self.assertEqual(alpha["trust_status"], "path_drift")
            self.assertIn("fake-agent two", alpha["current"]["version"])
            self.assertIn("fake-agent one", alpha["trusted"]["version"])

    def test_doctor_bootstraps_default_config_for_fresh_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.set_state_home(Path(tmp) / "state")
            root = Path(tmp) / "repo"
            root.mkdir()

            proc = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(project_config_path(root).exists())
            self.assertIn("Created default councli config", proc.stdout + proc.stderr)
            self.assertIn("councli doctor", proc.stdout + proc.stderr)

    def test_doctor_json_reports_intent_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            alpha = next(agent for agent in data["agents"] if agent["agent"] == "alpha")
            self.assertTrue(alpha["intents"]["chat"]["ready"])
            self.assertEqual(alpha["intents"]["chat"]["status"], "ready")
            self.assertTrue(alpha["intents"]["deliberate"]["ready"])
            self.assertTrue(alpha["intents"]["vote"]["ready"])

    def test_doctor_can_include_security_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            json_proc = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root), "--json", "--security"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(json_proc.returncode, 0, json_proc.stdout + json_proc.stderr)
            data = json.loads(json_proc.stdout)
            self.assertEqual(data["security"]["trust"]["status"], "trusted")
            self.assertIn("config", data["security"])
            self.assertIn("agents", data["security"])
            self.assertIn("agents", data)

            human_proc = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root), "--security"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(human_proc.returncode, 0, human_proc.stdout + human_proc.stderr)
            self.assertIn("councli doctor", human_proc.stdout)
            self.assertIn("councli security", human_proc.stdout)

    def test_doctor_json_reports_version_and_capability_gated_intents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["display_name"] = "Alpha Test Agent"
            raw["agents"]["alpha"]["capabilities"] = ["chat"]
            raw["agents"]["alpha"]["version_command"] = [PYTHON, "--version"]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            trust_project_config(root, reason="test", repair_identity=True)

            proc = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            alpha = next(agent for agent in data["agents"] if agent["agent"] == "alpha")
            self.assertEqual(alpha["display_name"], "Alpha Test Agent")
            self.assertEqual(alpha["version_status"], "ok")
            self.assertIn("Python", alpha["version"])
            self.assertEqual(alpha["readiness_status"], "not_configured")
            self.assertTrue(alpha["intents"]["chat"]["ready"])
            self.assertFalse(alpha["intents"]["vote"]["ready"])
            self.assertEqual(alpha["intents"]["vote"]["status"], "unsupported_intent")
            self.assertIn("broadcast", alpha["command_capabilities"])

    def test_doctor_json_reports_broadcast_policy_denial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"].pop("broadcast_command", None)
            raw["agents"]["alpha"]["command_capabilities"] = [
                "reads_workspace",
                "writes_workspace",
                "runs_tools",
                "full_permission",
            ]
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            trust_project_config(root, reason="test", repair_identity=True)

            proc = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            alpha = next(agent for agent in data["agents"] if agent["agent"] == "alpha")
            self.assertEqual(alpha["read_only_policy"], "safe_only")
            self.assertFalse(alpha["intents"]["chat"]["ready"])
            self.assertEqual(alpha["intents"]["chat"]["status"], "policy_denied")
            self.assertFalse(alpha["intents"]["broadcast"]["ready"])
            self.assertEqual(alpha["intents"]["broadcast"]["status"], "policy_denied")
            self.assertIn("full_permission", alpha["command_capabilities"]["broadcast"])

    def test_doctor_json_reports_configured_readiness_probe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["agents"]["alpha"]["readiness_command"] = [
                PYTHON,
                "-c",
                "import sys; sys.stderr.write('No model configured\\n'); sys.exit(7)",
            ]
            raw["agents"]["alpha"]["readiness_timeout_seconds"] = 5
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            trust_project_config(root, reason="test", repair_identity=True)

            proc = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            alpha = next(agent for agent in data["agents"] if agent["agent"] == "alpha")
            self.assertFalse(alpha["available"])
            self.assertEqual(alpha["readiness_status"], "model_unconfigured")
            self.assertIn("No model configured", alpha["readiness_detail"])
            self.assertFalse(alpha["intents"]["chat"]["ready"])
            self.assertEqual(alpha["intents"]["chat"]["status"], "model_unconfigured")

    def test_artifacts_scrub_and_prune_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            secret = "sk-proj-" + ("A" * 28)
            github_token = "ghp_" + ("B" * 36)
            raw_log = root / ".councli" / "session-recordings" / "alpha.raw.log"
            raw_log.parent.mkdir(parents=True)
            raw_log.write_text(f"token={secret}\n", encoding="utf-8")
            raw_log.chmod(0o600)
            run_dir = root / ".councli" / "runs" / "old-run"
            run_dir.mkdir(parents=True)
            (run_dir / "blackboard.md").write_text(f"github {github_token}\n", encoding="utf-8")

            dry_scrub = subprocess.run(
                [PYTHON, "-m", "councli", "artifacts", "scrub", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(dry_scrub.returncode, 0, dry_scrub.stdout + dry_scrub.stderr)
            self.assertIn("Would redact", dry_scrub.stdout)
            self.assertIn(secret, raw_log.read_text(encoding="utf-8"))

            write_scrub = subprocess.run(
                [PYTHON, "-m", "councli", "artifacts", "scrub", "-C", str(root), "--write"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(write_scrub.returncode, 0, write_scrub.stdout + write_scrub.stderr)
            self.assertIn("Redacted", write_scrub.stdout)
            self.assertNotIn(secret, raw_log.read_text(encoding="utf-8"))
            self.assertNotIn(github_token, (run_dir / "blackboard.md").read_text(encoding="utf-8"))
            self.assertEqual(raw_log.stat().st_mode & 0o077, 0)

            archive = root / ".councli" / "session-archives" / "old" / "alpha.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text("old archive\n", encoding="utf-8")
            old_time = time.time() - 40 * 24 * 60 * 60
            os.utime(archive, (old_time, old_time))

            dry_prune = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "councli",
                    "artifacts",
                    "prune",
                    "-C",
                    str(root),
                    "--older-than",
                    "30",
                    "--class",
                    "session-archive",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(dry_prune.returncode, 0, dry_prune.stdout + dry_prune.stderr)
            self.assertIn("Dry run", dry_prune.stdout)
            self.assertTrue(archive.exists())

            delete_prune = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "councli",
                    "artifacts",
                    "prune",
                    "-C",
                    str(root),
                    "--older-than",
                    "30",
                    "--class",
                    "session-archive",
                    "--delete",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(delete_prune.returncode, 0, delete_prune.stdout + delete_prune.stderr)
            self.assertIn("Deleted", delete_prune.stdout)
            self.assertFalse(archive.exists())

    def test_artifacts_export_creates_redacted_support_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            secret = "sk-proj-" + ("C" * 28)
            raw_log = root / ".councli" / "session-recordings" / "alpha.raw.log"
            raw_log.parent.mkdir(parents=True)
            raw_log.write_text(f"raw token={secret}\n", encoding="utf-8")
            run_dir = root / ".councli" / "runs" / "export-run"
            run_dir.mkdir(parents=True)
            (run_dir / "blackboard.md").write_text(f"safe line\nsecret={secret}\n", encoding="utf-8")
            output = Path(tmp) / "support.tar.gz"

            proc = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "councli",
                    "artifacts",
                    "export",
                    "-C",
                    str(root),
                    "--output",
                    str(output),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(output.exists())
            with tarfile.open(output, "r:gz") as archive:
                names = archive.getnames()
                self.assertIn("councli-artifacts/manifest.json", names)
                self.assertIn("councli-artifacts/runs/export-run/blackboard.md", names)
                self.assertFalse(any(name.startswith("councli-artifacts/session-recordings/") for name in names))
                manifest_file = archive.extractfile("councli-artifacts/manifest.json")
                blackboard_file = archive.extractfile("councli-artifacts/runs/export-run/blackboard.md")
                self.assertIsNotNone(manifest_file)
                self.assertIsNotNone(blackboard_file)
                manifest = json.loads(manifest_file.read().decode("utf-8"))
                blackboard = blackboard_file.read().decode("utf-8")
                all_payload = b"".join(
                    archive.extractfile(name).read()
                    for name in names
                    if archive.getmember(name).isfile()
                )

            self.assertEqual(manifest["schema_version"], "councli.artifacts.export.v1")
            self.assertTrue(manifest["redacted"])
            self.assertGreaterEqual(manifest["summary"]["redaction_matches"], 1)
            self.assertIn("[REDACTED]", blackboard)
            self.assertNotIn(secret.encode("utf-8"), all_payload)

    def test_doctor_json_is_pure_json_on_fresh_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.set_state_home(Path(tmp) / "state")
            root = Path(tmp) / "repo"
            root.mkdir()
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["config"], str(project_config_path(root)))
            self.assertIn("agents", data)
            self.assertNotIn("Created default councli config", proc.stdout)

    def test_init_disable_missing_disables_absent_default_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.set_state_home(Path(tmp) / "state")
            root = Path(tmp) / "repo"
            bin_dir = Path(tmp) / "empty-bin"
            root.mkdir()
            bin_dir.mkdir()
            env = os.environ.copy()
            env["COUNCLI_STATE_HOME"] = str(Path(tmp) / "state")
            env["PATH"] = str(bin_dir)

            proc = subprocess.run(
                [PYTHON, "-m", "councli", "init", "--disable-missing", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            raw = yaml.safe_load(project_config_path(root).read_text(encoding="utf-8"))
            self.assertTrue(raw["agents"])
            self.assertTrue(all(agent["enabled"] is False for agent in raw["agents"].values()))
            self.assertIn("Detected assistant CLIs", proc.stdout + proc.stderr)

    def test_default_assistant_command_templates_run_with_fake_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.set_state_home(Path(tmp) / "state")
            root = Path(tmp) / "repo"
            bin_dir = Path(tmp) / "bin"
            log_path = Path(tmp) / "fake-agent-log.jsonl"
            root.mkdir()
            bin_dir.mkdir()
            script = fake_default_cli_script()
            for name in ("codex", "claude", "agy", "codewhale", "kimi"):
                path = bin_dir / name
                path.write_text(script, encoding="utf-8")
                path.chmod(0o755)
            env = os.environ.copy()
            env["COUNCLI_STATE_HOME"] = str(Path(tmp) / "state")
            env["COUNCLI_FAKE_AGENT_LOG"] = str(log_path)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

            init_proc = subprocess.run(
                [PYTHON, "-m", "councli", "init", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(init_proc.returncode, 0, init_proc.stdout + init_proc.stderr)

            chat_proc = subprocess.run(
                [PYTHON, "-m", "councli", "chat", "-C", str(root)],
                cwd=REPO_ROOT,
                env=env,
                input="what can you do\n/quit\n",
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(chat_proc.returncode, 0, chat_proc.stdout + chat_proc.stderr)
            stdout = chat_proc.stdout + chat_proc.stderr
            self.assertIn("councli interactive", stdout)
            self.assertIn("[ board ]", stdout)
            for name in ("codex", "claude", "agy", "codewhale", "kimi"):
                self.assertIn(f"chat.round1:{name} start", stdout)
                self.assertIn(f"chat.round1:{name} done", stdout)
            self.assertIn("synthesis:codex done", stdout)
            self.assertIn("Councli", stdout)

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            prompt_records = [record for record in records if record["kind"] == "prompt"]
            prompted_tools = {record["tool"] for record in prompt_records if record.get("prompt_kind") == "packet"}
            self.assertEqual(prompted_tools, {"codex", "claude", "agy", "codewhale", "kimi"})
            self.assertTrue(any(record["tool"] == "codex" and record.get("prompt_kind") == "synthesis" for record in prompt_records))
            argv_by_tool = {record["tool"]: record["args"] for record in prompt_records if record.get("prompt_kind") == "packet"}
            self.assertEqual(argv_by_tool["codex"][:3], ["exec", "--sandbox", "read-only"])
            self.assertEqual(argv_by_tool["claude"][:3], ["--permission-mode", "plan", "-p"])
            self.assertEqual(argv_by_tool["agy"][:2], ["--sandbox", "--print"])
            self.assertEqual(argv_by_tool["codewhale"][0], "exec")
            self.assertEqual(argv_by_tool["kimi"][0], "--prompt")
            self.assertNotIn("--yolo", argv_by_tool["kimi"])

    def test_native_config_changes_require_retrust_and_validate_detach_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            config_path = project_config_path(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["native"] = {"detach_key": "C-a"}
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            blocked = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("trusted agent fields changed", blocked.stdout + blocked.stderr)

            trusted = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(trusted.returncode, 0, trusted.stdout + trusted.stderr)

            raw["native"] = {"detach_key": "#(echo pwned)"}
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            rejected = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "-C", str(root)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("simple tmux key chord", rejected.stdout + rejected.stderr)

    def test_project_identity_drift_requires_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            moved = Path(tmp) / "moved"
            shutil.copytree(root, moved)

            blocked = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(moved)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("different project path", blocked.stdout + blocked.stderr)

            repaired = subprocess.run(
                [PYTHON, "-m", "councli", "trust", "--repair-identity", "-C", str(moved)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)

            doctor = subprocess.run(
                [PYTHON, "-m", "councli", "doctor", "-C", str(moved)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)

    def test_sessions_import_lists_candidates_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self.prepare_fake_repo(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "councli", "sessions", "import", "alpha", "-C", str(root)],
                cwd=REPO_ROOT,
                env=self.experimental_env(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("No native session candidates found", proc.stdout + proc.stderr)

    def test_rawlog_rotates_and_uses_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recordings" / "alpha.raw.log"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "councli.rawlog",
                    "--path",
                    str(path),
                    "--max-bytes",
                    "10",
                    "--backups",
                    "2",
                ],
                input=b"0123456789abcdefghijKLMNOPQRST",
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="ignore"))
            self.assertTrue(path.exists())
            self.assertTrue(path.with_name("alpha.raw.log.1").exists())
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertEqual(path.with_name("alpha.raw.log.1").stat().st_mode & 0o077, 0)

    @unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
    def test_tmux_session_lifecycle_reconcile_capture_and_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.set_state_home(Path(tmp) / "state")
            root = Path(tmp) / "repo"
            root.mkdir()
            socket = f"councli-test-{os.getpid()}-{int(time.time() * 1000)}"
            config_dir = root / ".councli"
            config_dir.mkdir()
            (config_dir / "config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "agents": {
                            "alpha": {
                                "enabled": True,
                                "backend": "tmux",
                                "binary": "bash",
                                "command": ["bash", "--noprofile", "--norc"],
                                "start_command": ["bash", "--noprofile", "--norc"],
                                "resume_command": ["bash", "--noprofile", "--norc"],
                                "prompt_style": "compact",
                                "input_method": "paste",
                                "submit_keys": ["Enter"],
                                "timeout_seconds": 10,
                            }
                        },
                        "native": {
                            "tmux_socket": socket,
                            "detach_key": "C-]",
                            "raw_log_max_bytes": 1024,
                            "raw_log_backups": 1,
                            "session_prefix": "councli-test",
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            trust_project_config(root, reason="test", repair_identity=True)
            try:
                started = subprocess.run(
                    [PYTHON, "-m", "councli", "sessions", "start", "alpha", "-C", str(root)],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)

                sent = subprocess.run(
                    [
                        PYTHON,
                        "-m",
                        "councli",
                        "sessions",
                        "send",
                        "alpha",
                        "echo COUNCLI_SMOKE",
                        "-C",
                        str(root),
                        "--no-marker",
                    ],
                    cwd=REPO_ROOT,
                    env=self.experimental_env(),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(sent.returncode, 0, sent.stdout + sent.stderr)
                time.sleep(0.5)

                listed = subprocess.run(
                    [PYTHON, "-m", "councli", "sessions", "list", "-C", str(root)],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
                self.assertIn("Pane Cmd", listed.stdout)
                self.assertIn("running", listed.stdout)

                registry = json.loads((root / ".councli" / "sessions" / "registry.json").read_text(encoding="utf-8"))
                record = registry["sessions"]["alpha"]
                self.assertEqual(record["process_status"], "running")
                raw_capture = Path(record["raw_capture"])
                self.assertTrue(raw_capture.exists())
                self.assertEqual(raw_capture.stat().st_mode & 0o077, 0)

                imported = subprocess.run(
                    [
                        PYTHON,
                        "-m",
                        "councli",
                        "sessions",
                        "import",
                        "alpha",
                        "-C",
                        str(root),
                        "--session-id",
                        "native-123",
                    ],
                    cwd=REPO_ROOT,
                    env=self.experimental_env(),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)

                blocked_resume = subprocess.run(
                    [
                        PYTHON,
                        "-m",
                        "councli",
                        "sessions",
                        "resume",
                        "alpha",
                        "-C",
                        str(root),
                        "--no-attach",
                    ],
                    cwd=REPO_ROOT,
                    env=self.experimental_env(),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(blocked_resume.returncode, 0)
                self.assertIn("Refusing to resume over live session", blocked_resume.stdout + blocked_resume.stderr)

                stopped = subprocess.run(
                    [
                        PYTHON,
                        "-m",
                        "councli",
                        "sessions",
                        "stop",
                        "alpha",
                        "-C",
                        str(root),
                        "--no-archive",
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)

                ledger_path = root / ".councli" / "ledger" / "events.jsonl"
                deadline = time.time() + 3
                ledger_text = ""
                while time.time() < deadline:
                    if ledger_path.exists():
                        ledger_text = ledger_path.read_text(encoding="utf-8")
                        if "tmux.session-closed" in ledger_text:
                            break
                    time.sleep(0.1)
                self.assertIn("session.stopped", ledger_text)
                self.assertIn("tmux.session-closed", ledger_text)
            finally:
                subprocess.run(["tmux", "-L", socket, "kill-server"], text=True, capture_output=True, check=False)


if __name__ == "__main__":
    unittest.main()
