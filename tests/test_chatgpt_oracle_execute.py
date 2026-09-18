from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_execute.py"


def load_module():
    name = "chatgpt_oracle_execute_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def executor():
    return load_module()


def fake_preflight(_command, _profile, port, *, chatgpt_url="https://chatgpt.com/?temporary-chat=true", project_bootstrap=None):
    project_url = "https://chatgpt.com/g/g-p-workspace123/project"
    is_project = project_bootstrap is not None or chatgpt_url == project_url
    conversation_url = project_url if project_bootstrap else chatgpt_url
    return {
        "ok": True,
        "pid": 1234,
        "port": port,
        "target_id": "B" * 32,
        "conversation_url": conversation_url,
        "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/test",
        "personalization": "not-applicable" if is_project else "enabled",
        **({"project_url": project_url} if is_project else {}),
        **({"project_created": True, "instructions_verified": True} if project_bootstrap else {}),
    }


def fake_cleanup(*_args, **_kwargs):
    return {"status": "closed"}


def seed_run_project(executor, config, url="https://chatgpt.com/g/g-p-workspace123/project"):
    path = executor._workspace_project_map_path(config)
    payload = executor._load_workspace_project_map(path)
    executor._write_workspace_project_mapping(config, path, payload, url=url)


def browser_port(argv):
    return int(argv[argv.index("--remote-chrome") + 1].rsplit(":", 1)[1])


def test_high_effort_is_forwarded_without_pro_upgrade(executor, execution_paths):
    root, mission, run_root, *_ = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root,
        model="gpt-5.6-sol", effort="extended",
    )
    argv = executor.build_oracle_argv(config, ["node", "oracle.js"], run_root / "output.md", "high-check")
    assert argv[argv.index("--browser-thinking-time") + 1] == "extended"


def test_process_probe_does_not_signal_on_windows(executor, monkeypatch):
    import os

    if os.name == "nt":
        monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("Windows probe must never signal"))
    assert executor._pid_alive(os.getpid()) is True
    assert executor._pid_alive(-1) is False


def test_reconnect_lock_rejects_overlap_and_releases(executor, tmp_path):
    with executor._exact_run_lock(tmp_path):
        with pytest.raises(executor.ExecutionError, match="another reconnect"):
            with executor._exact_run_lock(tmp_path):
                pytest.fail("overlapping reconnect acquired the lock")
    # A persistent lock file is harmless after its OS lock has been released.
    with executor._exact_run_lock(tmp_path):
        pass


@pytest.fixture
def execution_paths(tmp_path: Path, monkeypatch, executor):
    root = tmp_path / "project"
    root.mkdir()
    mission = root / "mission.md"
    mission.write_text("Implement the requested change.\n", encoding="utf-8")
    run_root = tmp_path / "host-state" / "runs"
    session_root = tmp_path / "oracle-sessions"
    profile = tmp_path / "signed-in-profile"
    profile.mkdir()
    monkeypatch.setenv("ORACLE_SESSION_ROOT", str(session_root))
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile))
    project_map = tmp_path / "host-state" / "workspace-projects.json"
    monkeypatch.setenv("CODEX_ORACLE_PROJECT_MAP_PATH", str(project_map))
    project_map.parent.mkdir(parents=True, exist_ok=True)
    project_map.write_text(json.dumps({
        "schema": executor.WORKSPACE_PROJECT_MAP_SCHEMA,
        "projects": {
            str(root): {
                "name": root.name,
                "url": "https://chatgpt.com/g/g-p-workspace123/project",
            }
        },
    }), encoding="utf-8")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    return root, mission, run_root, session_root


def test_config_binds_ambient_task_owner(executor, execution_paths, monkeypatch):
    root, mission, run_root, _ = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    assert config.source_thread_id == owner
    assert executor.manifest_payload(config)["source_thread_id"] == owner


def test_slug_survives_oracle_normalization(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    slug = executor._slug(config)
    assert 3 <= len(slug.split("-")) <= 5
    assert all(len(word) <= 10 for word in slug.split("-"))


def test_child_does_not_request_a_runtime_personalization_patch(executor, monkeypatch):
    monkeypatch.setenv("CODEX_ORACLE_TEMPORARY_PERSONALIZATION", "disabled")
    environment = executor._child_environment()
    assert "CODEX_ORACLE_TEMPORARY_PERSONALIZATION" not in environment
    assert "CODEX_ORACLE_TEMPORARY_PERSONALIZATION_HELPER" not in environment


def test_personalization_preflight_is_bound_before_remote_oracle(executor, tmp_path):
    node = tmp_path / "node.exe"
    entry = tmp_path / "oracle" / "dist" / "bin" / "oracle-cli.js"
    profile = tmp_path / "profile"
    entry.parent.mkdir(parents=True)
    profile.mkdir()
    node.write_bytes(b"node")
    entry.write_bytes(b"oracle")
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "ok": True, "pid": 1234, "port": 49152, "target_id": "B" * 32,
            "conversation_url": "https://chatgpt.com/?temporary-chat=true",
            "browser_ws": "ws://127.0.0.1:49152/devtools/browser/test",
        }))

    result = executor._start_personalized_browser([str(node), str(entry)], profile, 49152, run_factory=run)
    assert result["target_id"] == "B" * 32
    assert observed[0][0][-2:] == ["49152", "https://chatgpt.com/?temporary-chat=true"]


def test_mapped_project_preflight_failure_is_explicit_and_never_rewritten_as_temporary_chat(executor, tmp_path):
    node = tmp_path / "node.exe"
    entry = tmp_path / "oracle" / "dist" / "bin" / "oracle-cli.js"
    profile = tmp_path / "profile"
    entry.parent.mkdir(parents=True)
    profile.mkdir()
    node.write_bytes(b"node")
    entry.write_bytes(b"oracle")
    project_url = "https://chatgpt.com/g/g-p-workspace123/project"
    observed = []

    def run(argv, **kwargs):
        observed.append(argv)
        return SimpleNamespace(returncode=2, stdout="", stderr=json.dumps({
            "ok": False,
            "code": "WORKSPACE_PROJECT_TARGET_UNCONFIRMED",
            "error": "owned workspace Project startup target is unavailable",
        }))

    with pytest.raises(executor.ExecutionError) as exc:
        executor._start_personalized_browser(
            [str(node), str(entry)], profile, 49152, chatgpt_url=project_url, run_factory=run
        )

    assert exc.value.code == "WORKSPACE_PROJECT_TARGET_UNCONFIRMED"
    assert observed[0][-1] == project_url
    assert "temporary-chat=true" not in " ".join(observed[0])


def test_project_bootstrap_accepts_best_effort_instruction_warning(executor, tmp_path):
    node = tmp_path / "node.exe"
    entry = tmp_path / "oracle" / "dist" / "bin" / "oracle-cli.js"
    profile = tmp_path / "profile"
    entry.parent.mkdir(parents=True)
    profile.mkdir()
    node.write_bytes(b"node")
    entry.write_bytes(b"oracle")
    project_url = "https://chatgpt.com/g/g-p-workspace123/project"
    warning = {
        "code": "WORKSPACE_PROJECT_INSTRUCTIONS_FAILED",
        "error": "Project instructions save action is unavailable",
    }

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "ok": True,
            "pid": 1234,
            "port": 49152,
            "target_id": "B" * 32,
            "conversation_url": project_url,
            "project_url": project_url,
            "project_created": True,
            "instructions_verified": False,
            "instructions_warning": warning,
            "personalization": "not-applicable",
            "browser_ws": "ws://127.0.0.1:49152/devtools/browser/test",
        }))

    result = executor._start_personalized_browser(
        [str(node), str(entry)],
        profile,
        49152,
        chatgpt_url="https://chatgpt.com/",
        project_bootstrap={"name": "project", "instructions": "instructions"},
        run_factory=run,
    )
    assert result["project_url"] == project_url
    assert result["instructions_verified"] is False
    assert result["instructions_warning"] == warning


def test_cleanup_delegates_exact_browser_and_tab_identity_once(executor):
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout='{"ok":true,"closed":true}')

    preflight = fake_preflight([], Path(), 49152)
    expected_url = "https://chatgpt.com/c/owned-temporary-run"
    result = executor._cleanup_personalized_browser(
        preflight, ["node", "oracle"], expected_url=expected_url, run_factory=run
    )
    assert result == {"status": "closed", "target_id": "B" * 32}
    assert observed[0][0][-4:] == [
        "49152", preflight["browser_ws"], preflight["target_id"], expected_url
    ]


def test_changed_mission_is_rejected_before_launch(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    mission.write_text("changed after preparation", encoding="utf-8")
    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(config, command_resolver=lambda: pytest.fail("must not launch"))
    assert exc.value.code == "MISSION_CHANGED"
    assert not (run_root / config.run_id).exists()


def test_retained_profile_copy_preserves_seed_and_normalizes_only_copy(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    seed = config.copy_profile / "Default"
    seed.mkdir()
    original = json.dumps({"profile": {"exit_type": "Crashed"}, "session": {"restore_on_startup": 1}, "custom": 42}).encode()
    (seed / "Preferences").write_bytes(original)
    (seed / "Cookies").write_bytes(b"fixture-cookie-data")
    (seed / "Cache").mkdir()
    (seed / "Cache" / "cached").write_bytes(b"discardable cache")
    run_dir = run_root / config.run_id
    run_dir.mkdir(parents=True)
    copied = executor._prepare_run_profile(config, run_dir)
    assert (seed / "Preferences").read_bytes() == original
    assert (copied / "Default" / "Cookies").read_bytes() == b"fixture-cookie-data"
    assert not (copied / "Default" / "Cache").exists()
    preferences = json.loads((copied / "Default" / "Preferences").read_text())
    assert preferences["profile"]["exit_type"] == "Normal"
    assert preferences["profile"]["exited_cleanly"] is True
    assert preferences["session"] == {"restore_on_startup": 5, "startup_urls": []}
    assert preferences["custom"] == 42


def test_profile_is_required_only_for_live_execution(executor, execution_paths, monkeypatch, tmp_path):
    root, mission, run_root, _ = execution_paths
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path / "missing-profile"))
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    assert executor.execute_config(config, dry_run=True)["writes_performed"] is False
    assert not run_root.exists()
    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(config, command_resolver=lambda: pytest.fail("must not launch"))
    assert exc.value.code == "SIGNED_IN_PROFILE_UNAVAILABLE"


def test_profile_copy_failure_is_definitely_before_submission(executor, execution_paths, monkeypatch):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    def failed_copy(*args):
        raise OSError("profile copy failed")
    monkeypatch.setattr(executor, "_prepare_run_profile", failed_copy)
    result = executor.execute_config(config, command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0", compat_factory=lambda version: {},
        popen_factory=lambda *args, **kwargs: pytest.fail("must not start Oracle"))
    assert result["result"]["submission"] == "not_observed"
    assert result["result"]["failure_stage"] == "profile-preparation"


def test_profile_copy_handles_long_windows_destination(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    source = config.copy_profile / "Default" / "nested-profile-assets"
    source.mkdir(parents=True)
    name = "asset-" + "x" * 100 + ".txt"
    (source / name).write_bytes(b"fixture")
    run_dir = run_root / ("long-run-" + "x" * 60)
    copied = executor._prepare_run_profile(config, run_dir)
    target = copied / "Default" / "nested-profile-assets" / name
    if executor.os.name == "nt":
        target = Path("\\\\?\\" + str(target.absolute()))
    try:
        assert target.read_bytes() == b"fixture"
    finally:
        # tempfile cleanup does not use the Windows extended-length prefix.
        target.unlink()


def test_ordinary_execution_does_not_require_legacy_devspace_config(executor, execution_paths, monkeypatch):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    assert not hasattr(executor, "DEVSPACE_PREFLIGHT")
    def reached_command_resolution():
        raise RuntimeError("resolved without legacy setup")
    with pytest.raises(RuntimeError, match="resolved without legacy setup"):
        executor.execute_config(config, command_resolver=reached_command_resolution)
    assert not (run_root / config.run_id).exists()


def test_filesystem_root_is_not_an_approved_project(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    with pytest.raises(executor.ExecutionError) as exc:
        executor.make_config(project_root=Path(root.anchor), mission_path=mission, run_root=run_root)
    assert exc.value.code == "PROJECT_ROOT_TOO_BROAD"


def test_selected_node_is_forwarded_to_compatibility(executor, execution_paths, tmp_path, monkeypatch):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    node = tmp_path / "bundled node" / "node.exe"
    node.parent.mkdir()
    node.write_bytes(b"fixture")
    command = [str(node), str(tmp_path / "oracle-cli.js")]
    observed = []
    def verify(version, **kwargs):
        observed.append((version, kwargs))
        raise RuntimeError("stop before browser or profile copy")
    with pytest.raises(RuntimeError, match="stop before browser"):
        executor.execute_config(config, command_resolver=lambda: command,
            version_resolver=lambda value: "oracle 0.20.0", compat_factory=verify)
    assert observed == [("oracle 0.20.0", {"node_executable": str(node)})]
    assert not (run_root / config.run_id).exists()


def picker_proof(effort: str = "pro") -> dict:
    ordinal = 5 if effort == "pro" else 4
    return {
        "schema": "codex.oracle.picker-dom-proof/v1",
        "latestClicked": True,
        "stableReads": 2,
        "modelRows": [
            {
                "text": "Latest",
                "ariaLabel": None,
                "role": "menuitemradio",
                "checked": "true",
                "expanded": None,
                "visible": True,
            }
        ],
        "composer": {
            "text": "6 Pro" if effort == "pro" else "Thinking effort",
            "ariaLabel": None,
            "role": None,
            "checked": None,
            "expanded": "true",
            "visible": True,
        },
        "modelSignals": [],
        "slider": {
            "minimum": 0,
            "maximum": 4,
            "current": ordinal - 1,
            "ordinal": ordinal,
            "total": 5,
            "displayOrdinal": ordinal,
            "displayTotal": 5,
            "atMaximum": ordinal == 5,
            "text": f"{ordinal} of 5",
            "visible": True,
        },
    }


def native_latest_evidence(
    effort: str = "pro", model_label: str = "Latest", effort_label: str | None = None
) -> str:
    label = effort_label or ("Pro" if effort == "pro" else "Extra High")
    return (
        f"[browser] Model selection evidence: requestedKey=latest; target=latest; resolvedLabel={model_label}; "
        "status=already-selected; strategy=select; verified=yes; source=chatgpt-model-picker; capturedAt=now\n"
        f"[browser] Thinking effort evidence: requestedLevel={effort}; status=already-selected; "
        f"resolvedLabel={label}; verified=yes; failClosed={'yes' if effort == 'pro' else 'no'}; "
        "targetModelKind=(none); observedModelKind=(none); source=chatgpt-thinking-picker; capturedAt=now\n"
    )


def write_session_meta(
    session_root: Path,
    slug: str,
    *,
    status: str,
    submitted: bool = True,
    port: int = 43123,
    target_id: str = "B" * 32,
) -> dict:
    url = "https://chatgpt.com/c/owned-temporary-run"
    directory = session_root / slug
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "browser": {
            "runtime": {
                "chromeHost": "127.0.0.1",
                "chromePort": port,
                "chromeTargetId": target_id,
                "tabUrl": url,
                "promptSubmitted": submitted,
            }
        },
    }
    (directory / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
    return {"target_id": target_id, "conversation_url": url}


class Process:
    pid = 4242

    def __init__(self, code: int = 0):
        self.code = code

    def wait(self):
        return self.code


def test_manifest_is_minimal_and_rejects_retired_fields(executor, execution_paths, tmp_path: Path):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0001",
    )
    manifest = executor.manifest_payload(config)
    assert set(manifest) == {
        "schema", "project_root", "mission_path", "run_root", "run_id", "model", "effort", "app_name"
    }

    manifest["mode"] = "review"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(executor.ExecutionError) as exc:
        executor.load_manifest(path)
    assert exc.value.code == "MANIFEST_FIELDS_INVALID"


def test_dry_run_has_temp_personalized_route_and_no_writes(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    executor._workspace_project_map_path(
        executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    ).unlink()
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0002", model="latest", effort="pro",
        app_name="git-app",
    )
    result = executor.execute_config(config, dry_run=True)
    argv = result["argv"]

    assert result["writes_performed"] is False
    assert result["workspace_project"]["name"] == root.name
    assert result["workspace_project"]["run_id"] == config.run_id
    assert result["workspace_project"]["bootstrap_required"] is True
    assert not run_root.exists()
    assert not Path(result["workspace_project"]["map_path"]).exists()
    assert argv[argv.index("--model") + 1] == "latest"
    assert argv[argv.index("--browser-model-strategy") + 1] == "select"
    assert argv[argv.index("--browser-thinking-time") + 1] == "pro"
    assert argv[argv.index("--chatgpt-url") + 1] == "https://chatgpt.com/?temporary-chat=true"
    assert argv[argv.index("--browser-archive") + 1] == "never"
    assert "--remote-chrome" in argv
    assert argv[argv.index("--remote-chrome") + 1].startswith("127.0.0.1:")
    assert "--browser-keep-browser" not in argv
    assert "--browser-hide-window" not in argv
    assert "--copy-profile" not in argv
    assert "--browser-manual-login-profile-dir" not in argv
    assert "--browser-tab" not in argv
    assert argv[argv.index("--prompt") + 1] == "<mission-handoff>"
    assert "TASK_OUTCOME" not in " ".join(argv)


def test_workspace_project_bootstrap_is_mapped_once_and_reused_for_same_run_id(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-project-session-0001"
    )
    executor._workspace_project_map_path(config).unlink()
    profile = config.copy_profile
    calls = []
    cleanup_calls = []
    project_url = "https://chatgpt.com/g/g-p-workspace123/project"

    def preflight(command, profile_path, port, *, chatgpt_url, project_bootstrap):
        calls.append({"url": chatgpt_url, "bootstrap": project_bootstrap, "port": port})
        return {
            "ok": True,
            "pid": 1234,
            "port": port,
            "target_id": "B" * 32,
            "conversation_url": project_url if project_bootstrap else chatgpt_url,
            "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/test",
            "personalization": "not-applicable",
            "project_url": project_url,
            **({"project_created": True, "instructions_verified": True} if project_bootstrap else {}),
        }

    def cleanup(preflight, command, *, expected_url):
        cleanup_calls.append((preflight["port"], expected_url))
        return {"status": "closed", "target_id": preflight["target_id"]}

    first, first_mapping = executor._open_workspace_project_browser(
        config, ["oracle"], profile, 43123, preflight, cleanup
    )
    second, second_mapping = executor._open_workspace_project_browser(
        config, ["oracle"], profile, 43124, preflight, cleanup
    )

    assert first_mapping == {"name": root.name, "url": project_url}
    assert second_mapping == first_mapping
    assert calls[0]["url"] == "https://chatgpt.com/"
    assert calls[0]["bootstrap"]["name"] == root.name
    instructions = calls[0]["bootstrap"]["instructions"]
    assert str(root) in instructions
    assert "한국어" in instructions
    assert "AGENTS.md" in instructions
    assert "DevSpace" in instructions
    assert calls[1]["url"] == project_url
    assert calls[1]["bootstrap"] is None
    assert calls[1]["port"] == 43123
    assert calls[2] == {
        "url": project_url,
        "bootstrap": None,
        "port": 43124,
    }
    assert cleanup_calls == [(calls[0]["port"], project_url)]
    assert first["conversation_url"] == project_url
    assert second["conversation_url"] == project_url
    map_path = executor._workspace_project_map_path(config)
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema": executor.WORKSPACE_PROJECT_MAP_SCHEMA,
        "projects": {
            config.run_id: {
                "run_id": config.run_id,
                "workspace_root": str(root),
                "name": root.name,
                "url": project_url,
            }
        },
    }


def test_workspace_project_mapping_is_scoped_to_run_id_not_workspace_or_codex_thread(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    first_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-project-session-1001",
        source_thread_id=owner,
    )
    second_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-project-session-1002",
        source_thread_id=owner,
    )
    map_path = executor._workspace_project_map_path(first_config)
    map_path.unlink()
    created_urls = iter([
        "https://chatgpt.com/g/g-p-session1001/project",
        "https://chatgpt.com/g/g-p-session1002/project",
    ])
    calls = []

    def preflight(command, profile_path, port, *, chatgpt_url, project_bootstrap):
        calls.append((chatgpt_url, project_bootstrap))
        if project_bootstrap is not None:
            url = next(created_urls)
            return {
                "ok": True,
                "pid": 1234,
                "port": port,
                "target_id": "B" * 32,
                "conversation_url": url,
                "project_url": url,
                "project_created": True,
                "instructions_verified": True,
                "personalization": "not-applicable",
                "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/bootstrap",
            }
        return {
            "ok": True,
            "pid": 1235,
            "port": port,
            "target_id": "C" * 32,
            "conversation_url": chatgpt_url,
            "project_url": chatgpt_url,
            "personalization": "not-applicable",
            "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/run",
        }

    cleanup = lambda *_args, **_kwargs: {"status": "closed", "target_id": "B" * 32}
    _, first_mapping = executor._open_workspace_project_browser(
        first_config, ["oracle"], first_config.copy_profile, 43123, preflight, cleanup
    )
    _, second_mapping = executor._open_workspace_project_browser(
        second_config, ["oracle"], second_config.copy_profile, 43124, preflight, cleanup
    )
    _, first_reconnect_mapping = executor._open_workspace_project_browser(
        first_config, ["oracle"], first_config.copy_profile, 43125, preflight, cleanup
    )

    assert first_mapping["url"] == "https://chatgpt.com/g/g-p-session1001/project"
    assert second_mapping["url"] == "https://chatgpt.com/g/g-p-session1002/project"
    assert first_reconnect_mapping == first_mapping
    assert [bootstrap is not None for _, bootstrap in calls] == [True, False, True, False, False]
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    assert payload["projects"][first_config.run_id]["workspace_root"] == str(root)
    assert payload["projects"][second_config.run_id]["workspace_root"] == str(root)
    assert payload["projects"][first_config.run_id]["url"] != payload["projects"][second_config.run_id]["url"]


def test_historical_workspace_mapping_is_preserved_but_not_reused(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-project-session-2001"
    )
    map_path = executor._workspace_project_map_path(config)
    historical_url = "https://chatgpt.com/g/g-p-historical/project"
    new_url = "https://chatgpt.com/g/g-p-current2001/project"
    historical_entry = {"name": root.name, "url": historical_url}
    map_path.write_text(json.dumps({
        "schema": executor.WORKSPACE_PROJECT_MAP_SCHEMA,
        "projects": {str(root): historical_entry},
    }), encoding="utf-8")

    preview = executor._workspace_project_preview(config)
    assert preview["mapped"] is False
    assert preview["bootstrap_required"] is True
    assert preview["run_id"] == config.run_id

    def preflight(command, profile_path, port, *, chatgpt_url, project_bootstrap):
        url = new_url if project_bootstrap is not None else chatgpt_url
        return {
            "ok": True,
            "pid": 1234,
            "port": port,
            "target_id": "B" * 32,
            "conversation_url": url,
            "project_url": url,
            "personalization": "not-applicable",
            **({"project_created": True, "instructions_verified": True} if project_bootstrap else {}),
            "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/test",
        }

    _, mapping = executor._open_workspace_project_browser(
        config,
        ["oracle"],
        config.copy_profile,
        43123,
        preflight,
        lambda *_args, **_kwargs: {"status": "closed", "target_id": "B" * 32},
    )
    assert mapping["url"] == new_url
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    assert payload["projects"][str(root)] == historical_entry
    assert payload["projects"][config.run_id] == {
        "run_id": config.run_id,
        "workspace_root": str(root),
        "name": root.name,
        "url": new_url,
    }


def test_workspace_project_bootstrap_persists_instruction_warning_and_continues(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    executor._workspace_project_map_path(config).unlink()
    project_url = "https://chatgpt.com/g/g-p-workspace123/project"
    warning = {
        "code": "WORKSPACE_PROJECT_INSTRUCTIONS_FAILED",
        "error": "Project instructions editor is missing or ambiguous",
    }
    calls = []

    def preflight(command, profile_path, port, *, chatgpt_url, project_bootstrap):
        calls.append((chatgpt_url, project_bootstrap))
        if project_bootstrap is not None:
            return {
                "ok": True,
                "pid": 1234,
                "port": port,
                "target_id": "B" * 32,
                "conversation_url": project_url,
                "project_url": project_url,
                "project_created": True,
                "instructions_verified": False,
                "instructions_warning": warning,
                "personalization": "not-applicable",
                "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/bootstrap",
            }
        return {
            "ok": True,
            "pid": 1235,
            "port": port,
            "target_id": "C" * 32,
            "conversation_url": chatgpt_url,
            "project_url": chatgpt_url,
            "personalization": "not-applicable",
            "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/run",
        }

    preflight_result, mapping = executor._open_workspace_project_browser(
        config,
        ["oracle"],
        config.copy_profile,
        43123,
        preflight,
        lambda *_args, **_kwargs: {"status": "closed", "target_id": "B" * 32},
    )

    assert mapping == {"name": root.name, "url": project_url}
    assert calls[0][0] == "https://chatgpt.com/"
    assert calls[1] == (project_url, None)
    assert preflight_result["conversation_url"] == project_url
    assert preflight_result["workspace_project_bootstrap"] == {
        "instructions_verified": False,
        "instructions_warning": warning,
    }


def test_workspace_project_chat_url_uses_exact_regular_project_url():
    module = load_module()
    project_url = "https://chatgpt.com/g/g-p-workspace123/project"
    assert module._workspace_project_chat_url(project_url) == project_url
    assert module._workspace_project_chat_url(f"{project_url}/") == project_url


def test_live_project_bootstrap_maps_then_executes_regular_project_chat(tmp_path: Path, monkeypatch):
    module = load_module()
    root = tmp_path / "project"
    root.mkdir()
    mission = root / "mission.md"
    mission.write_text("Use the workspace Project.\n", encoding="utf-8")
    run_root = tmp_path / "runs"
    session_root = tmp_path / "oracle-sessions"
    profile = tmp_path / "signed-in-profile"
    profile.mkdir()
    project_map = tmp_path / "state" / "workspace-projects.json"
    monkeypatch.setenv("ORACLE_SESSION_ROOT", str(session_root))
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile))
    monkeypatch.setenv("CODEX_ORACLE_PROJECT_MAP_PATH", str(project_map))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    config = module.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="project-bootstrap-regular", model="latest", effort="pro",
    )
    project_url = "https://chatgpt.com/g/g-p-workspace123/project"
    preflight_calls = []
    cleanup_calls = []

    def preflight(command, profile_path, port, *, chatgpt_url, project_bootstrap):
        preflight_calls.append((chatgpt_url, project_bootstrap, port))
        if project_bootstrap is not None:
            assert chatgpt_url == "https://chatgpt.com/"
            assert project_bootstrap["name"] == root.name
            return {
                "ok": True,
                "pid": 1234,
                "port": port,
                "target_id": "B" * 32,
                "conversation_url": project_url,
                "project_url": project_url,
                "project_created": True,
                "instructions_verified": True,
                "personalization": "not-applicable",
                "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/bootstrap",
            }
        assert chatgpt_url == project_url
        return {
            "ok": True,
            "pid": 1235,
            "port": port,
            "target_id": "B" * 32,
            "conversation_url": project_url,
            "project_url": project_url,
            "personalization": "not-applicable",
            "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/run",
        }

    def cleanup(preflight_result, command, *, expected_url=None):
        cleanup_calls.append(expected_url or preflight_result["conversation_url"])
        return {"status": "closed", "target_id": preflight_result["target_id"]}

    def popen(argv, **kwargs):
        assert argv[argv.index("--chatgpt-url") + 1] == project_url
        output = Path(argv[argv.index("--write-output") + 1])
        output.write_text("Project answer.\n", encoding="utf-8")
        kwargs["stdout"].write(native_latest_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="completed",
            port=browser_port(argv),
        )
        return Process(0)

    result = module.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        tab_closer=lambda binding: pytest.fail("owned preflight browser cleanup must own Project tab closure"),
        browser_preflight=preflight,
        browser_cleanup=cleanup,
    )

    assert [call[0] for call in preflight_calls] == ["https://chatgpt.com/", project_url]
    assert cleanup_calls == [project_url, "https://chatgpt.com/c/owned-temporary-run"]
    assert result["status"] == "captured"
    assert result["result"]["submission"] == "observed"
    assert result["result"]["workspace_project"] == {"name": root.name, "url": project_url}
    payload = json.loads(project_map.read_text(encoding="utf-8"))
    assert payload["projects"][config.run_id] == {
        "run_id": config.run_id,
        "workspace_root": str(root),
        "name": root.name,
        "url": project_url,
    }


def test_workspace_project_bootstrap_failure_is_explicit_and_not_mapped(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    bootstrap_config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    executor._workspace_project_map_path(bootstrap_config).unlink()
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-project-logout"
    )
    calls = 0

    def logged_out(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise executor.ExecutionError("WORKSPACE_PROJECT_LOGGED_OUT", "ChatGPT is logged out")

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        browser_preflight=logged_out,
        popen_factory=lambda *args, **kwargs: pytest.fail("must not submit without a workspace Project"),
    )

    assert calls == 1
    assert result["status"] == "attention_required"
    assert result["result"]["submission"] == "not_observed"
    assert result["result"]["failure_stage"] == "workspace-project-bootstrap"
    assert result["result"]["error_code"] == "WORKSPACE_PROJECT_LOGGED_OUT"
    assert not executor._workspace_project_map_path(config).exists()


@pytest.mark.parametrize("effort", ["pro", "extra-high"])
def test_one_latest_model_check_covers_model_and_effort(executor, tmp_path: Path, effort: str):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        f"[browser] Picker DOM proof: {json.dumps(picker_proof(effort))}\n",
        encoding="utf-8",
    )
    check = executor.observed_model_check(stdout, model="latest", effort=effort)
    assert check == {
        "verified": True,
        "model": "latest",
        "actual_model": "6 Pro" if effort == "pro" else None,
        "effort": effort,
        "source": "oracle-picker-dom-log",
    }


@pytest.mark.parametrize(
    "effort,model_label,effort_label",
    [("pro", "Latest", "Pro"), ("extra-high", "최신", "매우 높음")],
)
def test_latest_model_check_prefers_native_020_evidence(
    executor, tmp_path: Path, effort: str, model_label: str, effort_label: str
):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(native_latest_evidence(effort, model_label, effort_label), encoding="utf-8")
    result = executor.observed_model_check(stdout, model="latest", effort=effort)
    assert result["verified"] is True
    assert result["source"] == "oracle-native-selection-log"


def test_latest_native_evidence_fails_closed_when_thinking_is_unverified(executor, tmp_path: Path):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        native_latest_evidence("extra-high").replace(
            "status=already-selected; resolvedLabel=Extra High; verified=yes",
            "status=unverified; resolvedLabel=(none); verified=no",
        ),
        encoding="utf-8",
    )
    assert executor.observed_model_check(stdout, model="latest", effort="extra-high")["verified"] is False


@pytest.mark.parametrize("key,verified", [("gpt-6-astra", True), ("gpt-5.6-sol", False)])
def test_native_latest_alias_uses_oracle_normalized_request_key(executor, tmp_path: Path, key: str, verified: bool):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(native_latest_evidence("extra-high").replace("requestedKey=latest;", f"requestedKey={key};"), encoding="utf-8")
    assert executor.observed_model_check(stdout, model="latest", effort="extra-high")["verified"] is verified


@pytest.mark.parametrize("label, verified", [("Thinking effort", True), ("추론 수준", True), ("5.6 Pro", False)])
def test_latest_pro_accepts_actual_menu_signal(executor, tmp_path, label, verified):
    proof = picker_proof()
    proof["composer"]["text"] = label
    proof["modelSignals"] = [{"text": "6Pro", "visible": True}]
    stdout = tmp_path / "stdout.log"
    stdout.write_text(executor.PICKER_PROOF_PREFIX + json.dumps(proof), encoding="utf-8")
    result = executor.observed_model_check(stdout, model="latest", effort="pro")
    assert result["verified"] is verified


def test_explicit_model_uses_observed_select_logs(executor, tmp_path: Path):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        "[browser] Model selection evidence: requestedKey=gpt-5.6-sol; target=GPT-5.6 Sol; "
        "resolvedLabel=GPT-5.6 Sol; status=switched; strategy=select; verified=yes; source=chatgpt-model-picker\n"
        "[browser] Thinking time: Pro, 5 of 5\n",
        encoding="utf-8",
    )
    assert executor.observed_model_check(stdout, model="gpt-5.6-sol", effort="pro")["verified"] is True


@pytest.mark.parametrize("effort", ["pro", "extra-high"])
def test_explicit_model_accepts_native_already_selected_effort(executor, tmp_path: Path, effort: str):
    stdout = tmp_path / "stdout.log"
    evidence = native_latest_evidence(effort, "GPT-5.6 Sol").replace(
        "requestedKey=latest; target=latest;", "requestedKey=gpt-5.6-sol; target=GPT-5.6 Sol;"
    )
    stdout.write_text(evidence, encoding="utf-8")
    result = executor.observed_model_check(stdout, model="gpt-5.6-sol", effort=effort)
    assert result["verified"] is True
    assert result["actual_model"] is None
    assert result["source"] == "oracle-native-selection-log"
    assert executor.observed_model_check(stdout, model="latest", effort=effort)["verified"] is False


def test_close_owned_tab_targets_only_exact_recorded_target(executor):
    urls: list[str] = []
    target_id = "B" * 32

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        urls.append(request.full_url)
        if calls == 1:
            return Response([
                {"id": target_id, "url": "https://chatgpt.com/c/owned", "type": "page"},
                {"id": "C" * 32, "url": "https://chatgpt.com/c/foreign", "type": "page"},
            ])
        if calls == 2:
            return Response({})
        return Response([{"id": "C" * 32, "url": "https://chatgpt.com/c/foreign", "type": "page"}])

    result = executor.close_owned_tab(
        {
            "host": "127.0.0.1",
            "port": 43123,
            "target_id": target_id,
            "conversation_url": "https://chatgpt.com/c/owned",
        },
        opener=opener,
    )

    assert result == {"status": "closed", "target_id": target_id}
    assert urls[1].endswith(f"/json/close/{target_id}")
    assert all(("C" * 32) not in url for url in urls)


def test_capture_state_is_durable_before_owned_tab_close(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0003", model="latest", effort="pro",
    )
    seed_run_project(executor, config)
    state_path = run_root / config.run_id / "state.json"

    def popen(argv, **kwargs):
        assert argv[argv.index("--browser-tab") + 1] == "B" * 32
        output = Path(argv[argv.index("--write-output") + 1])
        output.write_text("Complete captured answer.\n", encoding="utf-8")
        kwargs["stdout"].write(native_latest_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="completed",
            port=browser_port(argv),
        )
        return Process(0)

    cleanup_calls = []

    def cleanup(preflight, command, *, expected_url):
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["status"] == "captured"
        assert persisted["capture"] == "durable"
        assert persisted["artifacts"]["output_sha256"]
        cleanup_calls.append((preflight["target_id"], command, expected_url))
        return {"status": "closed", "target_id": preflight["target_id"]}

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        tab_closer=lambda binding: pytest.fail("preflight run must not close a tab separately"),
        browser_preflight=fake_preflight,
        browser_cleanup=cleanup,
    )

    assert result["ok"] is True
    assert result["status"] == "captured"
    assert result["result"]["semantic_outcome"] == "unknown"
    assert result["result"]["tab_close"]["status"] == "not_attempted"
    assert result["result"]["browser_cleanup"]["status"] == "closed"
    assert cleanup_calls == [("B" * 32, ["oracle"], "https://chatgpt.com/c/owned-temporary-run")]


def test_completed_owned_run_closes_browser_even_when_model_check_fails(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-model-check-fail"
    )
    seed_run_project(executor, config)
    cleanup_calls = []

    def popen(argv, **kwargs):
        output = Path(argv[argv.index("--write-output") + 1])
        output.write_text("Durably captured answer with missing model proof.\n", encoding="utf-8")
        kwargs["stdout"].write(b"[browser] no usable model evidence\n")
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="completed",
            port=browser_port(argv),
        )
        return Process(0)

    def cleanup(preflight, command, *, expected_url):
        cleanup_calls.append((preflight["target_id"], expected_url))
        return {"status": "closed", "target_id": preflight["target_id"]}

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        tab_closer=lambda binding: pytest.fail("preflight browser cleanup owns this tab"),
        browser_preflight=fake_preflight,
        browser_cleanup=cleanup,
    )

    assert result["status"] == "attention_required"
    assert result["result"]["submission"] == "observed"
    assert result["result"]["capture"] == "durable"
    assert result["result"]["model_check"]["verified"] is False
    assert result["result"]["browser_cleanup"]["status"] == "closed"
    assert cleanup_calls == [("B" * 32, "https://chatgpt.com/c/owned-temporary-run")]


def test_preflight_target_mismatch_blocks_capture_and_preserves_browser(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-run-mismatch"
    )
    seed_run_project(executor, config)

    def popen(argv, **kwargs):
        Path(argv[argv.index("--write-output") + 1]).write_text("Answer from wrong tab.\n", encoding="utf-8")
        kwargs["stdout"].write(native_latest_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="completed",
            port=browser_port(argv),
            target_id="A" * 32,
        )
        return Process(0)

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        tab_closer=lambda binding: pytest.fail("mismatched target must be preserved"),
        browser_preflight=fake_preflight,
        browser_cleanup=lambda *_args: pytest.fail("uncertain target must preserve browser"),
    )

    assert result["status"] == "attention_required"
    assert result["result"]["capture"] == "durable"
    assert result["result"]["submission"] == "unknown"
    assert result["result"]["oracle"]["binding"] is None


def test_popen_failure_cleans_owned_preflight_before_submission(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-run-popen-fail"
    )
    seed_run_project(executor, config)
    cleanup_calls = []

    def cleanup(preflight, command):
        cleanup_calls.append((preflight["target_id"], command))
        return {"status": "closed", "target_id": preflight["target_id"]}

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("popen failed")),
        tab_closer=lambda binding: pytest.fail("Popen failure must use browser cleanup only"),
        browser_preflight=fake_preflight,
        browser_cleanup=cleanup,
    )

    assert result["result"]["submission"] == "not_observed"
    assert result["result"]["browser_cleanup"]["status"] == "closed"
    assert cleanup_calls == [("B" * 32, ["oracle"])]


def test_historical_run_without_preflight_keeps_exact_tab_close(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="historical-run-0001", model="latest", effort="pro"
    )
    run_dir = run_root / config.run_id
    run_dir.mkdir(parents=True)
    output = run_dir / "output.md"
    stdout = run_dir / "stdout.log"
    output.write_text("Historical captured answer.\n", encoding="utf-8")
    stdout.write_text(f"{executor.PICKER_PROOF_PREFIX} {json.dumps(picker_proof())}\n", encoding="utf-8")
    state = executor._initial_state(config, run_dir, executor._slug(config), output, ["oracle"], cdp_port=43123)
    state_path = run_dir / "state.json"
    write_session_meta(
        session_root, state["oracle"]["slug"], status="completed", port=43123, target_id="A" * 32
    )
    closed = []

    result = executor._finalize_capture(
        state_path,
        state,
        stdout_path=stdout,
        output_path=output,
        tab_closer=lambda binding: closed.append(binding["target_id"]) or {"status": "closed"},
    )

    assert result["status"] == "captured"
    assert result["tab_close"]["status"] == "closed"
    assert closed == ["A" * 32]


def test_timeout_keeps_same_tab_and_never_resubmits(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0004", model="latest", effort="pro",
    )
    launches: list[list[str]] = []

    def popen(argv, **kwargs):
        launches.append(list(argv))
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="running",
            port=browser_port(argv),
        )
        return Process(1)

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        tab_closer=lambda binding: pytest.fail("incomplete run must not close its tab"),
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )

    assert result["ok"] is False
    assert result["status"] == "attention_required"
    assert result["result"]["submission"] == "observed"
    assert result["result"]["tab_close"]["status"] == "not_attempted"
    assert len(launches) == 1
    assert launches[0].count("--prompt") == 1


def test_reconnect_is_prompt_free_and_uses_original_slug(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0005", model="latest", effort="pro",
    )

    def initial_popen(argv, **kwargs):
        kwargs["stdout"].write(native_latest_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="running",
            port=browser_port(argv),
        )
        return Process(1)

    first = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=initial_popen,
        tab_closer=lambda binding: pytest.fail("initial incomplete run must retain tab"),
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )
    run_dir = Path(first["run_dir"])
    original = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    slug = original["oracle"]["slug"]
    project_url = original["workspace_project"]["url"]
    map_path = executor._workspace_project_map_path(config)
    mapping_before_reconnect = map_path.read_bytes()
    before_preview = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    preview = executor.reconnect_run(run_dir, dry_run=True)
    assert preview["resubmit"] is False
    assert before_preview == {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    recovery_argv: list[str] = []

    def reconnect_popen(argv, **kwargs):
        recovery_argv.extend(argv)
        Path(original["artifacts"]["output"]).write_text("Recovered complete answer.\n", encoding="utf-8")
        write_session_meta(
            session_root,
            slug,
            status="completed",
            port=int(original["oracle"]["expected_cdp_port"]),
        )
        return Process(0)

    recovered = executor.reconnect_run(
        run_dir,
        popen_factory=reconnect_popen,
        tab_closer=lambda binding: {"status": "closed", "target_id": binding["target_id"]},
        browser_cleanup=fake_cleanup,
    )

    assert recovered["ok"] is True
    assert recovered["result"]["workspace_project"]["url"] == project_url
    assert map_path.read_bytes() == mapping_before_reconnect
    assert recovery_argv[:3] == ["oracle", "session", slug]
    assert "--live" in recovery_argv
    assert "--prompt" not in recovery_argv
    assert "-p" not in recovery_argv


def test_unresolved_observed_run_blocks_duplicate_submission(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths

    def popen(argv, **kwargs):
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="running",
            port=browser_port(argv),
        )
        return Process(1)

    first_config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-run-0006"
    )
    executor.execute_config(
        first_config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )
    mission.write_text("A changed mission must not bypass an uncertain submission.\n", encoding="utf-8")
    second_config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-run-0007"
    )
    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(
            second_config,
            command_resolver=lambda: pytest.fail("duplicate must fail before resolving Oracle"),
        )
    assert exc.value.code == "RUN_RECONNECT_REQUIRED"


def test_unreadable_prior_run_state_blocks_replacement_submission(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    corrupt_run = run_root / "ordinary-run-corrupt"
    corrupt_run.mkdir(parents=True)
    (corrupt_run / "state.json").write_text('{"schema":', encoding="utf-8")
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0008",
    )

    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(
            config,
            command_resolver=lambda: pytest.fail("corrupt prior state must block before Oracle resolution"),
        )

    assert exc.value.code == "RUN_RECONNECT_REQUIRED"
    assert exc.value.evidence == {
        "run_dir": str(corrupt_run),
        "status": "state_unreadable",
        "submission": "unknown",
        "state_error_code": "RUN_STATE_INVALID",
    }
    assert not (run_root / config.run_id).exists()


def test_workspace_project_mapping_reuses_project_for_same_session_id(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    session = "2d30cc44-df98-4bb3-807c-b0703310d54a"
    first_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="session-run-1001",
        session_id=session,
    )
    second_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="session-run-1002",
        session_id=session,
    )
    map_path = executor._workspace_project_map_path(first_config)
    map_path.unlink(missing_ok=True)
    created_urls = iter([
        "https://chatgpt.com/g/g-p-session-shared/project",
    ])
    calls = []

    def preflight(command, profile_path, port, *, chatgpt_url, project_bootstrap):
        calls.append((chatgpt_url, project_bootstrap))
        if project_bootstrap is not None:
            url = next(created_urls)
            return {
                "ok": True,
                "pid": 1234,
                "port": port,
                "target_id": "B" * 32,
                "conversation_url": url,
                "project_url": url,
                "project_created": True,
                "instructions_verified": True,
                "personalization": "not-applicable",
                "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/bootstrap",
            }
        return {
            "ok": True,
            "pid": 1235,
            "port": port,
            "target_id": "C" * 32,
            "conversation_url": chatgpt_url,
            "project_url": chatgpt_url,
            "personalization": "not-applicable",
            "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/run",
        }

    cleanup = lambda *_args, **_kwargs: {"status": "closed", "target_id": "B" * 32}
    _, first_mapping = executor._open_workspace_project_browser(
        first_config, ["oracle"], first_config.copy_profile, 43123, preflight, cleanup
    )
    _, second_mapping = executor._open_workspace_project_browser(
        second_config, ["oracle"], second_config.copy_profile, 43124, preflight, cleanup
    )

    assert first_mapping["url"] == "https://chatgpt.com/g/g-p-session-shared/project"
    assert second_mapping["url"] == first_mapping["url"]
    assert [bootstrap is not None for _, bootstrap in calls] == [True, False, False]


def test_default_selection_is_sol_high_and_high_aliases_extended(executor) -> None:
    assert executor.DEFAULT_MODEL == "gpt-5.6-sol"
    assert executor.DEFAULT_EFFORT == "extended"
    assert executor._normalize_effort(None) == "extended"
    assert executor._normalize_effort("high") == "extended"
    assert executor._normalize_effort("High") == "extended"


def _sol_native_evidence(requested: str, label: str) -> str:
    return (
        "[browser] Model selection evidence: requestedKey=gpt-5.6-sol; target=GPT-5.6 Sol; "
        "resolvedLabel=GPT-5.6 Sol; status=already-selected; strategy=select; verified=yes; "
        "source=chatgpt-model-picker; capturedAt=now\n"
        f"[browser] Thinking effort evidence: requestedLevel={requested}; status=already-selected; "
        f"resolvedLabel={label}; verified=yes; failClosed=no; targetModelKind=(none); "
        "observedModelKind=thinking; source=chatgpt-thinking-picker; capturedAt=now\n"
    )


@pytest.mark.parametrize("label", ["High", "높음", "Extended"])
def test_sol_extended_is_verified_by_three_tier_high_label(executor, tmp_path: Path, label: str) -> None:
    stdout = tmp_path / "stdout.log"
    stdout.write_text(_sol_native_evidence("extended", label), encoding="utf-8")
    result = executor.observed_model_check(stdout, model="gpt-5.6-sol", effort="extended")
    assert result["verified"] is True
    assert result["source"] == "oracle-native-selection-log"


def test_sol_extended_rejects_extra_high_and_unverified_evidence(executor, tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.log"
    stdout.write_text(_sol_native_evidence("extra-high", "Extra High"), encoding="utf-8")
    assert executor.observed_model_check(stdout, model="gpt-5.6-sol", effort="extended")["verified"] is False
    stdout.write_text(
        _sol_native_evidence("extended", "(none)").replace("verified=yes; failClosed", "verified=no; failClosed")
        .replace("status=already-selected; resolvedLabel=(none)", "status=unverified; resolvedLabel=(none)"),
        encoding="utf-8",
    )
    assert executor.observed_model_check(stdout, model="gpt-5.6-sol", effort="extended")["verified"] is False


def test_sol_extended_fallback_log_accepts_high_but_not_extra_high(executor, tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.log"
    model_line = (
        "[browser] Model selection evidence: requestedKey=gpt-5.6-sol; target=GPT-5.6 Sol; "
        "resolvedLabel=GPT-5.6 Sol; status=already-selected; strategy=select; verified=yes; "
        "source=chatgpt-model-picker; capturedAt=now\n"
    )
    stdout.write_text(model_line + "[browser] Thinking time: High (already selected)\n", encoding="utf-8")
    assert executor.observed_model_check(stdout, model="gpt-5.6-sol", effort="extended")["verified"] is True
    stdout.write_text(model_line + "[browser] Thinking time: Extra High (already selected)\n", encoding="utf-8")
    assert executor.observed_model_check(stdout, model="gpt-5.6-sol", effort="extended")["verified"] is False
