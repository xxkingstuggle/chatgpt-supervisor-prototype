import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skill" / "chatgpt-supervisor" / "scripts" / "supervisor.py"


def cli(project: Path, *args: str, check: bool = True):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--project", str(project)],
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise AssertionError("CLI failed: %s\n%s" % (result.stdout, result.stderr))
    return result


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        created = json.loads(cli(self.project, "init", "--goal", "验证一个隔离演示目标").stdout)
        self.task = created["task_id"]
        self.owner = created["owner_id"]

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name: str, content: str) -> Path:
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def report(self, text="首轮报告：目标、约束、测试失败证据和未验证项"):
        return json.loads(cli(self.project, "prepare-report", "--task", self.task, "--owner", self.owner, "--content-file", str(self.write("report.md", text))).stdout)

    def send(self):
        result = json.loads(cli(self.project, "prepare-send", "--task", self.task, "--owner", self.owner).stdout)
        request_text = Path(result["request_path"]).read_text(encoding="utf-8")
        self.assertIn(self.task, request_text)
        self.assertIn(result["request_id"], request_text)
        self.assertIn(result["report_sha256"], request_text)
        return result

    def confirm(self, request_id="req"):
        evidence = self.write("browser.txt", "real-browser observation\nrequest visibly present: %s\nno duplicate user message" % request_id)
        return json.loads(cli(self.project, "confirm-sent", "--task", self.task, "--owner", self.owner, "--conversation-url", "https://chatgpt.com/c/test-conversation", "--evidence-file", str(evidence)).stdout)

    def reply(self, status="CONTINUE", round_id=1, request_id=None, report_sha256=None, body="执行一个有界动作并回报证据。"):
        state = json.loads(cli(self.project, "status", "--task", self.task).stdout)
        request_id = request_id or state["current_request_id"]
        report_sha256 = report_sha256 or state["current_report_sha256"]
        return self.write(
            "reply.md",
            "```supervisor-meta\n%s\n```\n%s\nEND_SUPERVISOR_REPLY\n"
            % (
                json.dumps(
                    {
                        "protocol_version": 1,
                        "task_id": self.task,
                        "round_id": round_id,
                        "goal_revision": state["goal_revision"],
                        "request_id": request_id,
                        "report_sha256": report_sha256,
                        "status": status,
                    },
                    ensure_ascii=False,
                ),
                body,
            ),
        )

    def accept(self, reply_path=None, generation=True):
        reply_path = reply_path or self.reply()
        evidence = self.write("reply-page.txt", "real-browser observation\nfull assistant body visible\ncomposer no longer generating\nno error banner")
        args = ["accept-reply", "--task", self.task, "--owner", self.owner, "--reply-file", str(reply_path), "--evidence-file", str(evidence)]
        if generation:
            args.append("--generation-complete")
        return cli(self.project, *args)

    def test_help_is_available(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("prepare-report", result.stdout)
        self.assertIn("accept-reply", result.stdout)

    def test_full_state_loop_two_rounds_and_completion_review(self):
        first = self.report()
        self.assertEqual(first["round_id"], 1)
        self.send()
        self.confirm()
        self.accept()
        cli(self.project, "begin-execution", "--task", self.task, "--owner", self.owner)
        execution = self.write("execution.md", "actual command: unittest\nexit_code: 0\nchanged: first function only\nnot yet verified: second function")
        cli(self.project, "record-execution", "--task", self.task, "--owner", self.owner, "--record-file", str(execution))
        verification = self.write("verification.md", "tests for first function pass; second function remains unmodified")
        second_report = self.write("second-report.md", "第二轮报告：第一函数已验证，第二函数的失败证据和边界。")
        next_result = json.loads(cli(self.project, "record-verification", "--task", self.task, "--owner", self.owner, "--record-file", str(verification), "--outcome", "continue", "--next-report-file", str(second_report)).stdout)
        self.assertEqual(next_result["round_id"], 2)
        self.send()
        self.confirm()
        self.accept(self.reply(status="TASK_COMPLETE", round_id=2))
        cli(self.project, "begin-completion-review", "--task", self.task, "--owner", self.owner)
        final_verification = self.write("final-verification.md", "both functions tested with exact boundary cases")
        completion_evidence = self.write("completion.txt", "acceptance 1: pass\nacceptance 2: pass\nreal test command recorded")
        done = json.loads(cli(self.project, "record-verification", "--task", self.task, "--owner", self.owner, "--record-file", str(final_verification), "--outcome", "complete", "--complete-evidence-file", str(completion_evidence)).stdout)
        self.assertEqual(done["phase"], "COMPLETED")
        state = json.loads(cli(self.project, "status", "--task", self.task).stdout)
        self.assertEqual(state["task_status"], "COMPLETED")
        self.assertEqual(state["round_id"], 2)

    def test_protocol_rejects_identity_truncation_empty_duplicate_and_body_keywords(self):
        self.report()
        self.send()
        self.confirm()
        state = json.loads(cli(self.project, "status", "--task", self.task).stdout)
        expected = {"task_id": self.task, "round_id": 1, "goal_revision": 1, "request_id": state["current_request_id"], "report_sha256": state["current_report_sha256"]}
        from importlib.util import module_from_spec, spec_from_file_location
        spec = spec_from_file_location("supervisor", SCRIPT)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        valid_meta = {
            "protocol_version": 1,
            "task_id": self.task,
            "round_id": 1,
            "goal_revision": 1,
            "request_id": state["current_request_id"],
            "report_sha256": state["current_report_sha256"],
            "status": "CONTINUE",
        }
        valid = "```supervisor-meta\n%s\n```\nbody mentioning TASK_COMPLETE is not metadata\nEND_SUPERVISOR_REPLY\n" % json.dumps(valid_meta)
        self.assertEqual(module.parse_reply_text(valid, expected)["meta"]["status"], "CONTINUE")
        cases = [
            (valid.replace("END_SUPERVISOR_REPLY", ""), "结束标记"),
            (valid.replace("\n```\nbody", "\n```\n```supervisor-meta\n{}\n```\nbody"), "只能有一个"),
            (valid.replace('"round_id": 1', '"round_id": 99'), "身份不匹配"),
            (valid.replace("\nbody mentioning", "\nEND_SUPERVISOR_REPLY\nbody mentioning"), "只能有一个结束"),
            (valid.replace("body mentioning TASK_COMPLETE is not metadata", ""), "正文为空"),
            (valid[:valid.index("\n```")] + "\n}", "JSON"),
        ]
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(module.SupervisorError) as error:
                    module.parse_reply_text(raw, expected)
                self.assertIn(message, str(error.exception))

    def test_generation_completion_is_independent_and_status_word_in_body_is_safe(self):
        self.report()
        self.send()
        self.confirm()
        reply_path = self.reply()
        evidence = self.write("reply-page.txt", "real-browser observation\nfull assistant body visible\ncomposer no longer generating\nno error banner")
        result = cli(self.project, "accept-reply", "--task", self.task, "--owner", self.owner, "--reply-file", str(reply_path), "--evidence-file", str(evidence), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("生成完成确认", result.stderr)
        result = cli(self.project, "accept-reply", "--task", self.task, "--owner", self.owner, "--reply-file", str(reply_path), "--evidence-file", str(evidence), "--generation-complete")
        self.assertEqual(result.returncode, 0)

    def test_second_executor_and_stale_state_write_are_rejected(self):
        claim = cli(self.project, "claim", "--task", self.task, check=False)
        self.assertNotEqual(claim.returncode, 0)
        self.assertIn("已有执行者", claim.stderr)
        from importlib.util import module_from_spec, spec_from_file_location
        spec = spec_from_file_location("supervisor", SCRIPT)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        store = module.TaskStore(str(self.project), self.task)
        old_version = store.load()["state_version"]
        self.report()
        with self.assertRaises(module.SupervisorError):
            store.update(old_version, lambda state: state, "stale_test")

    def test_pause_resume_send_pending_and_recovery_does_not_resend(self):
        self.report()
        self.send()
        paused = json.loads(cli(self.project, "pause", "--task", self.task, "--owner", self.owner, "--reason", "页面发送结果尚未确认").stdout)
        self.assertEqual(paused["paused_from_phase"], "SEND_PENDING")
        recovery = json.loads(cli(self.project, "recovery-check", "--task", self.task).stdout)
        self.assertIn("不得重发", recovery["recommendation"])
        resumed = json.loads(cli(self.project, "resume", "--task", self.task, "--owner", self.owner).stdout)
        self.assertEqual(resumed["phase"], "SEND_PENDING")

    def test_need_user_revision_preserves_old_reply_and_increments_goal(self):
        self.report()
        self.send()
        self.confirm()
        self.accept(self.reply(status="NEED_USER", body="请用户决定是否保留当前兼容行为。"))
        state_before = json.loads(cli(self.project, "status", "--task", self.task).stdout)
        self.assertEqual(state_before["phase"], "WAITING_USER")
        old_reply_path = Path(state_before["accepted_reply_path"])
        answer = self.write("answer.md", "用户确认继续，并要求保留已发生的修改。")
        revised = json.loads(cli(self.project, "revise-goal", "--task", self.task, "--owner", self.owner, "--answer-file", str(answer)).stdout)
        self.assertEqual(revised["goal_revision"], 2)
        self.assertTrue(old_reply_path.exists())
        state_after = json.loads(cli(self.project, "status", "--task", self.task).stdout)
        self.assertEqual(state_after["phase"], "INITIALIZED")
        self.assertEqual(len(state_after["goal_history"]), 2)

    def test_complete_requires_local_acceptance_evidence(self):
        self.report()
        self.send()
        self.confirm()
        self.accept(self.reply(status="TASK_COMPLETE", body="所有验收项均有证据。"))
        cli(self.project, "begin-completion-review", "--task", self.task, "--owner", self.owner)
        verification = self.write("verification.md", "reviewed")
        result = cli(self.project, "record-verification", "--task", self.task, "--owner", self.owner, "--record-file", str(verification), "--outcome", "complete", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("逐项验收证据", result.stderr)
        state = json.loads(cli(self.project, "status", "--task", self.task).stdout)
        self.assertEqual(state["phase"], "VERIFYING")


if __name__ == "__main__":
    unittest.main()
