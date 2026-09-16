#!/usr/bin/env python3
"""Durable local state and protocol helper for the chatgpt-supervisor skill.

The script deliberately does not contain browser automation. Codex uses the real
browser tool, then records the page observation with the commands here.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional


SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1
REPLY_STATUSES = {"CONTINUE", "TASK_COMPLETE", "NEED_USER"}
PHASES = {
    "INITIALIZED",
    "REPORT_READY",
    "SEND_PENDING",
    "WAITING_REPLY",
    "REPLY_ACCEPTED",
    "EXECUTING",
    "VERIFYING",
    "WAITING_USER",
    "PAUSED",
    "COMPLETED",
}
TASK_STATUSES = {"ACTIVE", "PAUSED", "WAITING_USER", "COMPLETED"}
TASK_ID_RE = re.compile(r"^task-[a-z0-9][a-z0-9-]{5,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SupervisorError(Exception):
    """A user-actionable local workflow error."""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise SupervisorError("无法读取文件 %s: %s" % (path, exc)) from exc


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(path))
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SupervisorError("缺少 JSON 文件 %s" % path) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SupervisorError("JSON 文件损坏 %s: %s" % (path, exc)) from exc


def read_input_file(path_text: str, label: str) -> bytes:
    path = Path(path_text).expanduser().resolve()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SupervisorError("无法读取%s %s: %s" % (label, path, exc)) from exc
    if not data.strip():
        raise SupervisorError("%s不能为空: %s" % (label, path))
    return data


def immutable_copy(source_text: str, destination: Path, label: str) -> str:
    data = read_input_file(source_text, label)
    if destination.exists():
        raise SupervisorError("拒绝覆盖已有不可变文件: %s" % destination)
    atomic_write(destination, data)
    return sha256_bytes(data)


class TaskStore:
    def __init__(self, project_text: str, task_id: str):
        project = Path(project_text).expanduser().resolve()
        if not project.is_dir():
            raise SupervisorError("项目目录不存在: %s" % project)
        if not TASK_ID_RE.match(task_id):
            raise SupervisorError("非法 task_id: %s" % task_id)
        self.project = project
        self.base = project / ".agent-work" / "chatgpt-supervisor" / task_id
        self.state_path = self.base / "state.json"
        self.state_lock_path = self.base / "state.lock"
        self.executor_path = self.base / "executor.json"
        self.events_path = self.base / "events.jsonl"

    def ensure_layout(self) -> None:
        for name in ("reports", "requests", "replies", "executions", "verifications", "evidence"):
            (self.base / name).mkdir(parents=True, exist_ok=True)

    def require_exists(self) -> None:
        if not self.state_path.exists():
            raise SupervisorError("找不到任务状态: %s" % self.base)

    @contextmanager
    def state_lock(self) -> Iterator[None]:
        self.base.mkdir(parents=True, exist_ok=True)
        with self.state_lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> Dict[str, Any]:
        self.require_exists()
        state = read_json(self.state_path)
        validate_state(state, self)
        validate_references(state, self)
        return state

    def append_event(self, event: Dict[str, Any]) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def update(self, expected_version: int, mutator: Callable[[Dict[str, Any]], Dict[str, Any]], event: str) -> Dict[str, Any]:
        with self.state_lock():
            state = self.load()
            if state["state_version"] != expected_version:
                raise SupervisorError(
                    "状态版本已变化，拒绝旧写入: 期望 %d，实际 %d" % (expected_version, state["state_version"])
                )
            new_state = mutator(state)
            validate_state(new_state, self)
            validate_references(new_state, self)
            new_state["state_version"] = expected_version + 1
            new_state["updated_at"] = now()
            write_json(self.state_path, new_state)
            self.append_event({"at": new_state["updated_at"], "event": event, "state_version": new_state["state_version"]})
            return new_state


def validate_state(state: Dict[str, Any], store: TaskStore) -> None:
    if not isinstance(state, dict):
        raise SupervisorError("state.json 必须是 JSON 对象")
    required = {
        "schema_version", "task_id", "project_path", "original_goal", "goal_revision",
        "goal_history", "conversation_url", "conversation_observation", "round_id",
        "current_report_sha256", "current_report_path", "current_request_id", "current_request_path",
        "accepted_reply_sha256", "accepted_reply_path", "accepted_reply_status", "accepted_reply_meta_path",
        "phase", "task_status", "state_version", "updated_at", "paused_from_phase", "pause_reason",
    }
    missing = required.difference(state)
    if missing:
        raise SupervisorError("state.json 缺少字段: %s" % ", ".join(sorted(missing)))
    if state["schema_version"] != SCHEMA_VERSION:
        raise SupervisorError("不支持的 schema_version: %r" % state["schema_version"])
    if state["task_id"] != store.base.name or not TASK_ID_RE.match(state["task_id"]):
        raise SupervisorError("state.json 的 task_id 与路径不一致")
    if Path(state["project_path"]).resolve() != store.project:
        raise SupervisorError("state.json 的 project_path 与当前项目不一致")
    if not isinstance(state["original_goal"], str) or not state["original_goal"].strip():
        raise SupervisorError("original_goal 不能为空")
    if type(state["goal_revision"]) is not int or state["goal_revision"] < 1:
        raise SupervisorError("goal_revision 必须是正整数")
    if not isinstance(state["goal_history"], list) or not state["goal_history"]:
        raise SupervisorError("goal_history 必须保留至少一条记录")
    if type(state["round_id"]) is not int or state["round_id"] < 0:
        raise SupervisorError("round_id 必须是非负整数")
    if state["phase"] not in PHASES:
        raise SupervisorError("非法阶段: %r" % state["phase"])
    if state["task_status"] not in TASK_STATUSES:
        raise SupervisorError("非法任务状态: %r" % state["task_status"])
    if type(state["state_version"]) is not int or state["state_version"] < 1:
        raise SupervisorError("state_version 必须是正整数")
    if state["current_report_sha256"] is not None and not SHA256_RE.match(state["current_report_sha256"]):
        raise SupervisorError("current_report_sha256 非法")
    if state["accepted_reply_sha256"] is not None and not SHA256_RE.match(state["accepted_reply_sha256"]):
        raise SupervisorError("accepted_reply_sha256 非法")
    if state["accepted_reply_status"] is not None and state["accepted_reply_status"] not in REPLY_STATUSES:
        raise SupervisorError("accepted_reply_status 非法")
    for field in ("conversation_url", "current_report_path", "current_request_id", "current_request_path", "accepted_reply_sha256", "accepted_reply_path", "accepted_reply_meta_path", "paused_from_phase", "pause_reason"):
        value = state[field]
        if value is not None and not isinstance(value, str):
            raise SupervisorError("字段 %s 类型非法" % field)
    if state["phase"] == "PAUSED" and not state["paused_from_phase"]:
        raise SupervisorError("PAUSED 必须记录 paused_from_phase")
    if state["phase"] != "PAUSED" and state["paused_from_phase"] is not None:
        raise SupervisorError("非 PAUSED 阶段不能残留 paused_from_phase")
    if state["phase"] == "COMPLETED" and state["task_status"] != "COMPLETED":
        raise SupervisorError("COMPLETED 阶段必须对应 COMPLETED 任务状态")


def validate_references(state: Dict[str, Any], store: TaskStore) -> None:
    for field in ("current_report_path", "current_request_path", "accepted_reply_path", "accepted_reply_meta_path"):
        value = state.get(field)
        if value is not None and not Path(value).is_file():
            raise SupervisorError("状态引用的文件不存在: %s=%s" % (field, value))
    if state.get("current_report_path") and state.get("current_report_sha256"):
        actual = sha256_file(Path(state["current_report_path"]))
        if actual != state["current_report_sha256"]:
            raise SupervisorError("报告哈希不匹配: %s" % state["current_report_path"])
    if state.get("accepted_reply_path") and state.get("accepted_reply_sha256"):
        actual = sha256_file(Path(state["accepted_reply_path"]))
        if actual != state["accepted_reply_sha256"]:
            raise SupervisorError("回复哈希不匹配: %s" % state["accepted_reply_path"])


def require_owner(store: TaskStore, owner: str) -> None:
    if not owner:
        raise SupervisorError("此操作需要 --owner；先用 claim 或 init 获得执行者令牌")
    if not store.executor_path.exists():
        raise SupervisorError("任务没有执行者锁；请用 claim 获取，而不是手工编辑状态")
    lock = read_json(store.executor_path)
    if lock.get("owner_id") != owner:
        raise SupervisorError("第二执行者或错误令牌被拒绝；当前执行者令牌不匹配")


def claim_task(store: TaskStore) -> str:
    store.ensure_layout()
    owner = new_id("exec")
    payload = {"owner_id": owner, "claimed_at": now(), "task_id": store.base.name}
    try:
        fd = os.open(str(store.executor_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            existing = read_json(store.executor_path)
            raise SupervisorError("任务已有执行者 %s；不自动抢占" % existing.get("owner_id", "(未知)")) from exc
        raise SupervisorError("无法创建执行者锁: %s" % exc) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(store.executor_path)
        except OSError:
            pass
        raise
    return owner


def initial_state(task_id: str, project: Path, goal: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "project_path": str(project),
        "original_goal": goal,
        "goal_revision": 1,
        "goal_history": [{"revision": 1, "text": goal, "at": now(), "kind": "original"}],
        "conversation_url": None,
        "conversation_observation": {},
        "round_id": 0,
        "current_report_sha256": None,
        "current_report_path": None,
        "current_request_id": None,
        "current_request_path": None,
        "accepted_reply_sha256": None,
        "accepted_reply_path": None,
        "accepted_reply_status": None,
        "accepted_reply_meta_path": None,
        "phase": "INITIALIZED",
        "task_status": "ACTIVE",
        "state_version": 1,
        "updated_at": now(),
        "paused_from_phase": None,
        "pause_reason": None,
    }


def create_task(project_text: str, goal: str) -> Dict[str, Any]:
    project = Path(project_text).expanduser().resolve()
    if not project.is_dir():
        raise SupervisorError("项目目录不存在: %s" % project)
    if not goal.strip():
        raise SupervisorError("目标不能为空")
    task_id = "task-%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8])
    store = TaskStore(str(project), task_id)
    store.ensure_layout()
    owner = claim_task(store)
    state = initial_state(task_id, project, goal.strip())
    write_json(store.state_path, state)
    store.append_event({"at": state["updated_at"], "event": "initialized", "state_version": 1})
    return {"task_id": task_id, "owner_id": owner, "project_path": str(project), "phase": state["phase"]}


def clear_round_fields(state: Dict[str, Any]) -> None:
    state["current_request_path"] = None
    state["accepted_reply_sha256"] = None
    state["accepted_reply_path"] = None
    state["accepted_reply_status"] = None
    state["accepted_reply_meta_path"] = None


def request_payload(state: Dict[str, Any], report_data: bytes, request_id: Optional[str] = None, body_prefix: str = "") -> bytes:
    try:
        report_text = report_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SupervisorError("报告不是 UTF-8，不能作为 Markdown 请求发送: %s" % exc) from exc
    identity = {
        "task_id": state["task_id"],
        "round_id": state["round_id"],
        "goal_revision": state["goal_revision"],
        "request_id": request_id or state["current_request_id"],
        "report_sha256": state["current_report_sha256"],
    }
    header = (
        "# ChatGPT Supervisor request\n\n"
        "The following identity is generated locally. Copy it exactly into the `supervisor-meta` reply.\n\n"
        "```supervisor-request\n%s\n```\n\n"
        "%s"
    ) % (json.dumps(identity, ensure_ascii=False, sort_keys=True), body_prefix)
    return (header + report_text).encode("utf-8")


def prepare_report(store: TaskStore, owner: str, content_file: str) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] not in {"INITIALIZED", "VERIFYING", "WAITING_USER"}:
        raise SupervisorError("当前阶段 %s 不能准备报告" % old["phase"])
    data = read_input_file(content_file, "报告")
    round_id = old["round_id"] + 1
    report_path = store.base / "reports" / ("round-%03d-report.md" % round_id)
    report_hash = sha256_bytes(data)
    if report_path.exists():
        raise SupervisorError("报告路径已存在，拒绝覆盖: %s" % report_path)
    atomic_write(report_path, data)
    request_id = new_id("req")

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] not in {"INITIALIZED", "VERIFYING", "WAITING_USER"}:
            raise SupervisorError("状态在写入前已变化，不能准备报告")
        clear_round_fields(state)
        state["round_id"] = round_id
        state["current_report_sha256"] = report_hash
        state["current_report_path"] = str(report_path)
        state["current_request_id"] = request_id
        state["phase"] = "REPORT_READY"
        state["task_status"] = "ACTIVE"
        return state

    new_state = store.update(old["state_version"], mutate, "report_ready")
    return {"phase": new_state["phase"], "round_id": round_id, "request_id": request_id, "report_sha256": report_hash, "report_path": str(report_path)}


def prepare_send(store: TaskStore, owner: str) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "REPORT_READY":
        raise SupervisorError("当前阶段 %s 不能准备发送" % old["phase"])
    report_path = Path(old["current_report_path"])
    data = request_payload(old, report_path.read_bytes())
    request_path = store.base / "requests" / ("round-%03d-%s.md" % (old["round_id"], old["current_request_id"]))
    if request_path.exists():
        raise SupervisorError("请求路径已存在，拒绝覆盖: %s" % request_path)
    atomic_write(request_path, data)

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "REPORT_READY":
            raise SupervisorError("状态在写入前已变化，不能准备发送")
        state["current_request_path"] = str(request_path)
        state["phase"] = "SEND_PENDING"
        return state

    new_state = store.update(old["state_version"], mutate, "send_pending")
    return {"phase": new_state["phase"], "request_id": new_state["current_request_id"], "request_path": str(request_path), "report_sha256": new_state["current_report_sha256"]}


def prepare_protocol_repair(store: TaskStore, owner: str, content_file: str) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "WAITING_REPLY":
        raise SupervisorError("协议修复只能从 WAITING_REPLY 开始，当前是 %s" % old["phase"])
    repair_body = read_input_file(content_file, "协议修复请求")
    request_id = new_id("req")
    request_path = store.base / "requests" / ("round-%03d-repair-%s.md" % (old["round_id"], request_id))
    atomic_write(request_path, request_payload(old, repair_body, request_id=request_id))

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "WAITING_REPLY":
            raise SupervisorError("状态在写入前已变化，不能准备协议修复")
        state["current_request_id"] = request_id
        state["current_request_path"] = str(request_path)
        state["phase"] = "SEND_PENDING"
        return state

    new_state = store.update(old["state_version"], mutate, "protocol_repair_send_pending")
    return {"phase": new_state["phase"], "request_id": request_id, "request_path": str(request_path), "round_id": new_state["round_id"], "report_sha256": new_state["current_report_sha256"]}


def confirm_sent(store: TaskStore, owner: str, evidence_file: str, conversation_url: str) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "SEND_PENDING":
        raise SupervisorError("当前阶段 %s 不能确认发送" % old["phase"])
    if not re.match(r"^https://(?:chatgpt\.com|chat\.openai\.com)/c/[^\s]+$", conversation_url):
        raise SupervisorError("必须保存实际 ChatGPT 会话 URL，而不是主页 URL: %s" % conversation_url)
    evidence = read_input_file(evidence_file, "发送页面证据")
    evidence_kind = "simulated" if "simulated" in evidence.decode("utf-8", "ignore").lower() else "real-browser"
    evidence_path = store.base / "evidence" / ("round-%03d-send-%s.txt" % (old["round_id"], old["current_request_id"]))
    atomic_write(evidence_path, evidence)

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "SEND_PENDING":
            raise SupervisorError("状态在写入前已变化，不能确认发送")
        state["conversation_url"] = conversation_url
        state["conversation_observation"] = {"send_evidence_path": str(evidence_path), "request_id": state["current_request_id"], "recorded_at": now(), "evidence_kind": evidence_kind}
        state["phase"] = "WAITING_REPLY"
        return state

    new_state = store.update(old["state_version"], mutate, "message_confirmed_sent")
    return {"phase": new_state["phase"], "conversation_url": conversation_url, "evidence_path": str(evidence_path), "request_id": new_state["current_request_id"]}


def parse_reply_text(text: str, expected: Dict[str, Any]) -> Dict[str, Any]:
    if text.count("```supervisor-meta") != 1:
        raise SupervisorError("回复必须且只能有一个 supervisor-meta 代码块")
    match = re.match(r"\A```supervisor-meta\r?\n(?P<meta>.*?)\r?\n```\r?\n?(?P<body>.*)\Z", text, re.DOTALL)
    if not match:
        raise SupervisorError("回复开头不是完整的 supervisor-meta JSON 代码块")
    try:
        meta = json.loads(match.group("meta"))
    except json.JSONDecodeError as exc:
        raise SupervisorError("supervisor-meta JSON 非法或截断: %s" % exc) from exc
    if not isinstance(meta, dict):
        raise SupervisorError("supervisor-meta 必须是 JSON 对象")
    required = {"protocol_version", "task_id", "round_id", "goal_revision", "request_id", "report_sha256", "status"}
    missing = required.difference(meta)
    if missing:
        raise SupervisorError("supervisor-meta 缺少字段: %s" % ", ".join(sorted(missing)))
    if type(meta["protocol_version"]) is not int or meta["protocol_version"] != PROTOCOL_VERSION:
        raise SupervisorError("协议版本不匹配: %r" % meta["protocol_version"])
    if type(meta["round_id"]) is not int or type(meta["goal_revision"]) is not int:
        raise SupervisorError("round_id 和 goal_revision 必须是整数")
    for field in ("task_id", "request_id", "report_sha256"):
        if not isinstance(meta[field], str) or not meta[field].strip():
            raise SupervisorError("%s 必须是非空字符串" % field)
    if not SHA256_RE.match(meta["report_sha256"]):
        raise SupervisorError("report_sha256 不是 64 位十六进制哈希")
    if not isinstance(meta["status"], str) or meta["status"] not in REPLY_STATUSES:
        raise SupervisorError("status 必须是 CONTINUE、TASK_COMPLETE 或 NEED_USER")
    for field in ("task_id", "round_id", "goal_revision", "request_id", "report_sha256"):
        if meta[field] != expected[field]:
            raise SupervisorError("回复身份不匹配: %s=%r，期望 %r" % (field, meta[field], expected[field]))
    body = match.group("body")
    if body.count("END_SUPERVISOR_REPLY") != 1:
        raise SupervisorError("回复必须且只能有一个结束标记")
    end_match = re.match(r"(?s)\A(?P<content>.*?)(?:\r?\n)END_SUPERVISOR_REPLY[ \t]*(?:\r?\n[ \t]*)*\Z", body)
    if not end_match or not end_match.group("content").strip():
        raise SupervisorError("回复正文为空、结束标记不独占一行或回复被截断")
    return {"meta": meta, "body": end_match.group("content").rstrip("\r\n")}


def accept_reply(store: TaskStore, owner: str, reply_file: str, evidence_file: str, generation_complete: bool) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "WAITING_REPLY":
        raise SupervisorError("当前阶段 %s 不能接受回复" % old["phase"])
    if not generation_complete:
        raise SupervisorError("未提供独立的生成完成确认；正文有结束标记也不能直接接受")
    raw_data = read_input_file(reply_file, "完整网页回复")
    try:
        raw_text = raw_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SupervisorError("网页回复不是 UTF-8: %s" % exc) from exc
    expected = {"task_id": old["task_id"], "round_id": old["round_id"], "goal_revision": old["goal_revision"], "request_id": old["current_request_id"], "report_sha256": old["current_report_sha256"]}
    parsed = parse_reply_text(raw_text, expected)
    evidence = read_input_file(evidence_file, "回复页面证据")
    reply_path = store.base / "replies" / ("round-%03d-%s.md" % (old["round_id"], old["current_request_id"]))
    meta_path = store.base / "replies" / ("round-%03d-%s.meta.json" % (old["round_id"], old["current_request_id"]))
    evidence_path = store.base / "evidence" / ("round-%03d-reply-%s.txt" % (old["round_id"], old["current_request_id"]))
    atomic_write(reply_path, raw_data)
    write_json(meta_path, parsed["meta"])
    atomic_write(evidence_path, evidence)
    reply_hash = sha256_bytes(raw_data)

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "WAITING_REPLY":
            raise SupervisorError("状态在写入前已变化，不能接受回复")
        state["accepted_reply_sha256"] = reply_hash
        state["accepted_reply_path"] = str(reply_path)
        state["accepted_reply_status"] = parsed["meta"]["status"]
        state["accepted_reply_meta_path"] = str(meta_path)
        state["conversation_observation"] = dict(state.get("conversation_observation") or {}, reply_evidence_path=str(evidence_path), reply_request_id=state["current_request_id"], reply_recorded_at=now(), reply_evidence_kind="real-browser" if "simulated" not in evidence.decode("utf-8", "ignore").lower() else "simulated")
        state["phase"] = "WAITING_USER" if parsed["meta"]["status"] == "NEED_USER" else "REPLY_ACCEPTED"
        state["task_status"] = "WAITING_USER" if parsed["meta"]["status"] == "NEED_USER" else "ACTIVE"
        return state

    new_state = store.update(old["state_version"], mutate, "reply_accepted")
    return {"phase": new_state["phase"], "status": parsed["meta"]["status"], "reply_sha256": reply_hash, "reply_path": str(reply_path), "meta_path": str(meta_path), "evidence_path": str(evidence_path)}


def record_invalid_reply(store: TaskStore, owner: str, reply_file: str, evidence_file: str, reason: str) -> Dict[str, Any]:
    """Preserve a complete but rejected reply without changing business phase."""
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "WAITING_REPLY":
        raise SupervisorError("只有 WAITING_REPLY 可以保存无效回复")
    raw_data = read_input_file(reply_file, "完整网页回复")
    evidence = read_input_file(evidence_file, "回复页面证据")
    suffix = new_id("invalid")
    reply_path = store.base / "replies" / ("round-%03d-invalid-%s.md" % (old["round_id"], suffix))
    evidence_path = store.base / "evidence" / ("round-%03d-invalid-%s.txt" % (old["round_id"], suffix))
    atomic_write(reply_path, raw_data)
    atomic_write(evidence_path, evidence)
    reply_hash = sha256_bytes(raw_data)

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "WAITING_REPLY":
            raise SupervisorError("状态在写入前已变化，不能保存无效回复")
        state["conversation_observation"] = dict(
            state.get("conversation_observation") or {},
            invalid_reply_path=str(reply_path),
            invalid_reply_sha256=reply_hash,
            invalid_reply_evidence_path=str(evidence_path),
            invalid_reply_reason=reason.strip(),
            invalid_reply_recorded_at=now(),
        )
        return state

    new_state = store.update(old["state_version"], mutate, "invalid_reply_preserved")
    return {"phase": new_state["phase"], "reply_sha256": reply_hash, "reply_path": str(reply_path), "evidence_path": str(evidence_path), "reason": reason.strip()}


def begin_execution(store: TaskStore, owner: str, pre_state_file: Optional[str]) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "REPLY_ACCEPTED" or old["accepted_reply_status"] != "CONTINUE":
        raise SupervisorError("只有已接受的 CONTINUE 回复可以开始执行")
    pre_path = store.base / "executions" / ("round-%03d-pre.json" % old["round_id"])
    if pre_state_file:
        data = read_input_file(pre_state_file, "执行前现场记录")
    else:
        data = json.dumps({"recorded_at": now(), "project_path": str(store.project), "note": "未提供额外现场快照"}, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write(pre_path, data)

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "REPLY_ACCEPTED" or state["accepted_reply_status"] != "CONTINUE":
            raise SupervisorError("状态在写入前已变化，不能开始执行")
        state["conversation_observation"] = dict(state.get("conversation_observation") or {}, execution_pre_path=str(pre_path))
        state["phase"] = "EXECUTING"
        return state

    new_state = store.update(old["state_version"], mutate, "execution_started")
    return {"phase": new_state["phase"], "accepted_reply_path": new_state["accepted_reply_path"], "pre_state_path": str(pre_path)}


def record_execution(store: TaskStore, owner: str, record_file: str) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "EXECUTING":
        raise SupervisorError("当前阶段 %s 不能记录执行结果" % old["phase"])
    record_path = store.base / "executions" / ("round-%03d-result.md" % old["round_id"])
    immutable_copy(record_file, record_path, "执行结果记录")

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "EXECUTING":
            raise SupervisorError("状态在写入前已变化，不能记录执行结果")
        state["conversation_observation"] = dict(state.get("conversation_observation") or {}, execution_result_path=str(record_path))
        state["phase"] = "VERIFYING"
        return state

    new_state = store.update(old["state_version"], mutate, "execution_recorded")
    return {"phase": new_state["phase"], "execution_result_path": str(record_path)}


def begin_completion_review(store: TaskStore, owner: str) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "REPLY_ACCEPTED" or old["accepted_reply_status"] != "TASK_COMPLETE":
        raise SupervisorError("只有 TASK_COMPLETE 回复可以进入完成复核")

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "REPLY_ACCEPTED" or state["accepted_reply_status"] != "TASK_COMPLETE":
            raise SupervisorError("状态在写入前已变化，不能开始完成复核")
        state["phase"] = "VERIFYING"
        return state

    new_state = store.update(old["state_version"], mutate, "completion_review_started")
    return {"phase": new_state["phase"], "accepted_reply_path": new_state["accepted_reply_path"]}


def record_verification(store: TaskStore, owner: str, record_file: str, outcome: str, next_report_file: Optional[str], complete_evidence_file: Optional[str]) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "VERIFYING":
        raise SupervisorError("当前阶段 %s 不能记录核验" % old["phase"])
    if outcome not in {"continue", "complete"}:
        raise SupervisorError("outcome 只能是 continue 或 complete；NEED_USER 应由网页回复触发")
    verification_path = store.base / "verifications" / ("round-%03d.md" % old["round_id"])
    immutable_copy(record_file, verification_path, "核验记录")
    if outcome == "complete":
        if old["accepted_reply_status"] != "TASK_COMPLETE":
            raise SupervisorError("只有 TASK_COMPLETE 回复可以记录 complete")
        if not complete_evidence_file:
            raise SupervisorError("完成必须提供逐项验收证据文件")
        final_evidence_path = store.base / "evidence" / ("round-%03d-completion.txt" % old["round_id"])
        immutable_copy(complete_evidence_file, final_evidence_path, "完成验收证据")

        def complete_mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            if state["phase"] != "VERIFYING":
                raise SupervisorError("状态在写入前已变化，不能完成复核")
            state["phase"] = "COMPLETED"
            state["task_status"] = "COMPLETED"
            state["conversation_observation"] = dict(state.get("conversation_observation") or {}, verification_path=str(verification_path), completion_evidence_path=str(final_evidence_path))
            return state

        new_state = store.update(old["state_version"], complete_mutate, "task_completed_after_evidence_review")
        return {"phase": new_state["phase"], "verification_path": str(verification_path), "completion_evidence_path": str(final_evidence_path)}

    if old["accepted_reply_status"] != "CONTINUE":
        raise SupervisorError("只有 CONTINUE 回复可以继续下一轮")
    if not next_report_file:
        raise SupervisorError("continue 必须提供下一轮完整报告")
    report_data = read_input_file(next_report_file, "下一轮报告")
    next_round = old["round_id"] + 1
    report_path = store.base / "reports" / ("round-%03d-report.md" % next_round)
    atomic_write(report_path, report_data)
    report_hash = sha256_bytes(report_data)
    request_id = new_id("req")

    def continue_mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "VERIFYING":
            raise SupervisorError("状态在写入前已变化，不能准备下一轮")
        clear_round_fields(state)
        state["round_id"] = next_round
        state["current_report_sha256"] = report_hash
        state["current_report_path"] = str(report_path)
        state["current_request_id"] = request_id
        state["conversation_observation"] = dict(state.get("conversation_observation") or {}, verification_path=str(verification_path))
        state["phase"] = "REPORT_READY"
        return state

    new_state = store.update(old["state_version"], continue_mutate, "verification_recorded_next_report_ready")
    return {"phase": new_state["phase"], "verification_path": str(verification_path), "round_id": next_round, "request_id": request_id, "report_sha256": report_hash, "report_path": str(report_path)}


def revise_goal(store: TaskStore, owner: str, answer_file: str) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "WAITING_USER":
        raise SupervisorError("只有 WAITING_USER 可以接收用户补充")
    answer = read_input_file(answer_file, "用户补充")
    try:
        answer_text = answer.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SupervisorError("用户补充不是 UTF-8: %s" % exc) from exc
    if not answer_text:
        raise SupervisorError("用户补充不能为空")

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "WAITING_USER":
            raise SupervisorError("状态在写入前已变化，不能更新目标")
        revision = state["goal_revision"] + 1
        state["goal_revision"] = revision
        state["goal_history"].append({"revision": revision, "text": answer_text, "at": now(), "kind": "user_revision"})
        state["phase"] = "INITIALIZED"
        state["task_status"] = "ACTIVE"
        state["paused_from_phase"] = None
        state["pause_reason"] = None
        clear_round_fields(state)
        state["current_report_sha256"] = None
        state["current_report_path"] = None
        state["current_request_id"] = None
        state["conversation_observation"] = dict(state.get("conversation_observation") or {}, last_user_revision=revision)
        return state

    new_state = store.update(old["state_version"], mutate, "goal_revision_updated")
    return {"phase": new_state["phase"], "goal_revision": new_state["goal_revision"], "task_id": new_state["task_id"]}


def pause_task(store: TaskStore, owner: str, reason: str, evidence_file: Optional[str]) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] in {"COMPLETED", "PAUSED"}:
        raise SupervisorError("当前阶段 %s 不能再次暂停" % old["phase"])
    if not reason.strip():
        raise SupervisorError("暂停原因不能为空")
    evidence_path = None
    if evidence_file:
        evidence_path = store.base / "evidence" / ("pause-%s.txt" % new_id("evidence"))
        immutable_copy(evidence_file, evidence_path, "暂停证据")

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] in {"COMPLETED", "PAUSED"}:
            raise SupervisorError("状态在写入前已变化，不能暂停")
        state["paused_from_phase"] = state["phase"]
        state["pause_reason"] = reason.strip()
        state["phase"] = "PAUSED"
        state["task_status"] = "PAUSED"
        state["conversation_observation"] = dict(state.get("conversation_observation") or {}, pause_evidence_path=str(evidence_path) if evidence_path else None)
        return state

    new_state = store.update(old["state_version"], mutate, "task_paused")
    return {"phase": new_state["phase"], "paused_from_phase": new_state["paused_from_phase"], "reason": reason.strip(), "evidence_path": str(evidence_path) if evidence_path else None}


def resume_task(store: TaskStore, owner: str) -> Dict[str, Any]:
    require_owner(store, owner)
    old = store.load()
    if old["phase"] != "PAUSED" or not old["paused_from_phase"]:
        raise SupervisorError("当前任务不在可恢复的 PAUSED 状态")
    destination = old["paused_from_phase"]

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        if state["phase"] != "PAUSED" or state["paused_from_phase"] != destination:
            raise SupervisorError("状态在写入前已变化，不能恢复")
        state["phase"] = destination
        state["task_status"] = "WAITING_USER" if destination == "WAITING_USER" else "ACTIVE"
        state["paused_from_phase"] = None
        state["pause_reason"] = None
        return state

    new_state = store.update(old["state_version"], mutate, "task_resumed")
    return {"phase": new_state["phase"]}


def recovery_check(store: TaskStore) -> Dict[str, Any]:
    state = store.load()
    phase = state["phase"]
    recommendation = {
        "SEND_PENDING": "打开保存的 conversation_url，在同一会话查找原 request_id；确认未发送前不得重发，存在歧义就暂停。",
        "WAITING_REPLY": "重新定位已确认的用户消息及其对应 assistant 回复；生成中继续有界观察，未证明完整前不接受。",
        "REPLY_ACCEPTED": "使用已固定的 accepted_reply_path 和哈希；先检查 goal_revision，再开始或进入完成复核。",
        "EXECUTING": "检查真实 diff、文件和执行记录，判断完整/部分/未知后再继续，不能凭布尔标记重放。",
        "VERIFYING": "运行必要核验并记录结果；不要因为缺少报告而重放已发生修改。",
        "WAITING_USER": "保存当前材料；收到用户回答后调用 revise-goal，再准备新报告。",
        "PAUSED": "按 pause_reason 和 paused_from_phase 处理；先恢复同一会话及证据，不得抢占；不得重发。",
        "REPORT_READY": "确认报告哈希后调用 prepare-send，点击浏览器发送前保持 SEND_PENDING。",
        "INITIALIZED": "先准备首份现场报告。",
        "COMPLETED": "目标已在本地证据复核后完成；仅可读。",
    }[phase]
    return {"task_id": state["task_id"], "phase": phase, "state_version": state["state_version"], "conversation_url": state["conversation_url"], "recommendation": recommendation, "references_valid": True, "accepted_reply_status": state["accepted_reply_status"]}


def status(store: TaskStore) -> Dict[str, Any]:
    state = store.load()
    result = dict(state)
    result["executor_present"] = store.executor_path.exists()
    if store.executor_path.exists():
        lock = read_json(store.executor_path)
        result["executor_id"] = lock.get("owner_id")
    return result


def list_tasks(project_text: str) -> List[Dict[str, Any]]:
    project = Path(project_text).expanduser().resolve()
    root = project / ".agent-work" / "chatgpt-supervisor"
    if not root.exists():
        return []
    result = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and TASK_ID_RE.match(child.name) and (child / "state.json").exists():
            try:
                state = TaskStore(str(project), child.name).load()
                result.append({"task_id": child.name, "phase": state["phase"], "goal_revision": state["goal_revision"], "updated_at": state["updated_at"]})
            except SupervisorError as exc:
                result.append({"task_id": child.name, "error": str(exc)})
    return result


def release_task(store: TaskStore, owner: str) -> Dict[str, Any]:
    require_owner(store, owner)
    state = store.load()
    if state["phase"] not in {"PAUSED", "WAITING_USER", "COMPLETED"}:
        raise SupervisorError("仅可释放 PAUSED、WAITING_USER 或 COMPLETED 任务的执行者锁；当前是 %s" % state["phase"])
    try:
        store.executor_path.unlink()
    except OSError as exc:
        raise SupervisorError("无法释放执行者锁: %s" % exc) from exc
    return {"released": True, "task_id": state["task_id"]}


def add_common(parser: argparse.ArgumentParser, task: bool = True, owner: bool = False) -> None:
    parser.add_argument("--project", required=True, help="目标项目绝对或相对路径")
    if task:
        parser.add_argument("--task", required=True, help="chatgpt-supervisor task_id")
    if owner:
        parser.add_argument("--owner", required=True, help="init/claim 返回的执行者令牌")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="chatgpt-supervisor 的本地状态、协议和证据 CLI；浏览器由 Codex mcp__cua_repl 操作")
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("init", help="创建新目标并占有执行者锁")
    p.add_argument("--project", required=True)
    p.add_argument("--goal", required=True)
    p = subs.add_parser("claim", help="占有一个没有执行者锁的已有目标；不会自动抢占")
    add_common(p)
    p = subs.add_parser("list", help="列出项目内目标")
    p.add_argument("--project", required=True)
    p = subs.add_parser("status", help="查看并校验当前状态")
    add_common(p)
    p = subs.add_parser("recovery-check", help="按当前阶段给出可恢复动作")
    add_common(p)
    p = subs.add_parser("release", help="释放自己的执行者锁")
    add_common(p, owner=True)
    p = subs.add_parser("prepare-report", help="保存不可变现场报告并进入 REPORT_READY")
    add_common(p, owner=True)
    p.add_argument("--content-file", required=True)
    p = subs.add_parser("prepare-send", help="保存发送副本并在浏览器点击前进入 SEND_PENDING")
    add_common(p, owner=True)
    p = subs.add_parser("prepare-protocol-repair", help="同轮保存协议修复请求；不递增 round_id")
    add_common(p, owner=True)
    p.add_argument("--content-file", required=True)
    p = subs.add_parser("confirm-sent", help="记录页面已显示本轮用户消息并进入 WAITING_REPLY")
    add_common(p, owner=True)
    p.add_argument("--conversation-url", required=True)
    p.add_argument("--evidence-file", required=True)
    p = subs.add_parser("accept-reply", help="校验并固定完整网页回复")
    add_common(p, owner=True)
    p.add_argument("--reply-file", required=True)
    p.add_argument("--evidence-file", required=True)
    p.add_argument("--generation-complete", action="store_true", help="Codex 已从真实页面确认完成；本地测试可显式模拟，但不能代替真实浏览器证据")
    p = subs.add_parser("record-invalid-reply", help="保存完整但被协议拒绝的回复，不推进业务状态")
    add_common(p, owner=True)
    p.add_argument("--reply-file", required=True)
    p.add_argument("--evidence-file", required=True)
    p.add_argument("--reason", required=True)
    p = subs.add_parser("begin-execution", help="固定 CONTINUE 回复并进入 EXECUTING")
    add_common(p, owner=True)
    p.add_argument("--pre-state-file")
    p = subs.add_parser("record-execution", help="保存实际执行命令、diff 和结果并进入 VERIFYING")
    add_common(p, owner=True)
    p.add_argument("--record-file", required=True)
    p = subs.add_parser("begin-completion-review", help="固定 TASK_COMPLETE 回复并进入完成复核")
    add_common(p, owner=True)
    p = subs.add_parser("record-verification", help="保存核验，完成目标或准备下一轮报告")
    add_common(p, owner=True)
    p.add_argument("--record-file", required=True)
    p.add_argument("--outcome", choices=["continue", "complete"], required=True)
    p.add_argument("--next-report-file")
    p.add_argument("--complete-evidence-file")
    p = subs.add_parser("revise-goal", help="保存用户补充并递增 goal_revision")
    add_common(p, owner=True)
    p.add_argument("--answer-file", required=True)
    p = subs.add_parser("pause", help="显式暂停，不自动抢占或重发")
    add_common(p, owner=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence-file")
    p = subs.add_parser("resume", help="恢复到 pause 前的阶段")
    add_common(p, owner=True)
    return parser


def run(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "init":
        return create_task(args.project, args.goal)
    if args.command == "list":
        return {"project_path": str(Path(args.project).expanduser().resolve()), "tasks": list_tasks(args.project)}
    store = TaskStore(args.project, args.task)
    if args.command == "claim":
        store.require_exists()
        owner = claim_task(store)
        return {"task_id": args.task, "owner_id": owner}
    if args.command == "status":
        return status(store)
    if args.command == "recovery-check":
        return recovery_check(store)
    if args.command == "release":
        return release_task(store, args.owner)
    if args.command == "prepare-report":
        return prepare_report(store, args.owner, args.content_file)
    if args.command == "prepare-send":
        return prepare_send(store, args.owner)
    if args.command == "prepare-protocol-repair":
        return prepare_protocol_repair(store, args.owner, args.content_file)
    if args.command == "confirm-sent":
        return confirm_sent(store, args.owner, args.evidence_file, args.conversation_url)
    if args.command == "accept-reply":
        return accept_reply(store, args.owner, args.reply_file, args.evidence_file, args.generation_complete)
    if args.command == "record-invalid-reply":
        return record_invalid_reply(store, args.owner, args.reply_file, args.evidence_file, args.reason)
    if args.command == "begin-execution":
        return begin_execution(store, args.owner, args.pre_state_file)
    if args.command == "record-execution":
        return record_execution(store, args.owner, args.record_file)
    if args.command == "begin-completion-review":
        return begin_completion_review(store, args.owner)
    if args.command == "record-verification":
        return record_verification(store, args.owner, args.record_file, args.outcome, args.next_report_file, args.complete_evidence_file)
    if args.command == "revise-goal":
        return revise_goal(store, args.owner, args.answer_file)
    if args.command == "pause":
        return pause_task(store, args.owner, args.reason, args.evidence_file)
    if args.command == "resume":
        return resume_task(store, args.owner)
    raise SupervisorError("未知命令: %s" % args.command)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except SupervisorError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print("ERROR: 本地状态操作失败: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
