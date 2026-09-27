"""One-shot kiro-cli calls must not leave helpers running after they return.

A kiro-cli launcher wrapper can start a credential helper for a call as
short as ``kiro-cli chat --list-models`` and leave it running when
the call returns. ``/api/models`` re-polls every 8s while degraded, so one
gateway leaked a ~140-thread helper per poll until its agent cgroup hit
``pids.max``. These tests pin the supervisor's ``--reap-survivors`` mode that
ends such leftovers, and that the one-shot spawn sites use it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import _process_group_supervisor as supervisor
from kiro_crew import kiro_prerequisite, platform_compat
from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

# Above every supported platform's pid_max, so no cleanup path can reach a live
# process by it (same spelling as test/test_update_provider.py).
_UNALLOCATABLE_PID = 99_999_999_999

posix_only = pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX process groups")
reaping = pytest.mark.skipif(not supervisor.can_reap(), reason="reaping needs Linux pidfd support")

# A command that starts a detached-looking helper in its own process group (the
# credential-helper shape: same group, parent about to exit) and then returns at once.
# The helper records its pid so the test can check whether it survived.
# The pid file is published atomically (write, then rename), so a reader that sees
# it can always parse it.
_LEAKY_COMMAND = """
import os, subprocess, sys
helper = subprocess.Popen(
    [sys.executable, "-c", {helper!r}],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
with open({pid_file!r} + ".tmp", "w") as out:
    out.write(str(helper.pid))
os.replace({pid_file!r} + ".tmp", {pid_file!r})
sys.exit(3)
"""

_HELPER_SLEEPS = "import time; time.sleep(120)"
_HELPER_IGNORES_TERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"
)


def _alive(pid: int) -> bool:
    """Running and not a zombie (a zombie holds no resources)."""
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        return stat.rpartition(")")[2].split()[0] != "Z"
    return platform_compat.pid_exists(pid)


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@contextlib.contextmanager
def _supervised(tmp_path: Path, helper: str, *flags: str) -> Iterator[tuple[int, int, float]]:
    """Run the supervisor over a command that leaves a helper behind.

    Yields ``(exit code, helper pid, seconds)`` while the supervisor has exited
    but is still UNREAPED: its zombie keeps its pid, and so the group id, from
    being reissued. Cleanup of anything left in the group happens then, through
    the pinned signal, and only afterwards is the supervisor reaped. Nothing here
    ever signals a group number the test does not hold a handle on.
    """
    pid_file = tmp_path / "helper.pid"
    code = Path(supervisor.__file__).read_text(encoding="utf-8")
    command = _LEAKY_COMMAND.format(helper=helper, pid_file=str(pid_file))
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-local
        [
            sys.executable,
            "-I",
            "-c",
            code,
            *flags,
            os.path.realpath(sys.executable),
            "-c",
            command,
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        info = None
        deadline = time.monotonic() + 60
        while info is None and time.monotonic() < deadline:
            # WNOWAIT: observe the exit without reaping, so the pgid stays ours.
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if info is None:
                time.sleep(0.05)
        if info is None:
            # Still running, so still leading its group: the group id is ours.
            os.killpg(proc.pid, 9)
            raise AssertionError("supervisor did not exit within 60s")
        elapsed = time.monotonic() - started
        returncode = info.si_status if info.si_code == os.CLD_EXITED else 128 + info.si_status
        yield returncode, int(pid_file.read_text()), elapsed
    finally:
        if supervisor.can_reap():
            for pid in supervisor._group_members(proc.pid) - {proc.pid}:
                supervisor._signal_member(pid, proc.pid, 9)
        proc.wait()


@reaping
@pytest.mark.parametrize(
    "helper",
    [_HELPER_SLEEPS, _HELPER_IGNORES_TERM],
    ids=["exits-on-term", "ignores-term"],
)
def test_reap_survivors_ends_what_the_command_left_behind(tmp_path: Path, helper: str) -> None:
    with _supervised(tmp_path, helper, "--reap-survivors") as (returncode, helper_pid, elapsed):
        # The command's own exit status comes back, not the supervisor's.
        assert returncode == 3
        # The helper is gone: SIGTERM, or SIGKILL after the grace for one that
        # ignores SIGTERM. The call returns in about that grace, not 120s.
        assert _wait_gone(helper_pid), "helper survived the supervised call"
        assert elapsed < 20


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="needs os.waitid (not on macOS)")
def test_without_the_flag_the_supervisor_still_waits_for_the_group(tmp_path: Path) -> None:
    # The default mode is what _run_process relies on: the leader keeps the
    # group anchored until the last member exits on its own.
    helper = "import time; time.sleep(1.5)"
    with _supervised(tmp_path, helper) as (returncode, helper_pid, elapsed):
        assert returncode == 3
        assert elapsed >= 1.4
        assert _wait_gone(helper_pid)


@posix_only
def test_unknown_leading_flag_is_refused(tmp_path: Path) -> None:
    code = Path(supervisor.__file__).read_text(encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - fixed argv, test-local
        [sys.executable, "-I", "-c", code, "--no-such-flag", "/bin/true"],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        start_new_session=True,
    )
    assert done.returncode == 127


# Leads its own group and stays alive for the whole test. It starts the
# supervisor WITHOUT a new session, so the supervisor is a member of this group
# and not its leader, and must not reap it. The parent then checks the helper,
# writes the verdict, and SIGKILLs its own group, itself included. That is safe:
# the killer is a live member of the group it kills.
_NON_LEADER_PARENT = """
import os, signal, subprocess, sys, time
subprocess.Popen(
    [sys.executable, "-I", "-c", {code!r}, "--reap-survivors", sys.executable, "-c", {command!r}],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
verdict = "no-helper"
deadline = time.monotonic() + 30
while not os.path.exists({pid_file!r}) and time.monotonic() < deadline:
    time.sleep(0.05)
if os.path.exists({pid_file!r}):
    helper = int(open({pid_file!r}).read())
    # A reaping supervisor would end the helper within a poll or two.
    time.sleep(1.5)
    try:
        state = open(f"/proc/{{helper}}/stat").read().rpartition(")")[2].split()[0]
        verdict = "gone" if state == "Z" else "alive"
    except OSError:
        verdict = "gone"
with open({verdict_file!r}, "w") as out:
    out.write(verdict)
os.killpg(0, signal.SIGKILL)
"""


@reaping
def test_reap_is_skipped_when_the_supervisor_does_not_lead_its_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "helper.pid"
    verdict_file = tmp_path / "verdict"
    code = Path(supervisor.__file__).read_text(encoding="utf-8")
    command = _LEAKY_COMMAND.format(helper="import time; time.sleep(30)", pid_file=str(pid_file))
    parent = subprocess.Popen(  # noqa: S603 - fixed argv, test-local
        [
            sys.executable,
            "-c",
            _NON_LEADER_PARENT.format(
                code=code,
                command=command,
                pid_file=str(pid_file),
                verdict_file=str(verdict_file),
            ),
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        parent.wait(timeout=60)
    finally:
        if parent.returncode is None:
            # Still alive, so still leading its group: the group id is ours.
            os.killpg(parent.pid, 9)
            parent.wait()
    assert verdict_file.read_text() == "alive", "supervisor reaped a group it does not lead"


@pytest.mark.skipif(sys.platform != "linux", reason="pidfd syscalls are Linux-only")
def test_pidfd_fallback_through_ctypes_signals_a_group_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Standalone CPython builds that lack os.pidfd_open are exactly the hosts that
    # motivated the fix; without the ctypes path can_reap() is False there.
    #
    # Decide the host's TRUE pidfd_open capability with a witness independent of
    # the production ctypes constant, so a wrong _SYS_PIDFD_OPEN never disguises
    # itself as an incapable host and skips the ratchet:
    #   * stdlib os.pidfd_open present -> use it (the kernel answers directly);
    #   * absent (wrapper-less python-build-standalone) -> issue the LITERAL
    #     pidfd_open syscall number (434) via ctypes here in the test.
    # Skip ONLY when that independent witness itself fails (old kernel, or seccomp
    # refusing the syscall). On a capable host the deleted-wrapper ctypes path in
    # _pidfd_open MUST reproduce the capability, so any failure there (e.g. a wrong
    # _SYS_PIDFD_OPEN constant, whose ENOSYS is otherwise indistinguishable from a
    # refusing host) FAILS the assertion below rather than skipping — the ratchet
    # stays live on the very hosts the fallback exists for.
    _PIDFD_OPEN_SYSCALL = 434  # same on every Linux arch; independent of production

    def _host_can_pidfd_open() -> bool:
        opener = getattr(os, "pidfd_open", None)
        try:
            if opener is not None:
                os.close(opener(os.getpid()))
                return True
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.syscall(_PIDFD_OPEN_SYSCALL, os.getpid(), 0)
            if fd < 0:
                return False
            os.close(fd)
            return True
        except OSError:
            return False

    host_can_pidfd_open = _host_can_pidfd_open()

    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)

    if not host_can_pidfd_open:
        pytest.skip(
            "pidfd_open is unavailable on this host (old kernel, or seccomp "
            "refuses the syscall); the ctypes fallback cannot be exercised where "
            "the capability itself is missing"
        )
    # The host CAN pidfd_open, so the ctypes fallback MUST reproduce that — any
    # OSError here is a real fallback regression, asserted below, not skipped.
    assert supervisor.can_reap()
    child = subprocess.Popen(  # noqa: S603 - fixed argv, test-local
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # The unreaped child anchors its own group id while this runs.
        assert supervisor._signal_member(child.pid, child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.returncode is None:
            child.kill()
            child.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="pidfd syscalls are Linux-only")
def test_ctypes_pidfd_fallback_branches_are_exercised_without_the_stdlib_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Host-independent coverage of the ctypes pidfd fallback: with the stdlib
    # wrappers deleted, _pidfd_open / _pidfd_send_signal route through _syscall,
    # and _syscall raises OSError on a negative return. libc is faked so this
    # holds on any host (a kernel/seccomp that refuses pidfd_open otherwise
    # leaves these branches unrun on the very hosts they exist for).
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)

    calls: list[tuple[object, ...]] = []

    class _FakeLibc:
        def __init__(self) -> None:
            self.rc = 0

        def syscall(self, number: int, *args: object) -> int:
            calls.append((number, *args))
            return self.rc

    fake = _FakeLibc()
    monkeypatch.setattr(supervisor, "_libc", fake)

    # Success path: the fallback issues the pidfd_open syscall and returns its fd.
    fake.rc = 7
    assert supervisor._pidfd_open(4321) == 7
    assert calls[-1][0] == supervisor._SYS_PIDFD_OPEN

    # Send-signal fallback issues the send-signal syscall (return >= 0 is fine).
    fake.rc = 0
    supervisor._pidfd_send_signal(7, signal.SIGKILL)
    assert calls[-1][0] == supervisor._SYS_PIDFD_SEND_SIGNAL

    # A negative syscall return is raised as OSError, never swallowed.
    fake.rc = -1
    with pytest.raises(OSError):
        supervisor._pidfd_open(4321)


@reaping
def test_signal_member_refuses_a_pid_outside_the_group() -> None:
    # The identity check, not the pid, decides: this test process is alive but
    # not in that group, so it must not be signalled.
    assert supervisor._signal_member(os.getpid(), _UNALLOCATABLE_PID, 0) is False


def _spawn_capture() -> AsyncMock:
    return AsyncMock(return_value=SimpleNamespace(pid=_UNALLOCATABLE_PID))


@pytest.fixture
def reap_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the gateway-side capability probe on, so argv tests run on any POSIX host."""
    monkeypatch.setattr(kiro_prerequisite, "_host_can_reap", lambda: True)


def test_spawn_supervised_oneshot_skips_the_supervisor_where_it_cannot_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without pidfd the supervisor would only WAIT for leftovers, holding the call
    # open; the spawn must then be exactly today's plain one, in its own session.
    monkeypatch.setattr(kiro_prerequisite, "_host_can_reap", lambda: False)
    spawn = _spawn_capture()
    with patch.object(kiro_prerequisite, "create_subprocess_limited", spawn):
        asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["/usr/bin/env", "x"]))
    assert list(spawn.await_args.args) == ["/usr/bin/env", "x"]
    assert spawn.await_args.kwargs == {"start_new_session": True}


@posix_only
def test_unsupervised_spawns_are_noted_once_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(kiro_prerequisite, "_host_can_reap", lambda: False)
    monkeypatch.setattr(kiro_prerequisite, "_unsupervised_noted", False)
    with (
        patch.object(kiro_prerequisite, "create_subprocess_limited", _spawn_capture()),
        caplog.at_level(logging.INFO, logger=kiro_prerequisite.logger.name),
    ):
        for _ in range(3):
            asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["/usr/bin/env", "x"]))
    notes = [r for r in caplog.records if "without the reaping supervisor" in r.getMessage()]
    assert len(notes) == 1
    assert notes[0].levelno == logging.INFO


@pytest.mark.usefixtures("reap_capable")
def test_spawn_supervised_oneshot_wraps_the_command_in_its_own_session() -> None:
    spawn = _spawn_capture()
    with patch.object(kiro_prerequisite, "create_subprocess_limited", spawn):
        asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["/usr/bin/env", "x"], env={}))
    args = list(spawn.await_args.args)
    assert args[:3] == [sys.executable, "-I", "-c"]
    assert args[3] == kiro_prerequisite._PROCESS_GROUP_SUPERVISOR_CODE
    assert args[4:] == ["--reap-survivors", "/usr/bin/env", "x"]
    assert spawn.await_args.kwargs == {"start_new_session": True, "env": {}}


@posix_only
@pytest.mark.usefixtures("reap_capable")
def test_spawn_supervised_oneshot_resolves_a_relative_wrapper() -> None:
    spawn = _spawn_capture()
    with (
        patch.object(platform_compat, "trusted_system_bin", return_value="/usr/bin/env"),
        patch.object(kiro_prerequisite, "create_subprocess_limited", spawn),
    ):
        asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["env", "x"]))
    assert list(spawn.await_args.args)[-2:] == ["/usr/bin/env", "x"]


@posix_only
@pytest.mark.usefixtures("reap_capable")
def test_spawn_supervised_oneshot_runs_an_unresolvable_wrapper_unsupervised() -> None:
    spawn = _spawn_capture()
    with (
        patch.object(platform_compat, "trusted_system_bin", return_value=None),
        patch.object(kiro_prerequisite, "create_subprocess_limited", spawn),
    ):
        asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["nope", "x"]))
    assert list(spawn.await_args.args) == ["nope", "x"]
    assert spawn.await_args.kwargs["start_new_session"] is True


class _FakeProc:
    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout
        self.returncode = 0
        self.pid = _UNALLOCATABLE_PID

    def kill(self) -> None:
        pass

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


@posix_only
@pytest.mark.usefixtures("reap_capable")
def test_api_models_spawns_the_list_under_the_reaping_supervisor(tmp_path: Path) -> None:
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()
    spawn = AsyncMock(return_value=_FakeProc(payload))
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        audit_writer=_no_audit,
        assume_ready=True,
    )
    request = MagicMock()
    request.app = {"kiro_prerequisite_service": service}
    cfg = SimpleNamespace(agent=SimpleNamespace(provider="kiro"))
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=cfg),
        patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
        patch("kiro_crew.env.augmented_path", lambda p: p),
        patch(
            "kiro_crew.dashboard.handlers.agents.wrap_argv",
            lambda argv, **kwargs: (argv, None),
        ),
        patch("kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv),
        patch("kiro_crew.sandbox.resource_limit_preexec", lambda: None),
        patch.object(agents.asyncio, "create_subprocess_exec", spawn),
    ):
        resp = asyncio.run(agents.api_models(request))

    assert resp.status == 200
    argv = [str(a) for a in spawn.await_args.args]
    code = kiro_prerequisite._PROCESS_GROUP_SUPERVISOR_CODE
    at = argv.index(code)
    assert argv[at + 1] == "--reap-survivors"
    # The supervisor wraps the whole command, so the model list runs inside it.
    assert argv.index("/usr/bin/kiro-cli") > at
    assert spawn.await_args.kwargs["start_new_session"] is True
