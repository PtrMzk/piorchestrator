"""Docker-based sandbox for running agents in containers.

Auth is stored in a named Docker volume (po-claude-auth). On first run,
the user is prompted to log in interactively inside a one-off container.
Subsequent runs reuse the volume automatically.
"""

from __future__ import annotations

import importlib.resources
import logging
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from po.config import (
    SANDBOX_API_HOST,
    SANDBOX_AUTH_VOLUME,
    SANDBOX_IMAGE_NAME,
    SANDBOX_REGISTRY_HOSTS,
)
from po.sandbox.provider import SandboxError

logger = logging.getLogger(__name__)


def _resolve_hosts(hostnames: list[str]) -> dict[str, list[str]]:
    """Resolve a list of hostnames to their IPv4 addresses.

    Returns a dict mapping each hostname to its list of unique IPv4 addresses.
    """
    result: dict[str, list[str]] = {}
    for hostname in hostnames:
        try:
            infos = socket.getaddrinfo(hostname, 443, socket.AF_INET, socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise SandboxError(
                f"Cannot resolve {hostname}: {e}\n"
                "Check your network connection."
            ) from e
        ips = list(dict.fromkeys(r[4][0] for r in infos))
        if not ips:
            raise SandboxError(f"DNS returned no addresses for {hostname}")
        # Validate each IP to prevent injection via malicious DNS
        ip_re = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
        for ip in ips:
            if not ip_re.match(ip):
                raise SandboxError(f"Invalid IP from DNS for {hostname}: {ip}")
        result[hostname] = ips
    return result


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


def _volume_has_auth(
    volume_name: str = SANDBOX_AUTH_VOLUME,
    image_name: str = SANDBOX_IMAGE_NAME,
) -> bool:
    """Check if the auth volume contains Claude credentials."""
    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{volume_name}:/home/agent/.claude",
            image_name,
            "sh", "-c",
            "test -f /home/agent/.claude/credentials.json"
            " || test -f /home/agent/.claude/.credentials.json"
            ' || grep -q oauthAccount /home/agent/.claude.json 2>/dev/null',
        ],
        capture_output=True, timeout=15,
    )
    return result.returncode == 0


def _run_interactive_login(
    volume_name: str = SANDBOX_AUTH_VOLUME,
    image_name: str = SANDBOX_IMAGE_NAME,
) -> None:
    """Run an interactive container for the user to log in to Claude."""
    print(
        "\n  Sandbox auth not configured. Launching interactive login...\n"
        "  A browser window will open. Complete the login, then the\n"
        "  container will exit and credentials will be saved.\n"
    )

    # Need a TTY for the OAuth flow — run interactively
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-it",
            "-v", f"{volume_name}:/home/agent/.claude",
            "-e", "HOME=/home/agent",
            image_name,
            "claude", "/login",
        ],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if result.returncode != 0:
        raise SandboxError(
            "Login failed. Run 'po run' again to retry."
        )


class DockerSandbox:
    """Run agent commands inside a Docker container.

    Filesystem: mounts project_root at the same absolute path inside the
    container, preserving git worktree references.

    Network: allows HTTPS to api.anthropic.com and package registries.
    DNS is allowed so package managers can resolve CDN hostnames.
    API and registry IPs are injected via --add-host and the entrypoint
    configures iptables to reject everything else.

    Auth: stored in a named Docker volume (po-claude-auth). On first use,
    the user is prompted to log in interactively. The volume persists
    across all container runs.
    """

    def __init__(
        self,
        image_name: str = SANDBOX_IMAGE_NAME,
        auth_volume: str = SANDBOX_AUTH_VOLUME,
    ) -> None:
        self._image_name = image_name
        self._auth_volume = auth_volume
        self._host_ips: dict[str, list[str]] = {}

    async def prepare(self) -> None:
        """Check Docker, resolve host IPs, build the image, ensure auth."""
        _check_docker()
        all_hosts = [SANDBOX_API_HOST] + SANDBOX_REGISTRY_HOSTS
        self._host_ips = _resolve_hosts(all_hosts)
        logger.debug("Resolved hosts: %s", self._host_ips)
        _build_image(self._image_name)

        # Ensure the auth volume exists and has credentials
        if not _volume_has_auth(self._auth_volume, self._image_name):
            _run_interactive_login(self._auth_volume, self._image_name)
            # Verify login succeeded
            if not _volume_has_auth(self._auth_volume, self._image_name):
                raise SandboxError(
                    "Login completed but no credentials found in the volume.\n"
                    "Try again or use --no-sandbox."
                )
            logger.info("Auth volume %s is ready", self._auth_volume)

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
            # Mount auth volume — persisted credentials from one-time login
            "-v", f"{self._auth_volume}:/home/agent/.claude",
            # Tmpfs for scratch space
            "--tmpfs", "/tmp:size=1G",
            # Working directory
            "-w", str(worktree_path),
            # Environment
            "-e", f"ANTHROPIC_API_KEY={env.get('ANTHROPIC_API_KEY', '')}",
            "-e", "HOME=/home/agent",
            "-e", f"PO_PROJECT_ROOT={project_root}",
        ]

        # Inject all resolved host IPs via --add-host
        for hostname, ips in self._host_ips.items():
            for ip in ips:
                docker_cmd.extend(["--add-host", f"{hostname}:{ip}"])

        # Network isolation capabilities
        docker_cmd.extend([
            "--cap-add=NET_ADMIN",
            "--cap-add=NET_RAW",
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
            "--hostname", "po-agent",
        ])

        # Image and the actual command
        docker_cmd.append(self._image_name)
        docker_cmd.extend(cmd)

        return docker_cmd, env
