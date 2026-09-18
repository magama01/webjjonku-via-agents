from __future__ import annotations

"""Small ordinary Oracle executor.

This is intentionally separate from the historical workflow/state machinery in
``chatgpt_oracle_run.py`` and ``chatgpt_oracle_state.py``.  New executions have
one mission, one owned browser tab, one model/effort check, and one durable
capture.  Historical commands remain available only through their explicit
recovery entry points.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


BIN = Path(__file__).resolve().parent
MANIFEST_SCHEMA = "codex.chatgpt.oracle-execution/v1"
STATE_SCHEMA = "codex.chatgpt.oracle-execution-state/v1"
CHATGPT_URL = "https://chatgpt.com/?temporary-chat=true"
CHATGPT_HOME_URL = "https://chatgpt.com/"
WORKSPACE_PROJECT_MAP_SCHEMA = "codex.chatgpt.workspace-project-map/v1"
DEFAULT_APP_NAME = "codex"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "extended"
SUPPORTED_MODELS = ("latest", "gpt-5.6-sol")
SUPPORTED_EFFORTS = ("pro", "extra-high", "extended")
# ChatGPT labels Oracle's "extended" tier "High" on the GPT-5.6 Sol slider.
EFFORT_ALIASES = {"high": "extended"}
EXTENDED_EFFORT_LABELS = frozenset({"extended", "high", "hoch", "erweitert", "高い", "扩展", "深度", "加强", "高", "높음"})
ORACLE_EXPLICIT_STRATEGY = "select"
# Oracle's own --browser-timeout is 100m; the wrapper's budget must outlast it
# so a hung Oracle process (not a slow answer) is what trips the wrapper.
DEFAULT_OBSERVATION_BUDGET_SECONDS = 110 * 60
MAX_RECONNECTS = 2
TERMINAL_ORACLE_STATES = frozenset({"complete", "completed", "done", "finished"})
UNRESOLVED_STATUSES = frozenset({"prepared", "running", "attention_required"})
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,95}$")
THREAD_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
APP_NAME_RE = re.compile(r"^[^\r\n@][^\r\n]*$")
TARGET_ID_RE = re.compile(r"^[A-Fa-f0-9]{8,64}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PICKER_PROOF_PREFIX = "[browser] Picker DOM proof:"
MODEL_EVIDENCE_PREFIX = "[browser] Model selection evidence:"
THINKING_PREFIX = "[browser] Thinking time:"
THINKING_EVIDENCE_PREFIX = "[browser] Thinking effort evidence:"
ALLOWED_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "project_root",
        "mission_path",
        "run_root",
        "run_id",
        "source_thread_id",
        "session_id",
        "model",
        "effort",
        "app_name",
    }
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = _load("chatgpt_oracle_execute_runtime", BIN / "chatgpt_oracle_runtime.py")
COMPAT = _load("chatgpt_oracle_execute_compat", BIN / "chatgpt_oracle_compat.py")


class ExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")


def _resolve_session_id(explicit_session_id: str | None = None) -> str | None:
    if explicit_session_id is None or not str(explicit_session_id).strip():
        return None
    raw = str(explicit_session_id).strip()
    if SESSION_ID_RE.fullmatch(raw) is None:
        raise ExecutionError(
            "SESSION_ID_INVALID",
            "session_id must be a safe 3-96 character identifier starting with an alphanumeric character and containing only [A-Za-z0-9._-]",
            {"session_id": raw},
        )
    return raw


@dataclass(frozen=True)
class ExecutionConfig:
    project_root: Path
    mission_path: Path
    mission_sha256: str
    run_root: Path
    run_id: str
    source_thread_id: str | None
    model: str
    effort: str
    app_name: str
    copy_profile: Path
    session_id: str | None = None


def _absolute_path(value: str | Path | None, *, label: str, must_exist: bool) -> Path:
    raw = Path(str(value or "")).expanduser()
    if not raw.is_absolute():
        raise ExecutionError(f"{label.upper()}_ABSOLUTE_REQUIRED", f"{label} must be absolute", {"path": str(raw)})
    try:
        return raw.resolve(strict=must_exist)
    except OSError as exc:
        raise ExecutionError(f"{label.upper()}_INVALID", f"{label} could not be resolved", {"path": str(raw)}) from exc


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_model(value: str | None) -> str:
    model = str(value or DEFAULT_MODEL).strip().casefold()
    if model not in SUPPORTED_MODELS:
        raise ExecutionError("MODEL_UNSUPPORTED", "model is not supported by the lean Oracle route", {"supported": list(SUPPORTED_MODELS)})
    return model


def _normalize_effort(value: str | None) -> str:
    effort = str(value or DEFAULT_EFFORT).strip().casefold().replace("_", "-")
    effort = EFFORT_ALIASES.get(effort, effort)
    if effort not in SUPPORTED_EFFORTS:
        raise ExecutionError("EFFORT_UNSUPPORTED", "effort is not supported by the lean Oracle route", {"supported": list(SUPPORTED_EFFORTS)})
    return effort


def _normalize_app_name(value: str | None) -> str:
    app_name = str(value or DEFAULT_APP_NAME).strip().lstrip("@").strip()
    if not app_name or APP_NAME_RE.fullmatch(app_name) is None:
        raise ExecutionError("APP_NAME_INVALID", "app_name must be one nonempty line without a leading @")
    return app_name


def _default_run_root(project_root: Path) -> Path:
    base = Path(os.environ.get("CODEX_ORACLE_STATE_ROOT") or (Path.home() / ".codex" / "state" / "chatgpt-oracle")).expanduser().resolve()
    key = hashlib.sha256(str(project_root).casefold().encode("utf-8")).hexdigest()[:24]
    return base / "ordinary" / "projects" / key / "runs"


def _workspace_project_map_path(config: ExecutionConfig) -> Path:
    override = str(os.environ.get("CODEX_ORACLE_PROJECT_MAP_PATH") or "").strip()
    path = (
        Path(override).expanduser().resolve(strict=False)
        if override
        else (Path(os.environ.get("CODEX_ORACLE_STATE_ROOT") or (Path.home() / ".codex" / "state" / "chatgpt-oracle")).expanduser().resolve()
              / "workspace-projects.json")
    )
    if _is_within(config.project_root, path) or _is_within(path, config.project_root):
        raise ExecutionError(
            "WORKSPACE_PROJECT_MAP_OVERLAPS_PROJECT",
            "workspace Project mapping state must be outside the approved project root",
            {"path": str(path)},
        )
    return path


def _workspace_project_name(config: ExecutionConfig) -> str:
    name = config.project_root.name.strip()
    if not name:
        raise ExecutionError("WORKSPACE_PROJECT_NAME_INVALID", "project_root basename cannot be used as a ChatGPT Project name")
    return name


def _workspace_project_instructions(config: ExecutionConfig) -> str:
    return (
        f"Workspace 경로: {config.project_root}\n\n"
        "규칙:\n"
        "- 모든 응답은 한국어로 작성한다.\n"
        "- 작업을 시작하기 전에 이 workspace에 적용되는 AGENTS.md를 최우선으로 찾아 끝까지 읽고 따른다.\n"
        "- 파일·코드 작업은 승인된 workspace에서 DevSpace를 우선 사용한다.\n"
        "- 다른 workspace나 승인되지 않은 root로 대체하지 않는다.\n"
    )


def _normalize_project_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise ExecutionError("WORKSPACE_PROJECT_URL_INVALID", "stored ChatGPT Project URL is invalid") from exc
    if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
        raise ExecutionError("WORKSPACE_PROJECT_URL_INVALID", "ChatGPT Project URL must use https://chatgpt.com")
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"/g/g-p-[A-Za-z0-9_-]+/project", path):
        raise ExecutionError(
            "WORKSPACE_PROJECT_URL_INVALID",
            "ChatGPT Project URL has an unexpected shape",
            {"url": raw},
        )
    return urllib.parse.urlunsplit(("https", "chatgpt.com", path, "", ""))


def _workspace_project_chat_url(project_url: str) -> str:
    # Workspace Projects intentionally use their normal Project composer.  Do
    # not append temporary-chat=true: that would move the run out of the
    # Project scope instead of applying the Project instructions.
    return _normalize_project_url(project_url)


def _is_exact_workspace_project_url(value: Any) -> bool:
    try:
        return _normalize_project_url(value) == str(value or "").strip()
    except ExecutionError:
        return False


def _is_chatgpt_session_url(value: Any) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc == "chatgpt.com" and bool(parsed.path)


def _workspace_project_key(config: ExecutionConfig) -> str:
    if config.session_id:
        # Hash the whole (session_id, exact root) tuple so long or
        # punctuation-heavy host ids cannot collide after truncation.
        digest = hashlib.sha256(
            f"{config.session_id}\0{str(config.project_root).casefold()}".encode("utf-8")
        ).hexdigest()[:24]
        return f"sess-{digest}"
    return config.run_id


def _legacy_workspace_project_key(config: ExecutionConfig) -> str | None:
    # Pre-hash scheme: sanitized session id truncated to 70 chars. Read-only
    # so sessions mapped before the change keep their Project.
    if not config.session_id:
        return None
    normalized = re.sub(r"[^A-Za-z0-9._-]", "-", config.session_id.strip())
    root_key = hashlib.sha256(str(config.project_root).casefold().encode("utf-8")).hexdigest()[:12]
    key = f"sess-{normalized[:70]}-{root_key}"[:95]
    if RUN_ID_RE.fullmatch(key):
        return key
    return f"sess-{hashlib.sha256(config.session_id.encode('utf-8')).hexdigest()[:12]}-{root_key}"


def _load_workspace_project_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": WORKSPACE_PROJECT_MAP_SCHEMA, "projects": {}}
    if path.is_symlink() or not path.is_file():
        raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project mapping state is unsafe", {"path": str(path)})
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project mapping state is unreadable", {"path": str(path)}) from exc
    if not isinstance(payload, dict) or payload.get("schema") != WORKSPACE_PROJECT_MAP_SCHEMA or not isinstance(payload.get("projects"), dict):
        raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project mapping state has an unexpected schema", {"path": str(path)})
    for key, entry in payload["projects"].items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project mapping entry is invalid", {"path": str(path)})
        if not isinstance(entry.get("name"), str):
            raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project mapping name is invalid", {"path": str(path)})
        _normalize_project_url(entry.get("url"))
        # Historical v1 entries were keyed by absolute workspace path and only
        # stored name/url. Keep accepting and preserving them, but never use
        # them for a new execution session.
        if Path(key).is_absolute():
            continue
        if RUN_ID_RE.fullmatch(key) is None:
            raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project mapping key is invalid", {"path": str(path)})
        entry_key = str(entry.get("key") or entry.get("run_id") or "")
        if entry_key != key:
            raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project run_id does not match its mapping key", {"path": str(path)})
        workspace_root = str(entry.get("workspace_root") or "")
        if not workspace_root or not Path(workspace_root).is_absolute():
            raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project mapping workspace_root is invalid", {"path": str(path)})
    return payload


def _mapped_workspace_project(config: ExecutionConfig, payload: Mapping[str, Any]) -> dict[str, str] | None:
    projects = payload.get("projects") or {}
    key = _workspace_project_key(config)
    entry = projects.get(key)
    if entry is None:
        legacy_key = _legacy_workspace_project_key(config)
        if legacy_key is None or legacy_key not in projects:
            return None
        key = legacy_key
        entry = projects[key]
    if not isinstance(entry, Mapping):
        raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "workspace Project mapping entry is invalid")
    entry_key = str(entry.get("key") or entry.get("run_id") or "")
    if entry_key != key:
        raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "stored ChatGPT Project run_id does not match the current execution")
    if config.session_id:
        if str(entry.get("session_id") or "") != config.session_id:
            raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "stored ChatGPT Project session_id does not match the current execution")
    else:
        if str(entry.get("run_id") or "") != config.run_id:
            raise ExecutionError("WORKSPACE_PROJECT_MAP_INVALID", "stored ChatGPT Project run_id does not match the current execution")
    if str(entry.get("workspace_root") or "") != str(config.project_root):
        raise ExecutionError(
            "WORKSPACE_PROJECT_MAP_ROOT_MISMATCH",
            "stored ChatGPT Project workspace does not match the current execution",
            {"expected": str(config.project_root), "actual": entry.get("workspace_root")},
        )
    expected_name = _workspace_project_name(config)
    if str(entry.get("name") or "") != expected_name:
        raise ExecutionError(
            "WORKSPACE_PROJECT_MAP_NAME_MISMATCH",
            "stored ChatGPT Project name does not match the current workspace basename",
            {"expected": expected_name, "actual": entry.get("name")},
        )
    return {"name": expected_name, "url": _normalize_project_url(entry.get("url"))}


def _write_workspace_project_mapping(config: ExecutionConfig, path: Path, payload: dict[str, Any], *, url: str) -> dict[str, str]:
    name = _workspace_project_name(config)
    normalized_url = _normalize_project_url(url)
    key = _workspace_project_key(config)
    projects = dict(payload.get("projects") or {})
    entry: dict[str, Any] = {
        "run_id": config.run_id,
        "workspace_root": str(config.project_root),
        "name": name,
        "url": normalized_url,
    }
    if config.session_id:
        entry["session_id"] = config.session_id
        entry["key"] = key
    projects[key] = entry
    _write_json_atomic(path, {"schema": WORKSPACE_PROJECT_MAP_SCHEMA, "projects": projects})
    return {"name": name, "url": normalized_url}


def make_config(
    *,
    project_root: str | Path,
    mission_path: str | Path,
    run_root: str | Path | None = None,
    run_id: str | None = None,
    source_thread_id: str | None = None,
    session_id: str | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    app_name: str = DEFAULT_APP_NAME,
) -> ExecutionConfig:
    root = _absolute_path(project_root, label="project_root", must_exist=True)
    if not root.is_dir():
        raise ExecutionError("PROJECT_ROOT_NOT_DIRECTORY", "project_root must identify a directory")
    if root.parent == root:
        raise ExecutionError("PROJECT_ROOT_TOO_BROAD", "project_root must not be a filesystem or drive root")
    raw_mission = Path(str(mission_path)).expanduser()
    if raw_mission.is_symlink():
        raise ExecutionError("MISSION_FILE_INVALID", "mission_path must not be a symlink", {"path": str(raw_mission)})
    mission = _absolute_path(raw_mission, label="mission_path", must_exist=True)
    if not mission.is_file() or not _is_within(root, mission):
        raise ExecutionError("MISSION_OUTSIDE_APPROVED_ROOT", "mission_path must be a regular file inside project_root")
    try:
        mission.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExecutionError("MISSION_UTF8_REQUIRED", "mission_path must contain valid UTF-8", {"offset": exc.start}) from exc
    actual_run_root = _absolute_path(run_root, label="run_root", must_exist=False) if run_root else _default_run_root(root)
    if _is_within(root, actual_run_root) or _is_within(actual_run_root, root):
        raise ExecutionError("RUN_ROOT_OVERLAPS_PROJECT", "run_root must be disjoint from the approved project root")
    actual_run_id = str(run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}").strip()
    if RUN_ID_RE.fullmatch(actual_run_id) is None:
        raise ExecutionError("RUN_ID_INVALID", "run_id must be a safe 8-96 character identifier")
    actual_session_id = _resolve_session_id(session_id)
    explicit_thread = str(source_thread_id or "").strip().casefold()
    environment_thread = str(os.environ.get("CODEX_THREAD_ID") or "").strip().casefold()
    if explicit_thread and THREAD_ID_RE.fullmatch(explicit_thread) is None:
        raise ExecutionError("SOURCE_THREAD_ID_INVALID", "source_thread_id must be a Codex task UUID")
    if environment_thread and THREAD_ID_RE.fullmatch(environment_thread) is None:
        raise ExecutionError("SOURCE_THREAD_ID_INVALID", "CODEX_THREAD_ID must be a Codex task UUID when set")
    if explicit_thread and environment_thread and explicit_thread != environment_thread:
        raise ExecutionError("SOURCE_THREAD_ID_MISMATCH", "manifest task owner does not match the current Codex task")
    profile_override = str(os.environ.get("ORACLE_BROWSER_PROFILE_DIR") or "").strip()
    copy_profile = (
        Path(profile_override).expanduser().absolute()
        if profile_override
        else (Path.home() / ".oracle" / "browser-profile").absolute()
    )
    if copy_profile.is_symlink():
        raise ExecutionError(
            "SIGNED_IN_PROFILE_UNAVAILABLE",
            "the signed-in Oracle profile seed is unavailable or unsafe",
            {"copy_profile": str(copy_profile)},
        )
    if _is_within(root, copy_profile) or _is_within(copy_profile, root):
        raise ExecutionError("COPY_PROFILE_OVERLAPS_PROJECT", "profile seed must be outside project_root")
    return ExecutionConfig(
        root,
        mission,
        _sha256(mission),
        actual_run_root,
        actual_run_id,
        explicit_thread or environment_thread or None,
        _normalize_model(model),
        _normalize_effort(effort),
        _normalize_app_name(app_name),
        copy_profile,
        actual_session_id,
    )


def manifest_payload(config: ExecutionConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "project_root": str(config.project_root),
        "mission_path": str(config.mission_path),
        "model": config.model,
        "effort": config.effort,
        "app_name": config.app_name,
    }
    if config.run_root != _default_run_root(config.project_root):
        payload["run_root"] = str(config.run_root)
    if config.run_id:
        payload["run_id"] = config.run_id
    if config.source_thread_id:
        payload["source_thread_id"] = config.source_thread_id
    if config.session_id:
        payload["session_id"] = config.session_id
    return payload


def load_manifest(path: Path) -> ExecutionConfig:
    manifest_path = _absolute_path(path, label="manifest_path", must_exist=True)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionError("MANIFEST_INVALID", "manifest must be one valid UTF-8 JSON object") from exc
    if not isinstance(payload, dict) or payload.get("schema") != MANIFEST_SCHEMA:
        raise ExecutionError("MANIFEST_SCHEMA_INVALID", f"manifest schema must be {MANIFEST_SCHEMA}")
    unknown = sorted(set(payload) - ALLOWED_MANIFEST_FIELDS)
    if unknown:
        raise ExecutionError("MANIFEST_FIELDS_INVALID", "ordinary manifests contain retired or unknown fields", {"fields": unknown})
    return make_config(
        project_root=payload.get("project_root"),
        mission_path=payload.get("mission_path"),
        run_root=payload.get("run_root"),
        run_id=payload.get("run_id"),
        source_thread_id=payload.get("source_thread_id"),
        session_id=payload.get("session_id"),
        model=payload.get("model", DEFAULT_MODEL),
        effort=payload.get("effort", DEFAULT_EFFORT),
        app_name=payload.get("app_name", DEFAULT_APP_NAME),
    )


def public_contract(config: ExecutionConfig) -> dict[str, Any]:
    contract = {
        "schema": MANIFEST_SCHEMA,
        "project_root": str(config.project_root),
        "mission_path": str(config.mission_path),
        "model": config.model,
        "effort": config.effort,
        "app_name": config.app_name,
        "oracle_request": {"model": config.model, "model_strategy": _model_strategy(config.model)},
        "chatgpt_url": CHATGPT_URL,
        "workspace_project": {"name": _workspace_project_name(config), "key": _workspace_project_key(config)},
        "archive": "never",
        "temporary_chat": True,
        "personalization": "enabled-before-submit",
    }
    if config.session_id:
        contract["session_id"] = config.session_id
    return contract


def _model_strategy(model: str) -> str:
    return ORACLE_EXPLICIT_STRATEGY


def _composer_prompt(config: ExecutionConfig) -> str:
    return (
        f"@{config.app_name} Open exactly this approved project root in checkout mode: {config.project_root}. "
        f"Read and execute the mission file: {config.mission_path}. "
        "The mission defines the task intent and action authority; read it and applicable AGENTS.md fully before acting. "
        "Do not substitute another root or connector, and do not change ChatGPT account, privacy, app, or permission settings. The runner establishes the authorized ChatGPT conversation scope before submission."
    )


def _slug(config: ExecutionConfig) -> str:
    words = (re.findall(r"[a-z0-9]+", config.project_root.name.casefold()) or ["project"])[:3]
    identity = hashlib.sha256(
        (str(config.project_root).casefold() + "\0" + config.run_id + "\0" + str(config.source_thread_id or "cli")).encode("utf-8")
    ).hexdigest()[:16]
    # Oracle truncates each slug word to ten characters. Split the identity so
    # our persisted name is exactly the session directory Oracle creates.
    return f"oracle-{words[0][:10]}-{identity[:8]}-{identity[8:]}"


def build_oracle_argv(
    config: ExecutionConfig,
    command: Sequence[str],
    output_path: Path,
    slug: str,
    *,
    cdp_port: int | None = None,
    browser_tab: str | None = None,
    chatgpt_url: str = CHATGPT_URL,
) -> list[str]:
    if browser_tab is not None and (
        cdp_port is None or TARGET_ID_RE.fullmatch(browser_tab) is None
    ):
        raise ExecutionError("ORACLE_BROWSER_TAB_INVALID", "browser_tab requires an exact remote Chrome target")
    browser_args = (
        [
            "--remote-chrome", f"127.0.0.1:{cdp_port}",
            *(["--browser-tab", browser_tab] if browser_tab is not None else []),
        ]
        if cdp_port is not None
        else [
            "--browser-manual-login",
            "--browser-keep-browser",
            "--browser-hide-window",
            "--browser-manual-login-profile-dir", str(output_path.parent / "browser-profile"),
        ]
    )
    return [
        *command,
        "--engine", "browser",
        "--model", config.model,
        "--browser-model-strategy", _model_strategy(config.model),
        "--browser-thinking-time", config.effort,
        "--chatgpt-url", chatgpt_url,
        "--browser-archive", "never",
        *browser_args,
        "--browser-timeout", "100m",
        "--verbose",
        "--slug", slug,
        "--prompt", _composer_prompt(config),
        "--write-output", str(output_path),
    ]


def _redacted_argv(argv: Sequence[str]) -> list[str]:
    value = list(argv)
    if "--prompt" in value:
        value[value.index("--prompt") + 1] = "<mission-handoff>"
    return value


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionError("RUN_STATE_INVALID", "run state is unavailable or invalid", {"path": str(path)}) from exc
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        raise ExecutionError("RUN_STATE_SCHEMA_INVALID", f"run state schema must be {STATE_SCHEMA}")
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp(state: dict[str, Any], key: str) -> None:
    timeline = state.get("timeline")
    if not isinstance(timeline, dict):
        timeline = {}
        state["timeline"] = timeline
    timeline[key] = _utc_now()


def _observation_budget_seconds() -> float:
    raw = str(os.environ.get("CODEX_ORACLE_OBSERVATION_BUDGET_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else float(DEFAULT_OBSERVATION_BUDGET_SECONDS)
    except ValueError:
        value = float(DEFAULT_OBSERVATION_BUDGET_SECONDS)
    return value if value > 0 else float(DEFAULT_OBSERVATION_BUDGET_SECONDS)


def _new_observation() -> dict[str, Any]:
    budget = _observation_budget_seconds()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=budget)
    return {
        "budget_seconds": budget,
        "deadline": deadline.isoformat(timespec="seconds"),
        "reconnects": 0,
        "max_reconnects": MAX_RECONNECTS,
    }


def _observation_remaining(state: Mapping[str, Any]) -> float:
    observation = state.get("observation")
    if not isinstance(observation, Mapping):
        return _observation_budget_seconds()
    try:
        deadline = datetime.fromisoformat(str(observation.get("deadline")))
    except (TypeError, ValueError):
        return 0.0
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (deadline - datetime.now(timezone.utc)).total_seconds()


def _wait_bounded(process: Any, remaining: float) -> int:
    """Wait for an owned Oracle process, killing it at the observation deadline."""
    try:
        return int(process.wait(timeout=max(remaining, 0.0)))
    except subprocess.TimeoutExpired:
        for stop in ("terminate", "kill"):
            try:
                getattr(process, stop)()
                process.wait(timeout=10)
                break
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                break
        raise ExecutionError(
            "OBSERVATION_TIMEOUT",
            "the Oracle process outlived the observation deadline and was stopped; the browser and run directory are preserved",
            {"remaining_seconds": remaining},
        )


def _phase_for(state: Mapping[str, Any]) -> str:
    if state.get("status") == "captured":
        return "captured"
    submission = state.get("submission")
    if submission == "not_observed":
        return "failed_before_submit"
    if submission == "unknown":
        return "submission_unknown"
    if state.get("capture") == "durable":
        return "saved_unverified"
    return "awaiting_response"


def brief_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compress a runner payload to what an agent needs to decide the next step."""
    if payload.get("status") == "dry-run":
        preview = payload.get("workspace_project")
        return {
            "ok": payload.get("ok"),
            "status": "dry-run",
            "run_dir": payload.get("run_dir"),
            "resubmit": payload.get("resubmit", False),
            **({"workspace_project": {
                "mapped": preview.get("mapped"),
                "url": preview.get("url"),
                "bootstrap_required": preview.get("bootstrap_required"),
            }} if isinstance(preview, Mapping) else {}),
        }
    state = payload.get("result")
    if not isinstance(state, Mapping):
        return dict(payload)
    phase = str(state.get("phase") or _phase_for(state))
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), Mapping) else {}
    observation = state.get("observation") if isinstance(state.get("observation"), Mapping) else {}
    binding = (state.get("oracle") or {}).get("binding") if isinstance(state.get("oracle"), Mapping) else None
    conversation_url = binding.get("conversation_url") if isinstance(binding, Mapping) else None
    run_dir = str(payload.get("run_dir") or "")
    budget_left = (
        int(observation.get("reconnects") or 0) < int(observation.get("max_reconnects") or MAX_RECONNECTS)
        and _observation_remaining(state) > 0
    )
    if phase == "captured":
        next_action = "read output"
    elif phase == "failed_before_submit":
        next_action = "nothing was submitted; fix the error and execute again"
    elif phase == "saved_unverified":
        next_action = "output is saved; review model_check before trusting it"
    elif budget_left:
        next_action = f"reconnect --run-dir {run_dir}"
    else:
        next_action = "observation budget exhausted; inspect conversation_url manually before any resubmit"
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "phase": phase,
        "run_id": state.get("run_id"),
        "run_dir": run_dir,
        "output": artifacts.get("output") if state.get("capture") == "durable" else None,
        "output_bytes": artifacts.get("output_bytes", 0),
        "verified": bool((state.get("model_check") or {}).get("verified")),
        "conversation_url": conversation_url,
        "error_code": state.get("error_code"),
        "error": state.get("error"),
        "next_action": next_action,
    }


def _initial_state(
    config: ExecutionConfig,
    run_dir: Path,
    slug: str,
    output_path: Path,
    command: Sequence[str],
    *,
    cdp_port: int,
) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "run_id": config.run_id,
        "source_thread_id": config.source_thread_id,
        "session_id": config.session_id,
        "project_root": str(config.project_root),
        "approved_roots": [str(config.project_root)],
        "mission": {"path": str(config.mission_path), "sha256": config.mission_sha256},
        "selection": {
            "model": config.model,
            "effort": config.effort,
            "app_name": config.app_name,
            "requested_model": config.model,
            "model_strategy": _model_strategy(config.model),
        },
        "status": "prepared",
        "submission": "not_observed",
        "capture": "absent",
        "semantic_outcome": "unknown",
        "model_check": {"verified": False, "source": None},
        "oracle": {
            "version": RUNTIME.SUPPORTED_VERSION,
            "command": list(command),
            "slug": slug,
            "copy_profile": str(config.copy_profile),
            "manual_login_profile": str(run_dir / "browser-profile"),
            "expected_cdp_port": cdp_port,
            "binding": None,
        },
        "artifacts": {
            "output": str(output_path),
            "stdout": str(run_dir / "stdout.log"),
            "stderr": str(run_dir / "stderr.log"),
            "output_sha256": None,
            "output_bytes": 0,
        },
        "tab_close": {"status": "not_attempted"},
        "recovery": {"kind": "same-session-only", "resubmit": False},
        "observation": _new_observation(),
        "phase": "preparing",
        "timeline": {"prepared": _utc_now()},
    }


def _unresolved_duplicate(config: ExecutionConfig) -> dict[str, Any] | None:
    if not config.run_root.is_dir():
        return None
    for state_path in sorted(config.run_root.glob("*/state.json")):
        try:
            state = _load_state(state_path)
        except ExecutionError as exc:
            # This run root is the submission-ownership boundary.  An
            # unreadable state cannot prove that an earlier submission ended,
            # so fail closed and require attention to that exact run instead
            # of silently permitting a replacement send.
            return {
                "run_dir": str(state_path.parent),
                "status": "state_unreadable",
                "submission": "unknown",
                "state_error_code": exc.code,
            }
        if (
            state.get("project_root") == str(config.project_root)
            and state.get("source_thread_id") == config.source_thread_id
            and state.get("session_id") == config.session_id
            and state.get("status") in UNRESOLVED_STATUSES
            and state.get("submission") in {"unknown", "observed"}
            and state.get("capture") != "durable"
        ):
            return {"run_dir": str(state_path.parent), "status": state.get("status"), "submission": state.get("submission")}
    return None


@contextmanager
def _exclusive_file_lock(
    lock_path: Path,
    *,
    timeout_seconds: float,
    timeout_code: str,
    timeout_message: str,
) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise ExecutionError(timeout_code, timeout_message)
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _submit_lock(config: ExecutionConfig, timeout_seconds: float = 30.0) -> Iterator[None]:
    lock_key = hashlib.sha256(
        (str(config.project_root).casefold() + "\0" + str(config.source_thread_id or "local")).encode("utf-8")
    ).hexdigest()
    lock_path = config.run_root / ".locks" / f"{lock_key}.lock"
    with _exclusive_file_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        timeout_code="SUBMIT_LOCK_TIMEOUT",
        timeout_message="another execution owns this task/root scope",
    ):
        yield


@contextmanager
def _workspace_project_lock(config: ExecutionConfig, path: Path, timeout_seconds: float = 120.0) -> Iterator[None]:
    lock_key = hashlib.sha256(str(config.project_root).casefold().encode("utf-8")).hexdigest()[:24]
    lock_path = path.parent / ".locks" / f"workspace-project-{lock_key}.lock"
    with _exclusive_file_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        timeout_code="WORKSPACE_PROJECT_LOCK_TIMEOUT",
        timeout_message="another execution is initializing this workspace ChatGPT Project",
    ):
        yield


@contextmanager
def _exact_run_lock(run_dir: Path) -> Iterator[None]:
    lock_path = run_dir / ".reconnect.lock"
    handle = lock_path.open("a+b")
    acquired = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise ExecutionError("RUN_RECONNECT_ACTIVE", "another reconnect already owns this exact run") from exc
        yield
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87  # Unknown/access-denied is not proof of exit.
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": 0x08000000, "startupinfo": startup}


def _reserve_cdp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _resolve_version(command: Sequence[str], run_factory: Callable[..., Any] = subprocess.run) -> str:
    completed = run_factory(
        [*command, "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
        **_subprocess_kwargs(),
    )
    text = f"{getattr(completed, 'stdout', '') or ''}\n{getattr(completed, 'stderr', '') or ''}"
    if int(getattr(completed, "returncode", 1)) != 0 or RUNTIME.SUPPORTED_VERSION not in text:
        raise ExecutionError("ORACLE_VERSION_INVALID", "installed Oracle does not match the supported version")
    return f"oracle {RUNTIME.SUPPORTED_VERSION}"


def _session_meta(slug: str) -> tuple[Path, dict[str, Any]] | None:
    session_root = Path(os.environ.get("ORACLE_SESSION_ROOT") or (Path.home() / ".oracle" / "sessions")).expanduser().resolve()
    candidate = session_root / slug / "meta.json"
    if candidate.is_symlink() or candidate.parent.is_symlink():
        return None
    path = candidate.resolve()
    if not _is_within(session_root, path) or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return (path, payload) if isinstance(payload, dict) else None


def _binding_from_meta(meta_path: Path, meta: Mapping[str, Any]) -> dict[str, Any] | None:
    browser = meta.get("browser") if isinstance(meta.get("browser"), dict) else {}
    runtime = browser.get("runtime") if isinstance(browser.get("runtime"), dict) else {}
    host = str(runtime.get("chromeHost") or "").strip()
    target_id = str(runtime.get("chromeTargetId") or "").strip()
    try:
        port = int(runtime.get("chromePort"))
    except (TypeError, ValueError):
        port = 0
    if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535 or TARGET_ID_RE.fullmatch(target_id) is None:
        return None
    return {
        "session_meta_path": str(meta_path),
        "session_status": str(meta.get("status") or "").strip().casefold(),
        "host": host,
        "port": port,
        "target_id": target_id,
        "conversation_url": str(runtime.get("tabUrl") or "").strip() or None,
        "prompt_submitted": runtime.get("promptSubmitted") is True,
    }


def _clean_lines(path: Path) -> list[str]:
    try:
        return [ANSI_RE.sub("", line).strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return []


def _selected_latest_row(rows: Any) -> bool:
    if not isinstance(rows, list):
        return False
    selected = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("visible") is True
        and str(row.get("checked") or "").casefold() == "true"
    ]
    return len(selected) == 1 and re.sub(r"\s+", "", str(selected[0].get("text") or "")).casefold() in {"latest", "최신"}


def observed_model_check(stdout_path: Path, *, model: str, effort: str) -> dict[str, Any]:
    lines = _clean_lines(stdout_path)
    expected_ordinal = 5 if effort == "pro" else 4
    if model in SUPPORTED_MODELS:
        model_line = next((line for line in reversed(lines) if MODEL_EVIDENCE_PREFIX in line), "")
        thinking_line = next((line for line in reversed(lines) if THINKING_EVIDENCE_PREFIX in line), "")

        def fields(line: str, prefix: str) -> dict[str, str]:
            payload = line.split(prefix, 1)[1].strip() if prefix in line else ""
            return {
                key.strip(): value.strip()
                for part in payload.split(";")
                if "=" in part
                for key, value in [part.split("=", 1)]
            }

        model_evidence = fields(model_line, MODEL_EVIDENCE_PREFIX)
        thinking_evidence = fields(thinking_line, THINKING_EVIDENCE_PREFIX)
        expected_effort_labels = (
            {"pro"}
            if effort == "pro"
            else EXTENDED_EFFORT_LABELS
            if effort == "extended"
            else {"extrahigh", "sehrhoch", "非常に高い", "極高", "极高", "매우높음"}
        )
        native_verified = bool(
            # Oracle normalizes the public 'latest' alias before formatting logs.
            model_evidence.get("requestedKey", "").casefold() in (
                {"latest", "gpt-6-astra"} if model == "latest" else {"gpt-5.6-sol"}
            )
            and re.sub(r"\s+", "", model_evidence.get("target", "")).casefold() == (
                "latest" if model == "latest" else "gpt-5.6sol"
            )
            and model_evidence.get("resolvedLabel", "").strip() in (
                {"Latest", "最新", "최신"} if model == "latest" else {"GPT-5.6 Sol"}
            )
            and model_evidence.get("status") in {"already-selected", "switched"}
            and model_evidence.get("strategy") == "select"
            and model_evidence.get("verified") == "yes"
            and model_evidence.get("source") == "chatgpt-model-picker"
            and thinking_evidence.get("requestedLevel") == effort
            and thinking_evidence.get("status") in {"already-selected", "switched"}
            and re.sub(r"\s+", "", thinking_evidence.get("resolvedLabel", "")).casefold() in expected_effort_labels
            and thinking_evidence.get("verified") == "yes"
            and thinking_evidence.get("source") == "chatgpt-thinking-picker"
        )
        if native_verified:
            return {
                "verified": True,
                "model": model,
                "actual_model": "6 Pro" if model == "latest" and effort == "pro" else None,
                "effort": effort,
                "source": "oracle-native-selection-log",
            }

    if model == "latest":
        # Historical patched Oracle releases emitted one combined DOM proof.
        # Keep accepting it for recovery runs, after preferring 0.20's native evidence.
        for line in reversed(lines):
            if PICKER_PROOF_PREFIX not in line:
                continue
            try:
                proof = json.loads(line.split(PICKER_PROOF_PREFIX, 1)[1].strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(proof, dict):
                continue
            slider = proof.get("slider") if isinstance(proof, dict) and isinstance(proof.get("slider"), dict) else {}
            composer = proof.get("composer") if isinstance(proof, dict) and isinstance(proof.get("composer"), dict) else {}
            signals = proof.get("modelSignals") if isinstance(proof.get("modelSignals"), list) else []
            composer_label = re.sub(r"\s+", "", str(composer.get("text") or "")).casefold()
            pro_visible = composer.get("visible") is True and (
                composer_label == "6pro" or (
                    composer_label in {"thinkingeffort", "추론수준", "사고수준", "성능", "pro"}
                    and any(isinstance(signal, dict) and signal.get("visible") is True
                            and re.sub(r"\s+", "", str(signal.get("text") or "")).casefold() == "6pro"
                            for signal in signals)
                )
            )
            verified = bool(
                proof.get("schema") == "codex.oracle.picker-dom-proof/v1"
                and proof.get("latestClicked") is True
                and isinstance(proof.get("stableReads"), int) and proof["stableReads"] >= 2
                and _selected_latest_row(proof.get("modelRows"))
                and slider.get("visible") is True
                and slider.get("ordinal") == expected_ordinal
                and slider.get("total") == 5
                and slider.get("displayOrdinal") == expected_ordinal
                and slider.get("displayTotal") == 5
                and (
                    effort != "pro"
                    or pro_visible
                )
            )
            return {"verified": verified, "model": model, "actual_model": "6 Pro" if verified and effort == "pro" else None,
                    "effort": effort, "source": "oracle-picker-dom-log"}
        return {"verified": False, "model": model, "effort": effort, "source": None}

    evidence_line = next((line for line in reversed(lines) if MODEL_EVIDENCE_PREFIX in line), "")
    thinking_line = next((line for line in reversed(lines) if THINKING_PREFIX in line), "")
    compact_evidence = re.sub(r"\s+", "", evidence_line).casefold()
    compact_thinking = re.sub(r"\s+", "", thinking_line).casefold()
    model_verified = all(
        token in compact_evidence
        for token in ("resolvedlabel=gpt-5.6sol", "strategy=select", "verified=yes")
    )
    thinking_label = compact_thinking.split("thinkingtime:", 1)[1] if "thinkingtime:" in compact_thinking else ""
    effort_verified = (
        (effort == "pro" and ("pro,5of5" in compact_thinking or compact_thinking.endswith(":pro")))
        or (effort == "extra-high" and ("extrahigh" in compact_thinking or "4of5" in compact_thinking))
        # startswith keeps "extrahigh" from satisfying an "extended"/High request.
        or (effort == "extended" and thinking_label.startswith(tuple(EXTENDED_EFFORT_LABELS)))
    )
    return {
        "verified": bool(model_verified and effort_verified),
        "model": model,
        "effort": effort,
        "source": "oracle-observed-selection-log" if model_verified and effort_verified else None,
    }


def _capture(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        return {"status": "absent", "sha256": None, "bytes": 0}
    with path.open("r+b") as handle:
        data = handle.read()
        os.fsync(handle.fileno())
    if not data.strip():
        return {"status": "absent", "sha256": None, "bytes": len(data)}
    return {"status": "durable", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _urlopen_json(url: str, opener: Callable[..., Any]) -> Any:
    with opener(urllib.request.Request(url, method="GET"), timeout=5) as response:
        return json.loads(response.read().decode("utf-8", errors="strict"))


def close_owned_tab(binding: Mapping[str, Any], *, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    host = str(binding.get("host") or "")
    port = int(binding.get("port") or 0)
    target_id = str(binding.get("target_id") or "")
    if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535 or TARGET_ID_RE.fullmatch(target_id) is None:
        return {"status": "invalid-binding"}
    authority = f"http://{'[::1]' if host == '::1' else host}:{port}"
    try:
        before = _urlopen_json(f"{authority}/json/list", opener)
        targets = [
            item
            for item in before
            if isinstance(item, dict)
            and (item.get("id") == target_id or item.get("targetId") == target_id)
            and item.get("type") == "page"
        ]
        if not targets:
            return {"status": "already-closed", "target_id": target_id}
        expected_url = str(binding.get("conversation_url") or "")
        actual_url = str(targets[0].get("url") or "")
        if not expected_url or actual_url != expected_url:
            return {"status": "binding-mismatch", "target_id": target_id}
        with opener(
            urllib.request.Request(f"{authority}/json/close/{urllib.parse.quote(target_id, safe='')}", method="GET"),
            timeout=5,
        ) as response:
            response.read()
        after = _urlopen_json(f"{authority}/json/list", opener)
        if any(
            isinstance(item, dict)
            and (item.get("id") == target_id or item.get("targetId") == target_id)
            and item.get("type") == "page"
            for item in after
        ):
            return {"status": "close-unconfirmed", "target_id": target_id}
        return {"status": "closed", "target_id": target_id}
    except Exception as exc:
        return {"status": "close-failed", "target_id": target_id, "error": str(exc)}


def _finalize_capture(
    state_path: Path,
    state: dict[str, Any],
    *,
    stdout_path: Path,
    output_path: Path,
    tab_closer: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    session = _session_meta(str((state.get("oracle") or {}).get("slug") or ""))
    binding = _binding_from_meta(*session) if session else None
    expected_port = int((state.get("oracle") or {}).get("expected_cdp_port") or 0)
    preflight = state.get("personalization_preflight")
    if isinstance(preflight, dict):
        try:
            preflight_port = int(preflight.get("port") or 0)
        except (TypeError, ValueError):
            preflight_port = 0
        preflight_target = str(preflight.get("target_id") or "")
        project_url = str(preflight.get("project_url") or "").strip()
        workspace_project = state.get("workspace_project")
        project_binding_invalid = False
        if project_url:
            project_binding_invalid = bool(
                not _is_exact_workspace_project_url(project_url)
                or not isinstance(workspace_project, Mapping)
                or str(workspace_project.get("url") or "") != project_url
                or str(preflight.get("conversation_url") or "") != project_url
            )
        if binding and (
            binding.get("port") != expected_port
            or binding.get("port") != preflight_port
            or binding.get("target_id") != preflight_target
            or not _is_chatgpt_session_url(binding.get("conversation_url"))
            or project_binding_invalid
        ):
            binding = None
    elif binding and binding.get("port") != expected_port:
        binding = None
    capture = _capture(output_path)
    selection = state["selection"]
    model_check = observed_model_check(stdout_path, model=selection["model"], effort=selection["effort"])
    if binding and binding.get("prompt_submitted"):
        submission = "observed"
    elif binding:
        submission = "not_observed"
    else:
        submission = str(state.get("submission") or "unknown")
    oracle_terminal = bool(binding and binding.get("session_status") in TERMINAL_ORACLE_STATES)
    oracle_completed = bool(binding and binding.get("session_status") == "completed")
    captured = capture["status"] == "durable" and model_check["verified"] and oracle_terminal
    close_ready = bool(
        binding
        and submission == "observed"
        and capture["status"] == "durable"
        and oracle_completed
    )
    state.update(
        {
            "status": "captured" if captured else "attention_required",
            "submission": submission,
            "capture": capture["status"],
            "semantic_outcome": "unknown",
            "model_check": model_check,
        }
    )
    state["phase"] = _phase_for(state)
    _stamp(state, "finalized")
    state["oracle"]["binding"] = binding
    state["artifacts"].update(
        {"output_sha256": capture["sha256"], "output_bytes": capture["bytes"]}
    )
    # Persist the complete capture evidence before touching the browser target.
    _write_json_atomic(state_path, state)
    if not close_ready or binding is None or isinstance(preflight, dict):
        return state
    close_result = tab_closer(binding)
    state["tab_close"] = close_result
    if close_result.get("status") not in {"closed", "already-closed"}:
        state["status"] = "attention_required"
    _write_json_atomic(state_path, state)
    return state


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("CODEX_ORACLE_TEMPORARY_PERSONALIZATION", None)
    environment.pop("CODEX_ORACLE_TEMPORARY_PERSONALIZATION_HELPER", None)
    for key in (
        "ORACLE_TASK_OUTCOME_TERMINAL_CONTRACT",
        "ORACLE_TERMINAL_MARKER_CONFIRM_CYCLES",
        "ORACLE_TERMINAL_MARKER_MIN_STABLE_MS",
    ):
        environment.pop(key, None)
    return environment


def _start_personalized_browser(
    command: Sequence[str],
    profile_path: Path,
    cdp_port: int,
    *,
    chatgpt_url: str = CHATGPT_URL,
    project_bootstrap: Mapping[str, str] | None = None,
    run_factory: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    if len(command) < 2:
        raise ExecutionError("ORACLE_COMMAND_INVALID", "the resolved Oracle command has no package entry point")
    node = Path(command[0]).expanduser().resolve()
    entry = Path(command[1]).expanduser().resolve()
    package_root = entry.parents[2]
    helper = Path(__file__).with_name("oracle_temporary_personalization_preflight.mjs").resolve()
    argv = [
        str(node),
        str(helper),
        str(package_root),
        str(Path(__file__).with_name("oracle_temporary_personalization.mjs").resolve()),
        str(profile_path.resolve()),
        str(cdp_port),
        chatgpt_url,
    ]
    if project_bootstrap is not None:
        argv.append(json.dumps(dict(project_bootstrap), ensure_ascii=False, separators=(",", ":")))
    completed = run_factory(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
        **_subprocess_kwargs(),
    )
    expected_project_url = None
    if project_bootstrap is None and _is_exact_workspace_project_url(chatgpt_url):
        expected_project_url = _normalize_project_url(chatgpt_url)
    if completed.returncode != 0:
        detail_text = (completed.stderr or completed.stdout).strip()[-1200:]
        try:
            failure = json.loads(detail_text)
        except json.JSONDecodeError:
            failure = None
        failure_code = str(failure.get("code") or "") if isinstance(failure, dict) else ""
        if failure_code.startswith("WORKSPACE_PROJECT_"):
            raise ExecutionError(
                failure_code,
                str(failure.get("error") or "ChatGPT Project initialization failed"),
                {"detail": detail_text},
            )
        if expected_project_url is not None:
            raise ExecutionError(
                "WORKSPACE_PROJECT_PREFLIGHT_FAILED",
                "workspace Project browser preflight failed before submission",
                {"detail": detail_text, "project_url": expected_project_url},
            )
        raise ExecutionError(
            "TEMPORARY_PERSONALIZATION_UNCONFIRMED",
            "temporary-chat personalization could not be confirmed before submission",
            {"detail": detail_text},
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ExecutionError("TEMPORARY_PERSONALIZATION_UNCONFIRMED", "personalization preflight returned invalid evidence") from exc
    if (
        not isinstance(result, dict)
        or result.get("ok") is not True
        or int(result.get("port") or 0) != cdp_port
        or int(result.get("pid") or 0) <= 0
        or not TARGET_ID_RE.fullmatch(str(result.get("target_id") or ""))
        or not str(result.get("conversation_url") or "").startswith("https://chatgpt.com/")
        or not str(result.get("browser_ws") or "").startswith(f"ws://127.0.0.1:{cdp_port}/")
    ):
        code = "WORKSPACE_PROJECT_PREFLIGHT_FAILED" if expected_project_url is not None or project_bootstrap is not None else "TEMPORARY_PERSONALIZATION_UNCONFIRMED"
        raise ExecutionError(code, "browser preflight evidence is incomplete")
    if project_bootstrap is None:
        if str(result.get("conversation_url") or "") != chatgpt_url:
            raise ExecutionError("WORKSPACE_PROJECT_URL_UNCONFIRMED", "browser did not remain on the requested ChatGPT URL")
        if expected_project_url is not None:
            if _normalize_project_url(result.get("project_url")) != expected_project_url:
                raise ExecutionError("WORKSPACE_PROJECT_URL_UNCONFIRMED", "browser did not bind the exact workspace Project URL")
            if result.get("personalization") != "not-applicable":
                raise ExecutionError("WORKSPACE_PROJECT_PREFLIGHT_FAILED", "workspace Project run unexpectedly used temporary-chat personalization")
    else:
        project_url = _normalize_project_url(result.get("project_url"))
        if str(result.get("conversation_url") or "") != project_url:
            raise ExecutionError("WORKSPACE_PROJECT_URL_UNCONFIRMED", "browser did not confirm the initialized workspace Project page")
        if result.get("instructions_verified") is not True:
            warning = result.get("instructions_warning")
            if (
                not isinstance(warning, dict)
                or warning.get("code") != "WORKSPACE_PROJECT_INSTRUCTIONS_FAILED"
                or not str(warning.get("error") or "").strip()
            ):
                raise ExecutionError(
                    "WORKSPACE_PROJECT_INSTRUCTIONS_FAILED",
                    "workspace Project instructions were not verified and no best-effort warning evidence was returned",
                )
    return result


def _cleanup_personalized_browser(
    preflight: Mapping[str, Any],
    command: Sequence[str],
    *,
    expected_url: str | None = None,
    run_factory: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    binding = {
        "port": int(preflight.get("port") or 0),
        "target_id": str(preflight.get("target_id") or ""),
        "conversation_url": str(expected_url or preflight.get("conversation_url") or ""),
    }
    helper = Path(__file__).with_name("oracle_temporary_personalization_preflight.mjs").resolve()
    completed = run_factory(
        [str(Path(command[0]).expanduser().resolve()), str(helper), "--close", str(binding["port"]),
         str(preflight.get("browser_ws") or ""), binding["target_id"], binding["conversation_url"]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=15,
        **_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        return {"status": "browser-close-unconfirmed",
                "detail": (completed.stderr or completed.stdout).strip()[-800:]}
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "browser-close-unconfirmed", "detail": "cleanup helper returned invalid evidence"}
    if not isinstance(result, dict) or result.get("ok") is not True or result.get("closed") is not True:
        return {"status": "browser-close-unconfirmed", "detail": "cleanup helper did not confirm browser closure"}
    return {"status": "closed", "target_id": binding["target_id"]}


def _prepare_run_profile(config: ExecutionConfig, run_dir: Path) -> Path:
    """Copy the seed into an owned, retained profile; never pass Oracle copy-profile."""
    destination = run_dir / "browser-profile"
    excluded = {"Cache", "Code Cache", "GPUCache", "ShaderCache", "Crashpad", "Sessions",
                "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort", "LOCK"}

    def ignore(directory: str, names: list[str]) -> list[str]:
        ignored = []
        for name in names:
            candidate = Path(directory) / name
            attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
            if name in excluded or candidate.is_symlink() or attributes & 0x400:
                ignored.append(name)
        return ignored

    def native_path(path: Path) -> Path:
        value = str(path.absolute())
        if os.name != "nt" or value.startswith("\\\\?\\"):
            return path
        return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)

    copy_destination = native_path(destination)
    shutil.copytree(native_path(config.copy_profile), copy_destination, ignore=ignore)
    for preferences in copy_destination.glob("*/Preferences"):
        value = json.loads(preferences.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(
            key in value and not isinstance(value[key], dict) for key in ("profile", "session")
        ):
            raise ExecutionError("PROFILE_PREFERENCES_INVALID", "copied startup preferences are invalid")
        value.setdefault("profile", {}).update(exit_type="Normal", exited_cleanly=True)
        value.setdefault("session", {}).update(restore_on_startup=5, startup_urls=[])
        _write_json_atomic(preferences, value)
    return destination


def _workspace_project_preview(config: ExecutionConfig) -> dict[str, Any]:
    path = _workspace_project_map_path(config)
    payload = _load_workspace_project_map(path)
    mapped = _mapped_workspace_project(config, payload)
    return {
        "name": _workspace_project_name(config),
        "run_id": config.run_id,
        "session_id": config.session_id,
        "key": _workspace_project_key(config),
        "map_path": str(path),
        "mapped": mapped is not None,
        "url": mapped["url"] if mapped else None,
        "bootstrap_required": mapped is None,
    }


def _open_workspace_project_browser(
    config: ExecutionConfig,
    command: Sequence[str],
    profile_path: Path,
    cdp_port: int,
    browser_preflight: Callable[..., dict[str, Any]],
    browser_cleanup: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, str]]:
    map_path = _workspace_project_map_path(config)
    with _workspace_project_lock(config, map_path):
        payload = _load_workspace_project_map(map_path)
        mapped = _mapped_workspace_project(config, payload)
        bootstrap_evidence: dict[str, Any] | None = None
        if mapped is None:
            bootstrap = {
                "name": _workspace_project_name(config),
                "instructions": _workspace_project_instructions(config),
            }
            bootstrap_port = _reserve_cdp_port()
            bootstrap_preflight = browser_preflight(
                command,
                profile_path,
                bootstrap_port,
                chatgpt_url=CHATGPT_HOME_URL,
                project_bootstrap=bootstrap,
            )
            bootstrap_evidence = {
                "instructions_verified": bootstrap_preflight.get("instructions_verified") is True,
                **(
                    {"instructions_warning": bootstrap_preflight["instructions_warning"]}
                    if isinstance(bootstrap_preflight.get("instructions_warning"), dict)
                    else {}
                ),
            }
            mapped = _write_workspace_project_mapping(
                config,
                map_path,
                payload,
                url=str(bootstrap_preflight.get("project_url") or ""),
            )
            cleanup = browser_cleanup(
                bootstrap_preflight,
                command,
                expected_url=mapped["url"],
            )
            if cleanup.get("status") != "closed":
                raise ExecutionError(
                    "WORKSPACE_PROJECT_BOOTSTRAP_CLEANUP_UNCONFIRMED",
                    "workspace Project was initialized but its owned bootstrap browser could not be closed safely",
                    {"cleanup": cleanup, "project_url": mapped["url"]},
                )
        chatgpt_url = _workspace_project_chat_url(mapped["url"])
        preflight = browser_preflight(
            command,
            profile_path,
            cdp_port,
            chatgpt_url=chatgpt_url,
            project_bootstrap=None,
        )
        if bootstrap_evidence is not None:
            preflight = dict(preflight)
            preflight["workspace_project_bootstrap"] = bootstrap_evidence
        return preflight, mapped


def execute_config(
    config: ExecutionConfig,
    *,
    dry_run: bool = False,
    command_resolver: Callable[[], list[str]] = RUNTIME.resolve_default_oracle_command,
    version_resolver: Callable[[Sequence[str]], str] = _resolve_version,
    compat_factory: Callable[..., Mapping[str, Any]] = COMPAT.ensure_oracle_compatibility,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    tab_closer: Callable[[Mapping[str, Any]], dict[str, Any]] = close_owned_tab,
    browser_preflight: Callable[[Sequence[str], Path, int], dict[str, Any]] = _start_personalized_browser,
    browser_cleanup: Callable[..., dict[str, Any]] = _cleanup_personalized_browser,
) -> dict[str, Any]:
    logical_command = ["npx", "-y", f"@steipete/oracle@{RUNTIME.SUPPORTED_VERSION}"]
    run_dir = config.run_root / config.run_id
    output_path = run_dir / "output.md"
    slug = _slug(config)
    cdp_port = _reserve_cdp_port()
    if dry_run:
        project_preview = _workspace_project_preview(config)
        preview_url = CHATGPT_URL
        if project_preview["mapped"]:
            try:
                preview_url = _workspace_project_chat_url(str(project_preview["url"]))
            except ExecutionError as exc:
                project_preview["live_execution_blocker"] = exc.envelope()["error"]
        argv = build_oracle_argv(
            config,
            logical_command,
            output_path,
            slug,
            cdp_port=cdp_port,
            chatgpt_url=preview_url,
        )
        return {
            "ok": True,
            "status": "dry-run",
            "run_dir": str(run_dir),
            "contract": public_contract(config),
            "workspace_project": project_preview,
            "argv": _redacted_argv(argv),
            "writes_performed": False,
        }
    with _submit_lock(config):
        if not config.copy_profile.is_dir() or config.copy_profile.is_symlink():
            raise ExecutionError("SIGNED_IN_PROFILE_UNAVAILABLE", "the signed-in Oracle profile seed is unavailable or unsafe")
        # The manifest binds the user-authorized root; the chosen app enforces
        # its own access. Do not require an unrelated legacy DevSpace config or
        # write a qualification receipt for every ordinary mission.
        if not config.project_root.is_dir() or config.project_root.resolve() != config.project_root:
            raise ExecutionError("PROJECT_ROOT_UNAVAILABLE", "the exact project root changed before launch")
        if (
            config.mission_path.is_symlink()
            or config.mission_path.resolve() != config.mission_path
            or not _is_within(config.project_root, config.mission_path)
        ):
            raise ExecutionError("MISSION_OUTSIDE_APPROVED_ROOT", "mission_path changed or escaped project_root before launch")
        if _sha256(config.mission_path) != config.mission_sha256:
            raise ExecutionError("MISSION_CHANGED", "mission changed after configuration; prepare it again before submitting")
        duplicate = _unresolved_duplicate(config)
        if duplicate:
            raise ExecutionError(
                "RUN_RECONNECT_REQUIRED",
                "an unresolved execution already owns this task/root; reconnect it instead of resubmitting",
                duplicate,
            )
        if run_dir.exists():
            raise ExecutionError("RUN_ID_EXISTS", "run_id already exists", {"run_dir": str(run_dir)})
        command = command_resolver()
        version = version_resolver(command)
        compat_factory(version, **COMPAT.node_runtime_kwargs(command))
        run_dir.mkdir(parents=True, exist_ok=False)
        state_path = run_dir / "state.json"
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        state = _initial_state(config, run_dir, slug, output_path, command, cdp_port=cdp_port)
        _write_json_atomic(state_path, state)
        stdout_path.touch()
        stderr_path.touch()
        launch_attempted = False
        profile_prepared = False
        preflight: dict[str, Any] | None = None
        try:
            profile_path = _prepare_run_profile(config, run_dir)
            profile_prepared = True
            preflight, workspace_project = _open_workspace_project_browser(
                config, command, profile_path, cdp_port, browser_preflight, browser_cleanup
            )
            state["workspace_project"] = workspace_project
            state["personalization_preflight"] = preflight
            _stamp(state, "browser_ready")
            _write_json_atomic(state_path, state)
            argv = build_oracle_argv(
                config,
                command,
                output_path,
                slug,
                cdp_port=cdp_port,
                browser_tab=str(preflight["target_id"]),
                chatgpt_url=str(preflight["conversation_url"]),
            )
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = popen_factory(
                    argv,
                    cwd=str(config.project_root),
                    env=_child_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    **_subprocess_kwargs(),
                )
                launch_attempted = True
                state.update({"status": "running", "submission": "unknown", "phase": "submission_unknown",
                              "oracle_process_pid": getattr(process, "pid", None)})
                _stamp(state, "launched")
                _write_json_atomic(state_path, state)
                state["exit_code"] = _wait_bounded(process, _observation_remaining(state))
                _stamp(state, "exited")
        except Exception as exc:
            if preflight is not None and not launch_attempted:
                state["browser_cleanup"] = browser_cleanup(preflight, command)
            error_code = exc.code if isinstance(exc, ExecutionError) else None
            state.update({"status": "attention_required",
                          "submission": "unknown" if launch_attempted else "not_observed",
                          "failure_stage": (
                              "observation-timeout"
                              if error_code == "OBSERVATION_TIMEOUT"
                              else "oracle-launch-or-observation"
                              if launch_attempted
                              else "workspace-project-bootstrap"
                              if profile_prepared
                              else "profile-preparation"
                          ),
                          "error_code": error_code,
                          "error": str(exc),
                          # Keep the preflight/Oracle detail so a failure can be
                          # diagnosed from state.json without reproducing it.
                          "error_evidence": exc.evidence if isinstance(exc, ExecutionError) else None})
            state["phase"] = _phase_for(state)
            _stamp(state, "finalized")
            _write_json_atomic(state_path, state)
            return {"ok": False, "status": state["status"], "run_dir": str(run_dir), "result": state}
        state = _finalize_capture(
            state_path,
            state,
            stdout_path=stdout_path,
            output_path=output_path,
            tab_closer=tab_closer,
        )
        binding = state.get("oracle", {}).get("binding")
        cleanup_ready = bool(
            preflight is not None
            and isinstance(binding, Mapping)
            and state.get("submission") == "observed"
            and state.get("capture") == "durable"
            and binding.get("session_status") == "completed"
        )
        if cleanup_ready:
            cleanup = browser_cleanup(
                preflight,
                command,
                expected_url=str(binding["conversation_url"]),
            )
            state["browser_cleanup"] = cleanup
            if cleanup.get("status") != "closed":
                state["status"] = "attention_required"
            _write_json_atomic(state_path, state)
        return {"ok": state["status"] == "captured", "status": state["status"], "run_dir": str(run_dir), "result": state}


def execute_manifest(path: Path, **kwargs: Any) -> dict[str, Any]:
    return execute_config(load_manifest(path), **kwargs)


def reconnect_run(
    run_dir: Path,
    *,
    dry_run: bool = False,
    session_id: str | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    tab_closer: Callable[[Mapping[str, Any]], dict[str, Any]] = close_owned_tab,
    browser_cleanup: Callable[..., dict[str, Any]] = _cleanup_personalized_browser,
) -> dict[str, Any]:
    directory = _absolute_path(run_dir, label="run_dir", must_exist=True)
    state_path = directory / "state.json"
    if dry_run:
        return _reconnect_locked(
            directory, state_path, _load_state(state_path), dry_run=True, session_id=session_id,
            popen_factory=popen_factory, tab_closer=tab_closer,
            browser_cleanup=browser_cleanup,
        )
    with _exact_run_lock(directory):
        state = _load_state(state_path)
        return _reconnect_locked(
            directory,
            state_path,
            state,
            dry_run=dry_run,
            session_id=session_id,
            popen_factory=popen_factory,
            tab_closer=tab_closer,
            browser_cleanup=browser_cleanup,
        )


def _reconnect_locked(
    directory: Path,
    state_path: Path,
    state: dict[str, Any],
    *,
    dry_run: bool,
    popen_factory: Callable[..., Any],
    tab_closer: Callable[[Mapping[str, Any]], dict[str, Any]],
    browser_cleanup: Callable[..., dict[str, Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    owner = str(state.get("source_thread_id") or "").strip().casefold()
    current = str(os.environ.get("CODEX_THREAD_ID") or "").strip().casefold()
    if owner and owner != current:
        raise ExecutionError("FOREIGN_TASK_RUN", "only the owning Codex task may reconnect this execution")
    owner_session = str(state.get("session_id") or "")
    if owner_session and owner_session != (_resolve_session_id(session_id) or ""):
        # A run created inside one host session is never taken over by another
        # session, and never by an anonymous CLI call: pass --session-id.
        raise ExecutionError(
            "FOREIGN_SESSION_RUN",
            "only the owning host session may reconnect this execution; pass the same --session-id",
            {"owner_session_id": owner_session},
        )
    if state.get("status") == "captured":
        raise ExecutionError("RUN_ALREADY_CAPTURED", "captured executions do not need reconnect")
    if state.get("status") == "running" and _pid_alive(state.get("oracle_process_pid")):
        raise ExecutionError("RUN_STILL_ACTIVE", "the original Oracle process is still active; do not start another observer")
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    command = oracle.get("command")
    slug = str(oracle.get("slug") or "")
    output_path = Path(str((state.get("artifacts") or {}).get("output") or ""))
    if output_path != directory / "output.md" or output_path.is_symlink():
        raise ExecutionError("RUN_RECOVERY_BINDING_INVALID", "output must belong to the exact run directory")
    stdout_path = Path(str((state.get("artifacts") or {}).get("stdout") or ""))
    if stdout_path != directory / "stdout.log" or stdout_path.is_symlink():
        raise ExecutionError("RUN_RECOVERY_BINDING_INVALID", "model observation must belong to the exact run directory")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command) or not slug:
        raise ExecutionError("RUN_RECOVERY_BINDING_INVALID", "run has no exact Oracle command/slug binding")
    argv = [*command, "session", slug, "--live", "--write-output", str(output_path)]
    if "--prompt" in argv or "-p" in argv:
        raise ExecutionError("RECOVERY_PROMPT_FORBIDDEN", "reconnect must never contain a prompt")
    if dry_run:
        return {"ok": True, "status": "dry-run", "run_dir": str(directory), "argv": argv, "resubmit": False}
    # The observation budget belongs to the run, not to one observer process:
    # reconnecting never resets the deadline or the reconnect count.
    observation = state.get("observation")
    if not isinstance(observation, dict):
        observation = _new_observation()
        state["observation"] = observation
    reconnects = int(observation.get("reconnects") or 0)
    remaining = _observation_remaining(state)
    exhausted = (
        ("OBSERVATION_RECONNECTS_EXHAUSTED", f"reconnect budget of {observation.get('max_reconnects', MAX_RECONNECTS)} is used up")
        if reconnects >= int(observation.get("max_reconnects") or MAX_RECONNECTS)
        else ("OBSERVATION_DEADLINE_PASSED", "the run's observation deadline has passed")
        if remaining <= 0
        else None
    )
    if exhausted:
        state.update({"status": "attention_required", "error_code": exhausted[0], "error": exhausted[1]})
        state["phase"] = _phase_for(state)
        _stamp(state, "finalized")
        _write_json_atomic(state_path, state)
        return {"ok": False, "status": state["status"], "run_dir": str(directory), "result": state}
    observation["reconnects"] = reconnects + 1
    reconnect_stdout = directory / "reconnect-stdout.log"
    reconnect_stderr = directory / "reconnect-stderr.log"
    try:
        with reconnect_stdout.open("ab") as stdout, reconnect_stderr.open("ab") as stderr:
            process = popen_factory(
                argv,
                cwd=str(state["project_root"]),
                env=_child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                **_subprocess_kwargs(),
            )
            state.update({"status": "running", "reconnect_process_pid": getattr(process, "pid", None)})
            _stamp(state, f"reconnect_{reconnects + 1}_launched")
            _write_json_atomic(state_path, state)
            state["reconnect_exit_code"] = _wait_bounded(process, remaining)
            _stamp(state, f"reconnect_{reconnects + 1}_exited")
    except Exception as exc:
        state.update({"status": "attention_required",
                      "error_code": exc.code if isinstance(exc, ExecutionError) else None,
                      "error": str(exc)})
        state["phase"] = _phase_for(state)
        _stamp(state, "finalized")
        _write_json_atomic(state_path, state)
        return {"ok": False, "status": state["status"], "run_dir": str(directory), "result": state}
    # The original stdout contains the one model/effort proof; reconnect never
    # requests or repeats that check.
    state = _finalize_capture(
        state_path,
        state,
        stdout_path=Path(str(state["artifacts"]["stdout"])),
        output_path=output_path,
        tab_closer=tab_closer,
    )
    preflight = state.get("personalization_preflight")
    binding = state.get("oracle", {}).get("binding")
    cleanup_ready = bool(
        isinstance(preflight, dict)
        and isinstance(binding, Mapping)
        and state.get("submission") == "observed"
        and state.get("capture") == "durable"
        and binding.get("session_status") == "completed"
    )
    if cleanup_ready:
        cleanup = browser_cleanup(
            preflight,
            command,
            expected_url=str(binding["conversation_url"]),
        )
        state["browser_cleanup"] = cleanup
        if cleanup.get("status") != "closed":
            state["status"] = "attention_required"
        _write_json_atomic(state_path, state)
    return {"ok": state["status"] == "captured", "status": state["status"], "run_dir": str(directory), "result": state}
