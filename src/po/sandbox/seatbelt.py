"""macOS sandbox-exec (seatbelt) sandbox for agent isolation.

Uses Apple's sandbox-exec to restrict agent processes:
- File writes: only to project root, temp dirs, and home caches
- Network: HTTPS (port 443) and DNS (port 53) only
- File reads: unrestricted (agents need system binaries, Python, node, etc.)

sandbox-exec is deprecated by Apple but still functional on current macOS.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from po.sandbox.provider import SandboxError

logger = logging.getLogger(__name__)


def _check_sandbox_exec() -> None:
    """Verify sandbox-exec is available."""
    if shutil.which("sandbox-exec") is None:
        raise SandboxError(
            "sandbox-exec is not available on this system.\n"
            "It is only available on macOS. Use --no-sandbox on other platforms."
        )


def _build_profile(
    *,
    project_root: Path,
    worktree_path: Path,
    home: Path,
) -> str:
    """Generate a seatbelt sandbox profile string.

    The profile:
    - Denies everything by default
    - Allows all file reads (agents need system binaries, Python, node, etc.)
    - Allows file writes only to project root, temp dirs, and home caches
    - Allows network only for HTTPS (443) and DNS (53)
    - Allows all process/system/IPC operations (needed for spawning tools)
    """
    # Resolve to real paths (no symlinks) — seatbelt matches real paths
    project_root_str = str(project_root.resolve())
    worktree_str = str(worktree_path.resolve())
    home_str = str(home.resolve())

    return f"""\
(version 1)
(deny default)

;; ── Process & system operations ──
(allow process*)
(allow signal)
(allow sysctl*)
(allow mach*)
(allow ipc*)
(allow iokit*)
(allow system*)

;; ── File reads — unrestricted ──
(allow file-read*)

;; ── File writes — restricted ──
;; Project root (covers worktree inside .po/worktrees/ and .git/)
(allow file-write* (subpath "{project_root_str}"))
;; Worktree (in case it's outside project root)
(allow file-write* (subpath "{worktree_str}"))
;; Temp directories
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/private/var/folders"))
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath "/var/folders"))
;; Home directory caches and config
(allow file-write* (subpath "{home_str}/.claude"))
(allow file-write* (regex #"^{home_str}/\\.claude\\.json"))
(allow file-write* (subpath "{home_str}/.npm"))
(allow file-write* (subpath "{home_str}/.cache"))
(allow file-write* (subpath "{home_str}/.local"))
(allow file-write* (subpath "{home_str}/Library/Caches"))
(allow file-write* (subpath "{home_str}/Library/Preferences"))

;; ── Network — HTTPS + DNS only ──
(allow network-outbound (remote tcp "*:443"))
(allow network-outbound (remote udp "*:53"))
(allow network-outbound (remote tcp "*:53"))
;; Localhost (for IPC, language servers, etc.)
(allow network-outbound (remote tcp "localhost:*"))
(allow network-outbound (remote udp "localhost:*"))
(allow network-inbound (local tcp "localhost:*"))
(allow network-inbound (local udp "localhost:*"))
;; Unix domain sockets
(allow network* (local unix-socket))
(allow network* (remote unix-socket))
"""


class SeatbeltSandbox:
    """Run agent commands inside a macOS sandbox-exec sandbox.

    Filesystem: allows reads everywhere, restricts writes to the project
    root (covering worktrees and .git), temp dirs, and home caches.

    Network: allows HTTPS (port 443) and DNS (port 53). All other
    outbound connections are denied.

    Auth: agents inherit the host shell environment, so OAuth tokens
    from the macOS Keychain work without any special handling.
    """

    def __init__(self) -> None:
        self._profile_dir: str | None = None

    async def prepare(self) -> None:
        """Verify sandbox-exec is available."""
        _check_sandbox_exec()
        logger.info("sandbox-exec is available — agents will be sandboxed")

    def wrap_command(
        self,
        cmd: list[str],
        *,
        worktree_path: Path,
        project_root: Path,
        env: dict[str, str],
    ) -> tuple[list[str], dict[str, str]]:
        """Wrap a command to run inside sandbox-exec."""
        home = Path(env.get("HOME", str(Path.home())))

        profile = _build_profile(
            project_root=project_root,
            worktree_path=worktree_path,
            home=home,
        )

        # Write profile to a temp file (sandbox-exec -f requires a file path).
        # Use a persistent temp dir so the file outlives this method call —
        # the subprocess needs it at exec time.
        if self._profile_dir is None:
            self._profile_dir = tempfile.mkdtemp(prefix="po-sandbox-")
        profile_path = os.path.join(self._profile_dir, "sandbox.sb")
        with open(profile_path, "w") as f:
            f.write(profile)

        sandbox_cmd = ["sandbox-exec", "-f", profile_path] + cmd
        return sandbox_cmd, env
