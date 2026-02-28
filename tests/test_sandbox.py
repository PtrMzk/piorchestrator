"""Tests for sandbox provider protocol and Docker wrapping."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from po.sandbox.docker import DockerSandbox, _build_image, _check_docker, _resolve_hosts
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


class TestDockerSandbox:
    def test_wrap_command_structure(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox(image_name="test-image:latest")
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

        # Volume mount preserves absolute path
        vol_idx = new_cmd.index("-v")
        assert new_cmd[vol_idx + 1] == f"{project}:{project}"

        # Working directory
        w_idx = new_cmd.index("-w")
        assert new_cmd[w_idx + 1] == str(worktree)

        # API key is passed via -e
        assert "-e" in new_cmd
        env_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "-e"]
        assert any("ANTHROPIC_API_KEY=sk-test-123" in e for e in env_args)

        # --add-host entries for all hosts
        add_host_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "--add-host"]
        assert "api.anthropic.com:1.2.3.4" in add_host_args
        assert "api.anthropic.com:5.6.7.8" in add_host_args
        assert "pypi.org:10.0.0.1" in add_host_args

        # NET_ADMIN capability
        assert "--cap-add=NET_ADMIN" in new_cmd

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
    async def test_prepare_calls_check_resolve_build(self) -> None:
        sandbox = DockerSandbox(image_name="test:v1")

        with (
            patch("po.sandbox.docker._check_docker") as mock_check,
            patch("po.sandbox.docker._resolve_hosts", return_value={
                "api.anthropic.com": ["10.0.0.1"],
                "pypi.org": ["10.0.0.2"],
                "files.pythonhosted.org": ["10.0.0.3"],
                "registry.npmjs.org": ["10.0.0.4"],
            }) as mock_dns,
            patch("po.sandbox.docker._build_image") as mock_build,
        ):
            await sandbox.prepare()

        mock_check.assert_called_once()
        mock_dns.assert_called_once()
        mock_build.assert_called_once_with("test:v1")
        assert "api.anthropic.com" in sandbox._host_ips
        assert sandbox._host_ips["api.anthropic.com"] == ["10.0.0.1"]

    def test_tmpfs_mounts(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox()
        sandbox._host_ips = {"api.anthropic.com": ["1.2.3.4"]}

        new_cmd, _ = sandbox.wrap_command(
            ["claude"], worktree_path=tmp_path, project_root=tmp_path, env={},
        )

        tmpfs_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "--tmpfs"]
        assert "/tmp:size=1G" in tmpfs_args
        assert "/home/agent" in tmpfs_args

    def test_hostname_set(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox()
        sandbox._host_ips = {"api.anthropic.com": ["1.2.3.4"]}

        new_cmd, _ = sandbox.wrap_command(
            ["claude"], worktree_path=tmp_path, project_root=tmp_path, env={},
        )

        hostname_idx = new_cmd.index("--hostname")
        assert new_cmd[hostname_idx + 1] == "po-agent"

    def test_wrap_command_mounts_claude_config(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox()
        sandbox._host_ips = {"api.anthropic.com": ["1.2.3.4"]}

        # Create a fake .claude dir to simulate host config
        fake_claude_dir = tmp_path / "fakehome" / ".claude"
        fake_claude_dir.mkdir(parents=True)

        with patch("po.sandbox.docker.os.environ", {"CLAUDE_CONFIG_DIR": str(fake_claude_dir)}):
            new_cmd, _ = sandbox.wrap_command(
                ["claude"], worktree_path=tmp_path, project_root=tmp_path, env={},
            )

        vol_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "-v"]
        assert f"{fake_claude_dir}:/home/agent/.claude-host:ro" in vol_args

    def test_wrap_command_skips_claude_config_when_missing(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox()
        sandbox._host_ips = {"api.anthropic.com": ["1.2.3.4"]}

        fake_home = tmp_path / "emptyhome"
        fake_home.mkdir()

        with (
            patch("po.sandbox.docker.os.environ", {"CLAUDE_CONFIG_DIR": "/nonexistent/.claude"}),
            patch("po.sandbox.docker.Path.home", return_value=fake_home),
        ):
            new_cmd, _ = sandbox.wrap_command(
                ["claude"], worktree_path=tmp_path, project_root=tmp_path, env={},
            )

        vol_args = [new_cmd[i + 1] for i, x in enumerate(new_cmd) if x == "-v"]
        # Only project root mount, no claude config mount
        assert len(vol_args) == 1

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


# --- helpers ---


class _FakeContextManager:
    """Minimal context manager that yields a fixed value."""

    def __init__(self, value: Path) -> None:
        self._value = value

    def __enter__(self) -> Path:
        return self._value

    def __exit__(self, *args: object) -> None:
        pass
