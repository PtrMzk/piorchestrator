"""Tests for sandbox provider protocol and Docker wrapping."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from po.sandbox.docker import (
    DockerSandbox,
    _build_image,
    _check_docker,
    _resolve_hosts,
    _run_interactive_login,
    _volume_has_auth,
)
from po.sandbox.provider import NoSandbox, SandboxError


class TestNoSandbox:
    def test_wrap_command_passthrough(self, tmp_path: Path) -> None:
        sandbox = NoSandbox()
        cmd = ["claude", "-p", "hello"]
        env = {"FOO": "bar"}
        new_cmd, new_env = sandbox.wrap_command(
            cmd, worktree_path=tmp_path, project_root=tmp_path, env=env,
        )
        assert new_cmd == cmd
        assert new_env == env

    @pytest.mark.asyncio
    async def test_prepare_is_noop(self) -> None:
        sandbox = NoSandbox()
        await sandbox.prepare()  # Should not raise


class TestResolveHosts:
    def test_resolves_single_host(self) -> None:
        fake_results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("5.6.7.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443)),  # duplicate
        ]
        with patch("po.sandbox.docker.socket.getaddrinfo", return_value=fake_results):
            result = _resolve_hosts(["example.com"])
        assert result == {"example.com": ["1.2.3.4", "5.6.7.8"]}

    def test_resolves_multiple_hosts(self) -> None:
        def fake_getaddrinfo(hostname: str, port: int, family: int, type: int) -> list:
            if hostname == "api.anthropic.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
            elif hostname == "pypi.org":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.3", 443))]

        with patch("po.sandbox.docker.socket.getaddrinfo", side_effect=fake_getaddrinfo):
            result = _resolve_hosts(["api.anthropic.com", "pypi.org", "registry.npmjs.org"])
        assert result == {
            "api.anthropic.com": ["10.0.0.1"],
            "pypi.org": ["10.0.0.2"],
            "registry.npmjs.org": ["10.0.0.3"],
        }

    def test_dns_failure_raises_sandbox_error(self) -> None:
        with (
            patch(
                "po.sandbox.docker.socket.getaddrinfo",
                side_effect=socket.gaierror("DNS failed"),
            ),
            pytest.raises(SandboxError, match="Cannot resolve"),
        ):
            _resolve_hosts(["bad.host"])

    def test_empty_results_raises_sandbox_error(self) -> None:
        with (
            patch("po.sandbox.docker.socket.getaddrinfo", return_value=[]),
            pytest.raises(SandboxError, match="no addresses"),
        ):
            _resolve_hosts(["empty.host"])


class TestCheckDocker:
    def test_docker_not_installed(self) -> None:
        with (
            patch("po.sandbox.docker.shutil.which", return_value=None),
            pytest.raises(SandboxError, match="not installed"),
        ):
            _check_docker()

    def test_docker_daemon_not_running(self) -> None:
        with (
            patch("po.sandbox.docker.shutil.which", return_value="/usr/bin/docker"),
            patch(
                "po.sandbox.docker.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "docker info"),
            ),
            pytest.raises(SandboxError, match="not running"),
        ):
            _check_docker()

    def test_docker_daemon_timeout(self) -> None:
        with (
            patch("po.sandbox.docker.shutil.which", return_value="/usr/bin/docker"),
            patch(
                "po.sandbox.docker.subprocess.run",
                side_effect=subprocess.TimeoutExpired("docker info", 10),
            ),
            pytest.raises(SandboxError, match="not responding"),
        ):
            _check_docker()

    def test_docker_ok(self) -> None:
        with (
            patch("po.sandbox.docker.shutil.which", return_value="/usr/bin/docker"),
            patch("po.sandbox.docker.subprocess.run"),
        ):
            _check_docker()  # Should not raise


class TestBuildImage:
    def test_build_failure_raises(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "build error details"

        with (
            patch("po.sandbox.docker.importlib.resources.files") as mock_files,
            patch("po.sandbox.docker.importlib.resources.as_file") as mock_as_file,
            patch("po.sandbox.docker.subprocess.run", return_value=mock_result),
        ):
            mock_pkg = MagicMock()
            mock_files.return_value = mock_pkg
            # Set up traversable paths
            mock_df = MagicMock()
            mock_ep = MagicMock()
            mock_pkg.__truediv__ = lambda self, key: mock_df if key == "Dockerfile" else mock_ep

            # as_file returns context managers yielding real Path-like objects
            df_path = Path("/fake/sandbox/Dockerfile")
            ep_path = Path("/fake/sandbox/entrypoint.sh")
            mock_as_file.side_effect = [
                _FakeContextManager(df_path),
                _FakeContextManager(ep_path),
            ]

            with pytest.raises(SandboxError, match="build error details"):
                _build_image("test-image:latest")


class TestVolumeAuth:
    def test_volume_has_auth_returns_true(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("po.sandbox.docker.subprocess.run", return_value=mock_result):
            assert _volume_has_auth("test-vol", "test:v1") is True

    def test_volume_has_auth_returns_false(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("po.sandbox.docker.subprocess.run", return_value=mock_result):
            assert _volume_has_auth("test-vol", "test:v1") is False

    def test_volume_has_auth_checks_credentials(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("po.sandbox.docker.subprocess.run", return_value=mock_result) as mock_run:
            _volume_has_auth("my-vol", "my-image:latest")
        # Should mount the named volume and check for credential files
        cmd = mock_run.call_args[0][0]
        assert "my-vol:/home/agent/.claude" in " ".join(cmd)
        assert "credentials.json" in " ".join(cmd)

    def test_interactive_login_raises_on_failure(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        with (
            patch("po.sandbox.docker.subprocess.run", return_value=mock_result),
            pytest.raises(SandboxError, match="Login failed"),
        ):
            _run_interactive_login("test-vol", "test:v1")

    def test_interactive_login_runs_docker_it(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("po.sandbox.docker.subprocess.run", return_value=mock_result) as mock_run:
            _run_interactive_login("my-vol", "my-image:latest")
        cmd = mock_run.call_args[0][0]
        assert "-it" in cmd
        assert "my-vol:/home/agent/.claude" in " ".join(cmd)
        assert cmd[-1] == "/login"


class TestDockerSandbox:
    def test_wrap_command_structure(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox(image_name="test-image:latest", auth_volume="test-vol")
        sandbox._host_ips = {
            "api.anthropic.com": ["1.2.3.4", "5.6.7.8"],
            "pypi.org": ["10.0.0.1"],
        }

        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        cmd = ["claude", "-p", "hello", "--output-format", "stream-json"]
        env = {"ANTHROPIC_API_KEY": "sk-test-123", "HOME": "/old/home"}

        new_cmd, new_env = sandbox.wrap_command(
            cmd, worktree_path=worktree, project_root=project, env=env,
        )

        assert new_cmd[0] == "docker"
        assert "run" in new_cmd
        assert "--rm" in new_cmd
        assert "-i" in new_cmd

        # Volume mounts: project root + auth volume
        vol_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "-v"]
        assert f"{project}:{project}" in vol_args
        assert "test-vol:/home/agent/.claude" in vol_args

        # Working directory
        w_idx = new_cmd.index("-w")
        assert new_cmd[w_idx + 1] == str(worktree)

        # API key is passed via -e
        env_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "-e"]
        assert any("ANTHROPIC_API_KEY=sk-test-123" in e for e in env_args)

        # --add-host entries for all hosts
        add_host_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "--add-host"]
        assert "api.anthropic.com:1.2.3.4" in add_host_args
        assert "api.anthropic.com:5.6.7.8" in add_host_args
        assert "pypi.org:10.0.0.1" in add_host_args

        # NET_ADMIN and NET_RAW capabilities
        assert "--cap-add=NET_ADMIN" in new_cmd
        assert "--cap-add=NET_RAW" in new_cmd

        # IPv6 disabled
        sysctl_idx = new_cmd.index("--sysctl")
        assert new_cmd[sysctl_idx + 1] == "net.ipv6.conf.all.disable_ipv6=1"

        # Image name before the actual command
        img_idx = new_cmd.index("test-image:latest")
        assert new_cmd[img_idx + 1:] == cmd

    def test_wrap_command_with_no_api_key(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox()
        sandbox._host_ips = {"api.anthropic.com": ["1.2.3.4"]}

        new_cmd, _ = sandbox.wrap_command(
            ["claude", "-p", "hi"],
            worktree_path=tmp_path,
            project_root=tmp_path,
            env={"PATH": "/usr/bin"},
        )

        # Should still produce a valid command even without ANTHROPIC_API_KEY
        env_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "-e"]
        assert any("ANTHROPIC_API_KEY=" in e for e in env_args)

    @pytest.mark.asyncio
    async def test_prepare_calls_check_resolve_build_and_auth(self) -> None:
        sandbox = DockerSandbox(image_name="test:v1", auth_volume="test-vol")

        with (
            patch("po.sandbox.docker._check_docker") as mock_check,
            patch("po.sandbox.docker._resolve_hosts", return_value={
                "api.anthropic.com": ["10.0.0.1"],
                "pypi.org": ["10.0.0.2"],
                "files.pythonhosted.org": ["10.0.0.3"],
                "registry.npmjs.org": ["10.0.0.4"],
            }) as mock_dns,
            patch("po.sandbox.docker._build_image") as mock_build,
            patch("po.sandbox.docker._volume_has_auth", return_value=True) as mock_auth,
        ):
            await sandbox.prepare()

        mock_check.assert_called_once()
        mock_dns.assert_called_once()
        mock_build.assert_called_once_with("test:v1")
        mock_auth.assert_called_once_with("test-vol", "test:v1")
        assert sandbox._host_ips["api.anthropic.com"] == ["10.0.0.1"]

    @pytest.mark.asyncio
    async def test_prepare_triggers_login_when_no_auth(self) -> None:
        sandbox = DockerSandbox(image_name="test:v1", auth_volume="test-vol")

        with (
            patch("po.sandbox.docker._check_docker"),
            patch("po.sandbox.docker._resolve_hosts", return_value={
                "api.anthropic.com": ["10.0.0.1"],
            }),
            patch("po.sandbox.docker._build_image"),
            patch(
                "po.sandbox.docker._volume_has_auth",
                side_effect=[False, True],  # No auth → login → auth found
            ),
            patch("po.sandbox.docker._run_interactive_login") as mock_login,
        ):
            await sandbox.prepare()

        mock_login.assert_called_once_with("test-vol", "test:v1")

    @pytest.mark.asyncio
    async def test_prepare_raises_when_login_fails(self) -> None:
        sandbox = DockerSandbox(image_name="test:v1", auth_volume="test-vol")

        with (
            patch("po.sandbox.docker._check_docker"),
            patch("po.sandbox.docker._resolve_hosts", return_value={
                "api.anthropic.com": ["10.0.0.1"],
            }),
            patch("po.sandbox.docker._build_image"),
            patch(
                "po.sandbox.docker._volume_has_auth",
                return_value=False,  # Still no auth after login
            ),
            patch("po.sandbox.docker._run_interactive_login"),
            pytest.raises(SandboxError, match="no credentials found"),
        ):
            await sandbox.prepare()

    def test_tmpfs_mounts(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox()
        sandbox._host_ips = {"api.anthropic.com": ["1.2.3.4"]}

        new_cmd, _ = sandbox.wrap_command(
            ["claude"], worktree_path=tmp_path, project_root=tmp_path, env={},
        )

        tmpfs_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "--tmpfs"]
        assert "/tmp:size=1G" in tmpfs_args

    def test_hostname_set(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox()
        sandbox._host_ips = {"api.anthropic.com": ["1.2.3.4"]}

        new_cmd, _ = sandbox.wrap_command(
            ["claude"], worktree_path=tmp_path, project_root=tmp_path, env={},
        )

        hostname_idx = new_cmd.index("--hostname")
        assert new_cmd[hostname_idx + 1] == "po-agent"

    def test_wrap_command_mounts_auth_volume(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox(auth_volume="my-auth-vol")
        sandbox._host_ips = {"api.anthropic.com": ["1.2.3.4"]}

        new_cmd, _ = sandbox.wrap_command(
            ["claude"], worktree_path=tmp_path, project_root=tmp_path, env={},
        )

        vol_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "-v"]
        assert "my-auth-vol:/home/agent/.claude" in vol_args

    def test_wrap_command_includes_registry_hosts(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox()
        sandbox._host_ips = {
            "api.anthropic.com": ["10.0.0.1"],
            "pypi.org": ["10.0.0.2"],
            "files.pythonhosted.org": ["10.0.0.3", "10.0.0.4"],
            "registry.npmjs.org": ["10.0.0.5"],
        }

        new_cmd, _ = sandbox.wrap_command(
            ["claude"], worktree_path=tmp_path, project_root=tmp_path, env={},
        )

        add_host_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "--add-host"]
        assert "pypi.org:10.0.0.2" in add_host_args
        assert "files.pythonhosted.org:10.0.0.3" in add_host_args
        assert "files.pythonhosted.org:10.0.0.4" in add_host_args
        assert "registry.npmjs.org:10.0.0.5" in add_host_args


class _FakeContextManager:
    """Minimal context manager that yields a fixed value."""

    def __init__(self, value: Path) -> None:
        self._value = value

    def __enter__(self) -> Path:
        return self._value

    def __exit__(self, *args: object) -> None:
        pass
