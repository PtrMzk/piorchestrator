"""Docker-based sandbox for running agents in containers."""

from __future__ import annotations

import importlib.resources
import logging
import shutil
import socket
import subprocess
from pathlib import Path

from po.config import SANDBOX_API_HOST, SANDBOX_IMAGE_NAME
from po.sandbox.provider import SandboxError

logger = logging.getLogger(__name__)


def _resolve_api_ips(hostname: str = SANDBOX_API_HOST) -> list[str]:
    """Resolve the API hostname to a list of IPv4 addresses."""
    try:
        results = socket.getaddrinfo(hostname, 443, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SandboxError(
            f"Cannot resolve {hostname}: {e}\n"
            "Check your network connection."
        ) from e
    ips = list(dict.fromkeys(r[4][0] for r in results))
    if not ips:
        raise SandboxError(f"DNS returned no addresses for {hostname}")
    return ips


def _check_docker() -> None:
    """Verify Docker is installed and the daemon is running."""
    docker = shutil.which("docker")
    if docker is None:
        raise SandboxError(
            "Docker is not installed.\n"
            "Install it from https://docs.docker.com/get-docker/"
        )
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True, check=True, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise SandboxError(
            "Docker daemon is not running.\n"
            "Start it with: open -a Docker  (macOS)  or  sudo systemctl start docker  (Linux)"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise SandboxError("Docker daemon is not responding (timed out).") from e


def _build_image(image_name: str = SANDBOX_IMAGE_NAME) -> None:
    """Build the sandbox Docker image from the bundled Dockerfile."""
    pkg = importlib.resources.files("po.sandbox")
    dockerfile = pkg / "Dockerfile"
    entrypoint = pkg / "entrypoint.sh"

    # importlib.resources may return traversable objects; we need real paths
    # for the docker build context.  Use as_file() for safety.
    with (
        importlib.resources.as_file(dockerfile) as df_path,
        importlib.resources.as_file(entrypoint) as ep_path,
    ):
        # Build context is the directory containing both files
        context_dir = df_path.parent
        # Ensure entrypoint is in the same dir (it should be, both are in po.sandbox)
        if ep_path.parent != context_dir:
            raise SandboxError("Dockerfile and entrypoint.sh must be in the same directory")

        logger.info("Building sandbox image %s ...", image_name)
        result = subprocess.run(
            ["docker", "build", "-t", image_name, "-f", str(df_path), str(context_dir)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise SandboxError(
                f"Failed to build sandbox image:\n{result.stderr}"
            )
        logger.info("Sandbox image %s built successfully", image_name)


class DockerSandbox:
    """Run agent commands inside a Docker container.

    Filesystem: mounts project_root at the same absolute path inside the
    container, preserving git worktree references.

    Network: only allows HTTPS to api.anthropic.com.  DNS is blocked;
    API IPs are injected via --add-host and the entrypoint configures
    iptables to drop everything else.
    """

    def __init__(self, image_name: str = SANDBOX_IMAGE_NAME) -> None:
        self._image_name = image_name
        self._api_ips: list[str] = []

    async def prepare(self) -> None:
        """Check Docker, resolve API IPs, build the image."""
        _check_docker()
        self._api_ips = _resolve_api_ips()
        logger.debug("Resolved %s to %s", SANDBOX_API_HOST, self._api_ips)
        _build_image(self._image_name)

    def wrap_command(
        self,
        cmd: list[str],
        *,
        worktree_path: Path,
        project_root: Path,
        env: dict[str, str],
    ) -> tuple[list[str], dict[str, str]]:
        """Wrap a command to run inside the sandbox container."""
        docker_cmd = [
            "docker", "run", "--rm", "-i",
            # Mount project at the same absolute path
            "-v", f"{project_root}:{project_root}",
            # Tmpfs for scratch space
            "--tmpfs", "/tmp:size=1G",
            "--tmpfs", "/home/agent",
            # Working directory
            "-w", str(worktree_path),
            # Environment
            "-e", f"ANTHROPIC_API_KEY={env.get('ANTHROPIC_API_KEY', '')}",
            "-e", "HOME=/home/agent",
        ]

        # Inject API IPs via --add-host
        for ip in self._api_ips:
            docker_cmd.extend(["--add-host", f"{SANDBOX_API_HOST}:{ip}"])

        # Network isolation capabilities
        docker_cmd.extend([
            "--cap-add=NET_ADMIN",
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
            "--hostname", "po-agent",
        ])

        # Image and the actual command
        docker_cmd.append(self._image_name)
        docker_cmd.extend(cmd)

        # The container manages its own env; host env is not passed through
        # except ANTHROPIC_API_KEY which is handled via -e above.
        # Return empty env so create_subprocess_exec uses the host env
        # (Docker handles isolation).
        return docker_cmd, env
