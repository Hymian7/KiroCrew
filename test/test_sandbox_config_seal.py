"""The runtime config is sealed read-only for every sandboxed process.

``config.json`` and ``config.local.json`` carry the switches that loosen confinement
(``agent.sandbox``, ``agent.apps_allow_third_party``, ...). The file-edit tool fence
never sees a spawned shell's ``open()``, so the OS disposition is the load-bearing half:
without it an in-sandbox shell could write ``agent.sandbox: "off"`` and run its next
spawn unconfined. Both files are sealed because the loader merges the overlay over the
base with the overlay winning.
"""

from __future__ import annotations

import argparse
import ast
import errno
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, sandbox

_CONFIG_LEAVES = ("config.json", "config.local.json")


@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_config_leaf_is_read_only_in_every_mode(leaf):
    assert leaf in sandbox._CREW_READONLY_LEAVES
    assert leaf not in sandbox._CREW_HIDDEN_LEAVES
    assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    for prefix in (".kiro/crew", ".kirocrew"):
        assert f"{prefix}/{leaf}" in sandbox._CREW_READONLY_TARGETS


@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_config_leaf_is_precreated_so_the_linux_bind_has_a_file(leaf):
    # An absent overlay is the default state, and an absent name is exactly the one an
    # agent would create to win the merge.
    assert leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES


def _set_args(*, local: bool = True):
    return argparse.Namespace(
        config_action="set", key="agent.sandbox", value="off", file=None, local=local
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data home with the two config paths pointed at it, and NO sandbox marker.

    ``cli.main()`` pops ``KIROCREW_SANDBOX_ACTIVE`` before dispatch, so the marker is
    never in the environment when ``kirocrew config`` runs -- inside the sandbox or
    out. The hint has to be decided from the failure itself; a test that set the
    marker would pass against a hint that never fires in production.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    d = tmp_path / "crew"
    d.mkdir()
    (d / "config.json").write_text(
        json.dumps({"session": {"autocompact_pct": 90.0}}), encoding="utf-8"
    )
    with (
        patch("kiro_crew.cli_config.config_path", return_value=d / "config.json"),
        patch("kiro_crew.cli_config.config_local_path", return_value=d / "config.local.json"),
        patch("kiro_crew.config.loader.config_path", return_value=d / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=d),
        patch("kiro_crew.cli_config.sel"),
    ):
        yield d


def _assert_hint(err: str) -> None:
    assert "read-only inside the agent sandbox" in err
    assert "dashboard" in err


@pytest.mark.parametrize("code", [errno.EPERM, errno.EACCES, errno.EBUSY, errno.EROFS])
@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_sandboxed_config_set_reports_the_seal_on_an_in_place_write(home, capsys, code, leaf):
    """``open(path, "w")`` refused by the seal names the sealed file as ``filename``."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(code, "denied", str(home / leaf)),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(_set_args(local=leaf == "config.local.json"))
    assert exc.value.code == 1
    _assert_hint(capsys.readouterr().err)


@pytest.mark.parametrize("code", [errno.EPERM, errno.EBUSY])
def test_sandboxed_config_set_reports_the_seal_on_the_publishing_rename(home, capsys, code):
    """``os.replace(tmp, path)`` names the temp as ``filename`` and the seal as ``filename2``.

    That is the shape ``atomic_write`` fails with: the temp lands (the data-home root is
    writable), and the rename over the sealed leaf is what the OS refuses -- ``EPERM``
    from Seatbelt, ``EBUSY`` from a Linux bind mount.
    """
    from kiro_crew.cli_config import _config_cmd

    tmp = home / "tmpa1b2c3.tmp"
    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(code, "denied", str(tmp), None, str(home / "config.local.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(_set_args())
    assert exc.value.code == 1
    _assert_hint(capsys.readouterr().err)


def test_a_denial_against_an_unrelated_file_is_not_relabelled(home):
    """Errno alone is not the seal: the same errno against another path is re-raised."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=PermissionError(errno.EPERM, "denied", str(home / "other.json")),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(_set_args())


def test_a_denial_with_no_filename_is_not_relabelled(home):
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=PermissionError(errno.EPERM, "denied"),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(_set_args())


def test_a_non_denial_errno_against_the_config_is_not_relabelled(home):
    """A full data home names the same file but is not the seal."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.ENOSPC, "full", str(home / "config.local.json")),
    ):
        with pytest.raises(OSError) as exc:
            _config_cmd(_set_args())
    assert exc.value.errno == errno.ENOSPC


def test_config_edit_editor_exec_denial_is_not_relabelled(home):
    """``config edit`` failing to exec the editor is an editor problem, not the seal."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.os.execvp",
        side_effect=PermissionError(errno.EACCES, "denied", "vi"),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(argparse.Namespace(config_action="edit"))


def test_sandboxed_config_defaults_adopt_reports_the_seal(home, capsys):
    """``config defaults --adopt`` writes ``config.json`` through its own error path."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.EPERM, "denied", str(home / "config.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(
                argparse.Namespace(config_action="defaults", keys=[], adopt=True, keep=False)
            )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    _assert_hint(err)
    assert "Could not write" not in err


def test_config_defaults_adopt_keeps_its_own_message_for_other_failures(home, capsys):
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.ENOSPC, "full", str(home / "config.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(
                argparse.Namespace(config_action="defaults", keys=[], adopt=True, keep=False)
            )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Could not write" in err
    assert "read-only inside the agent sandbox" not in err


# ── The seal must survive the owner's own saves ───────────────────────────────
# On Linux the read-only seal is a per-file bind mount, and a file bind is pinned to
# the INODE it was made over. A temp+rename publish installs a new inode at the name,
# so every sandbox already running would see the new file unsealed. The owner's write
# therefore has to land through the inode that is already there.


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="the bind-mount seal is a POSIX concern; Windows keeps temp+rename",
)
@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_config_write_keeps_the_inode_the_seal_is_pinned_to(tmp_path, leaf):
    from kiro_crew.config.loader import write_config_atomically

    p = tmp_path / leaf
    p.write_text(json.dumps({"agent": {"model": "a" * 50}}) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    before = p.stat()

    # A shorter document, then a longer one: both directions of the in-place write.
    write_config_atomically(p, {"agent": {"model": "b"}})
    assert json.loads(p.read_text(encoding="utf-8")) == {"agent": {"model": "b"}}
    longer = {"agent": {"model": "c" * 400}}
    write_config_atomically(p, longer, fsync=True)
    after = p.stat()

    assert json.loads(p.read_text(encoding="utf-8")) == longer
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert stat.S_IMODE(after.st_mode) == 0o600


def test_config_write_creates_an_absent_file_owner_only(tmp_path):
    from kiro_crew.config.loader import write_config_atomically

    p = tmp_path / "crew" / "config.local.json"
    write_config_atomically(p, {"agent": {"sandbox": "auto"}})
    assert json.loads(p.read_text(encoding="utf-8")) == {"agent": {"sandbox": "auto"}}
    if platform_compat.IS_POSIX:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
#: Zero-argument path accessors for the two sealed leaves. An app's own
#: ``config_path(root)`` takes an argument and names a different file.
_CONFIG_ACCESSORS = frozenset({"config_path", "config_local_path"})
#: Calls that install a NEW inode at their destination (or truncate-write it
#: outside the shared writer): ``atomic_write(path, ...)``, ``os.replace(src, dst)``
#: and the ``Path`` methods on the destination itself.
_FIRST_ARG_PUBLISHERS = frozenset({"atomic_write", "atomic_write_at"})
_DESTINATION_PUBLISHERS = frozenset({"replace", "rename"})  # os.replace / os.rename
_PATH_METHOD_PUBLISHERS = frozenset({"replace", "rename", "write_text", "write_bytes"})


def _is_config_accessor_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    fn = node.func
    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
    return name in _CONFIG_ACCESSORS


def _config_publishes_outside_the_shared_writer(scope: ast.AST) -> list[int]:
    """Line numbers in *scope* that publish a sealed config leaf directly."""
    bound: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and _is_config_accessor_call(node.value):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def is_config(target: ast.AST | None) -> bool:
        if target is None:
            return False
        if isinstance(target, ast.Name):
            return target.id in bound
        return _is_config_accessor_call(target)

    hits: list[int] = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in _FIRST_ARG_PUBLISHERS:
            target = node.args[0] if node.args else None
            for kw in node.keywords:
                if kw.arg == "path":
                    target = kw.value
            if is_config(target):
                hits.append(node.lineno)
        elif isinstance(fn, ast.Attribute):
            receiver = fn.value
            if (
                isinstance(receiver, ast.Name)
                and receiver.id == "os"
                and fn.attr in _DESTINATION_PUBLISHERS
            ):
                if len(node.args) > 1 and is_config(node.args[1]):
                    hits.append(node.lineno)
            elif fn.attr in _PATH_METHOD_PUBLISHERS and is_config(receiver):
                hits.append(node.lineno)
    return hits


def test_no_source_publishes_the_sealed_config_outside_write_config_atomically():
    """Every writer of ``config.json`` / ``config.local.json`` uses the shared writer.

    ``write_config_atomically`` is what keeps the inode (and so the seal) on POSIX. A
    direct ``atomic_write`` / ``os.replace`` onto either leaf would re-open the hole
    on the next save. Scoped per function so an unrelated local named ``cp`` in
    another function is not a hit; the loader's own writer takes ``path`` as a
    parameter and is not bound from an accessor, so it needs no exemption.
    """
    offenders: list[str] = []
    for source in sorted(_SRC_ROOT.rglob("*.py")):
        if "_vendor" in source.parts:
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        if "config_path()" not in text and "config_local_path()" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        scopes = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for scope in scopes:
            for lineno in _config_publishes_outside_the_shared_writer(scope):
                rel = source.relative_to(_SRC_ROOT.parent.parent)
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "config.json / config.local.json must be published through "
        "write_config_atomically, which writes in place so the sandbox's read-only "
        "bind (pinned to the inode) survives the save:\n  " + "\n  ".join(offenders)
    )
