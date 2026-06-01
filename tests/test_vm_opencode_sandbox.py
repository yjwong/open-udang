import subprocess
from pathlib import Path
from unittest.mock import patch

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.libvirt import LibvirtSandbox
from open_shrimp.sandbox.lima import LimaSandbox
from open_shrimp.sandbox.lima_helpers import _build_mounts
from open_shrimp.sandbox.lima_macos_helpers import _build_mounts_macos
from open_shrimp.sandbox.skill_paths import global_skill_dir_candidates


class FakeProc:
    def __init__(self, args: list[str], *, tunnel: bool = False) -> None:
        self.args = args
        self.tunnel = tunnel
        self.running = True
        self.returncode: int | None = None
        self.stdout = []
        self.stderr = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else self.returncode or 0

    def wait(self, timeout: float | None = None) -> int:
        if self.tunnel and self.running and timeout == 0.5:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.running = False
        self.returncode = self.returncode or 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.running = False
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.running = False
        self.returncode = -9


def test_global_skill_dir_candidates_support_opencode_and_external_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert global_skill_dir_candidates(guest_home="/guest") == [
        (tmp_path / ".claude" / "skills", "/guest/.claude/skills"),
        (tmp_path / ".agents" / "skills", "/guest/.agents/skills"),
        (tmp_path / ".config" / "opencode" / "skills", "/guest/.config/opencode/skills"),
        (tmp_path / ".config" / "opencode" / "skill", "/guest/.config/opencode/skill"),
    ]


def test_lima_mounts_opencode_home_for_linux_and_macos(tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")

    linux_mounts = _build_mounts(tmp_path, "/repo", None)
    macos_mounts = _build_mounts_macos(tmp_path, "/repo", None)

    assert {
        "location": str(tmp_path / "opencode-home"),
        "mountPoint": "/home/alice.guest/.local/share/opencode",
        "writable": True,
    } in linux_mounts
    assert {
        "location": str(tmp_path / "opencode-home"),
        "mountPoint": "/Users/alice.guest/Library/Application Support/opencode",
        "writable": True,
    } in macos_mounts


def test_lima_mounts_global_skill_dirs(tmp_path, monkeypatch):
    paths = {
        tmp_path / ".claude" / "skills": (
            "/home/alice.guest/.claude/skills",
            "/Users/alice.guest/.claude/skills",
        ),
        tmp_path / ".agents" / "skills": (
            "/home/alice.guest/.agents/skills",
            "/Users/alice.guest/.agents/skills",
        ),
        tmp_path / ".config" / "opencode" / "skills": (
            "/home/alice.guest/.config/opencode/skills",
            "/Users/alice.guest/.config/opencode/skills",
        ),
        tmp_path / ".config" / "opencode" / "skill": (
            "/home/alice.guest/.config/opencode/skill",
            "/Users/alice.guest/.config/opencode/skill",
        ),
    }
    for path in paths:
        path.mkdir(parents=True)
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    linux_mounts = _build_mounts(tmp_path, "/repo", None)
    macos_mounts = _build_mounts_macos(tmp_path, "/repo", None)

    for skills, (mount_point, _) in paths.items():
        assert {
            "location": str(skills),
            "mountPoint": mount_point,
            "writable": False,
        } in linux_mounts

    for skills, (_, mount_point) in paths.items():
        assert {
            "location": str(skills),
            "mountPoint": mount_point,
            "writable": False,
        } in macos_mounts


def test_libvirt_mounts_global_skill_dirs(tmp_path, monkeypatch):
    legacy_skills = tmp_path / ".claude" / "skills"
    agents_skills = tmp_path / ".agents" / "skills"
    opencode_skills = tmp_path / ".config" / "opencode" / "skills"
    opencode_skill = tmp_path / ".config" / "opencode" / "skill"
    for path in (legacy_skills, agents_skills, opencode_skills, opencode_skill):
        path.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    with (
        patch("open_shrimp.sandbox.libvirt.state_dir_for", return_value=tmp_path / "state"),
        patch("open_shrimp.sandbox.libvirt.find_virtiofsd", return_value=None),
    ):
        sandbox = LibvirtSandbox(
            "dev",
            SandboxConfig(backend="libvirt"),
            "/repo",
            conn=object(),
        )
        all_dirs, mount_overrides, readonly_dirs = sandbox._shared_dirs_and_overrides()

    assert str(legacy_skills) in all_dirs
    assert str(agents_skills) in all_dirs
    assert str(opencode_skills) in all_dirs
    assert str(opencode_skill) in all_dirs
    assert mount_overrides[str(legacy_skills)] == "/home/openshrimp/.claude/skills"
    assert mount_overrides[str(agents_skills)] == "/home/openshrimp/.agents/skills"
    assert mount_overrides[str(opencode_skills)] == "/home/openshrimp/.config/opencode/skills"
    assert mount_overrides[str(opencode_skill)] == "/home/openshrimp/.config/opencode/skill"
    assert {str(legacy_skills), str(agents_skills), str(opencode_skills), str(opencode_skill)} <= readonly_dirs


def test_libvirt_ensure_opencode_server_starts_guest_server(tmp_path):
    procs: list[FakeProc] = []

    def fake_popen(args, **kwargs):
        proc = FakeProc(args, tunnel=args[:1] == ["ssh"] and "-N" in args)
        procs.append(proc)
        return proc

    with (
        patch("open_shrimp.sandbox.libvirt.state_dir_for", return_value=tmp_path),
        patch("open_shrimp.sandbox.libvirt.find_virtiofsd", return_value=None),
        patch("open_shrimp.sandbox.libvirt.allocate_host_port", return_value=49152),
        patch("open_shrimp.sandbox.libvirt._sync_opencode_auth") as sync_auth,
        patch("open_shrimp.sandbox.libvirt._wait_for_opencode_ready"),
        patch("open_shrimp.sandbox.libvirt._drain_opencode_output"),
        patch("open_shrimp.sandbox.libvirt.subprocess.Popen", side_effect=fake_popen),
        patch(
            "open_shrimp.sandbox.libvirt_helpers._ssh_common_opts",
            return_value=["-p", "2222", "-i", str(tmp_path / "ssh_key")],
        ),
    ):
        sandbox = LibvirtSandbox(
            "dev",
            SandboxConfig(backend="libvirt"),
            "/repo",
            conn=object(),
        )
        sandbox._ssh_port = 2222
        endpoint = sandbox.ensure_opencode_server(provider_id="openai")

    assert endpoint.base_url == "http://127.0.0.1:49152"
    assert endpoint.auth_header.startswith("Basic ")
    sync_auth.assert_called_once_with("openai", tmp_path / "opencode-home")
    assert procs[0].args[:2] == ["ssh", "-p"]
    assert procs[0].args[-1] == "openshrimp@localhost"
    assert "-L" in procs[0].args
    assert "opencode serve --hostname 127.0.0.1" in procs[1].args[-1]


def test_lima_ensure_opencode_server_uses_internal_tunnel_and_cache(
    tmp_path, monkeypatch,
):
    lima_home = tmp_path / "lima-home"
    ssh_dir = lima_home / "dev"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "ssh.config").write_text("Host lima-dev\n", encoding="utf-8")
    procs: list[FakeProc] = []

    def fake_popen(args, **kwargs):
        proc = FakeProc(args, tunnel=args[:1] == ["ssh"] and "-N" in args)
        procs.append(proc)
        return proc

    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    with (
        patch("open_shrimp.sandbox.lima.state_dir_for", return_value=tmp_path / "state"),
        patch("open_shrimp.sandbox.lima._lima_env", return_value={"LIMA_HOME": str(lima_home)}),
        patch("open_shrimp.sandbox.lima.limactl_instance_status", return_value="Running"),
        patch("open_shrimp.sandbox.lima.allocate_host_port", return_value=49153),
        patch("open_shrimp.sandbox.lima._sync_opencode_auth"),
        patch("open_shrimp.sandbox.lima._wait_for_opencode_ready"),
        patch("open_shrimp.sandbox.lima._drain_opencode_output"),
        patch("open_shrimp.sandbox.lima.subprocess.Popen", side_effect=fake_popen),
    ):
        sandbox = LimaSandbox(
            "dev",
            SandboxConfig(backend="lima"),
            "/repo",
            "limactl",
            guest_os="linux",
        )
        first = sandbox.ensure_opencode_server(provider_id="openai")
        second = sandbox.ensure_opencode_server(provider_id="openai")
        sandbox._opencode_forward.running = False
        third = sandbox.ensure_opencode_server(provider_id="openai")

    assert first is second
    assert third.base_url == "http://127.0.0.1:49153"
    assert len(procs) == 4
    assert procs[0].args[:3] == ["ssh", "-F", str(ssh_dir / "ssh.config")]
    assert procs[0].args[-1] == "lima-dev"
    assert "-L" in procs[0].args
    assert procs[1].args[:4] == ["limactl", "shell", "dev", "--"]
    assert "OPENCODE_SERVER_PASSWORD=" in procs[1].args[-1]
    assert "opencode serve --hostname 127.0.0.1" in procs[1].args[-1]
