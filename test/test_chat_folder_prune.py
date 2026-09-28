"""``POST /api/chat/folders/prune`` -- server-side cleanup of empty folders.

The caller only names candidates. The handler checks each named folder's whole
subtree and deletes it only when nothing in it holds a live session, an
archived session, or a setting the person chose. These tests drive the REAL
``FolderRepository.mutate`` so the checks run inside the same transaction as the
delete.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import channel_slots, chat_folders
from kiro_crew.dashboard.channel_slots import close_began, close_ended, note_slot_closed
from kiro_crew.dashboard.chat_folders import api_chat_folder_prune
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.loop_lock import LoopBoundLock

TOP = "fldr000000a1"
MID = "fldr000000a2"
LEAF = "fldr000000a3"
OTHER = "fldr000000b1"


@pytest.fixture(autouse=True)
def _no_channel_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Channel folder names come from THIS host's config; no case may read it."""
    monkeypatch.setattr(chat_folders, "configured_folder_names", lambda: set())


def _tree() -> list[dict[str, Any]]:
    return [
        {"id": TOP, "name": "Top", "parent_id": "", "order": 0},
        {"id": MID, "name": "Mid", "parent_id": TOP, "order": 0},
        {"id": LEAF, "name": "Leaf", "parent_id": MID, "order": 0},
        {"id": OTHER, "name": "Other", "parent_id": "", "order": 1},
    ]


def _state(
    *,
    folders: list[dict[str, Any]] | None = None,
    live: dict[str, str] | None = None,
    archived: list[str] | None = None,
) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = _tree() if folders is None else folders
    slots = {}
    for key, fid in (live or {}).items():
        slot = _ChatSlot(key)
        slot.folder_id = fid
        slots[key] = slot
    state._slots = slots
    state.push_slots_update = MagicMock()
    log = MagicMock()
    log.list_sessions.return_value = [{"folder_id": fid} for fid in (archived or [])]
    state.conversation_log = log

    repo = FolderRepository(lambda: MagicMock())
    lock = LoopBoundLock()

    async def _mutate(fn: Any, on_committed: Any = None, *, prepare: Any = None) -> Any:
        return await repo.mutate(
            lambda: state._folders,
            lock,
            fn,
            lambda: MagicMock(name="folders.json"),
            lambda _path, _snapshot: None,
            on_committed,
            prepare=prepare,
        )

    state.mutate_folders = _mutate
    state._folders_lock = lock
    return state


def _make_app(state: DashboardState, *, app_scope: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = app_scope
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders/prune", api_chat_folder_prune)
    return app


async def _prune(
    state: DashboardState, ids: Any, *, app_scope: str = ""
) -> tuple[int, dict[str, Any]]:
    async with TestClient(TestServer(_make_app(state, app_scope=app_scope))) as client:
        resp = await client.post("/api/chat/folders/prune", json={"ids": ids})
        return resp.status, await resp.json()


def _ids(state: DashboardState) -> set[str]:
    return {f["id"] for f in state._folders}


class TestEmptySubtreesAreDeleted:
    @pytest.mark.asyncio
    async def test_whole_empty_subtree_goes_deepest_first(self) -> None:
        state = _state()
        status, body = await _prune(state, [TOP])
        assert status == 200
        assert body["deleted"] == [LEAF, MID, TOP]
        assert body["skipped"] == []
        assert _ids(state) == {OTHER}
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_naming_parent_and_child_deletes_each_once(self) -> None:
        state = _state()
        status, body = await _prune(state, [TOP, LEAF])
        assert status == 200
        assert body["deleted"] == [LEAF, MID, TOP]
        assert body["skipped"] == []


class TestAnythingInsideKeepsTheWholeSubtree:
    @pytest.mark.asyncio
    async def test_live_session_in_the_leaf_keeps_every_level(self) -> None:
        state = _state(live={"dashboard:chat-1": LEAF})
        status, body = await _prune(state, [TOP])
        assert status == 200
        assert body["deleted"] == []
        assert body["skipped"] == [{"id": TOP, "reason": "not empty: holds sessions"}]
        assert _ids(state) == {TOP, MID, LEAF, OTHER}
        state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_archived_session_in_the_leaf_keeps_every_level(self) -> None:
        state = _state(archived=[LEAF])
        _, body = await _prune(state, [TOP])
        assert body["deleted"] == []
        assert _ids(state) == {TOP, MID, LEAF, OTHER}

    @pytest.mark.asyncio
    async def test_empty_child_of_a_busy_parent_can_still_go(self) -> None:
        state = _state(live={"dashboard:chat-1": MID})
        _, body = await _prune(state, [TOP, LEAF])
        assert body["deleted"] == [LEAF]
        assert body["skipped"] == [{"id": TOP, "reason": "not empty: holds sessions"}]
        assert _ids(state) == {TOP, MID, OTHER}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "field",
        [
            "project_dir",
            "default_agent",
            "tags",
            "steering_dirs",
            "color",
            "icon",
            "channel",
            "owner_app",
        ],
    )
    async def test_a_configured_folder_is_kept(self, field: str) -> None:
        folders = _tree()
        folders[2][field] = ["x"] if field in ("tags", "steering_dirs") else "x"
        state = _state(folders=folders)
        _, body = await _prune(state, [TOP])
        assert body["deleted"] == []
        assert body["skipped"][0]["reason"].startswith("in use")
        assert _ids(state) == {TOP, MID, LEAF, OTHER}

    @pytest.mark.asyncio
    async def test_a_session_filed_inside_the_transaction_is_seen(self) -> None:
        """Live occupancy is read under the lock, not before it.

        The session is filed after the handler's pre-lock archive scan has run,
        which is the window a pre-lock live read would miss.
        """
        state = _state()
        log = state.conversation_log

        def _scan_then_file() -> list[dict[str, Any]]:
            slot = _ChatSlot("dashboard:late")
            slot.folder_id = LEAF
            state._slots["dashboard:late"] = slot
            return []

        log.list_sessions.side_effect = _scan_then_file
        _, body = await _prune(state, [TOP])
        assert body["deleted"] == []
        assert _ids(state) == {TOP, MID, LEAF, OTHER}

    @pytest.mark.asyncio
    async def test_a_folder_a_channel_adopted_by_name_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A channel adopts an existing folder by name without stamping it."""
        monkeypatch.setattr(chat_folders, "configured_folder_names", lambda: {"other"})
        state = _state()
        _, body = await _prune(state, [OTHER, TOP])
        assert body["deleted"] == [LEAF, MID, TOP]
        assert [s["id"] for s in body["skipped"]] == [OTHER]

    @pytest.mark.asyncio
    async def test_a_folder_a_scheduled_job_files_into_is_kept(self) -> None:
        """A cron job's chat folder is named only in the job, not on the row."""
        state = _state()
        crons = MagicMock()
        crons.list_jobs.return_value = [MagicMock(chat_folder_id=LEAF)]
        state.crons = crons
        _, body = await _prune(state, [TOP, OTHER])
        assert body["deleted"] == [OTHER]
        assert body["skipped"][0]["reason"].startswith("in use: a scheduled job")
        crons.list_jobs.assert_called_with(include_disabled=True)

    @pytest.mark.asyncio
    async def test_a_subtree_with_a_repeated_id_is_kept(self) -> None:
        """The second row with an id shadows the first, whose settings go unread."""
        folders = _tree()
        folders.insert(0, {"id": LEAF, "name": "Real", "parent_id": MID, "project_dir": "/p"})
        state = _state(folders=folders)
        _, body = await _prune(state, [TOP])
        assert body["deleted"] == []
        assert body["skipped"][0]["reason"] == "in use: another folder shares its id"
        assert len(state._folders) == 5

    @pytest.mark.asyncio
    async def test_channel_names_are_read_under_the_folder_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A channel save commits its name, then adopts under the folder lock.

        Read before the lock, the names can miss a save that commits and adopts
        in between, and the adopted folder is deleted. Read under it, that save
        either shows in the names or adopts after the prune.
        """
        state = _state()
        held: list[bool] = []

        def _names() -> set[str]:
            held.append(state._folders_lock.locked())
            return {"other"}

        monkeypatch.setattr(chat_folders, "configured_folder_names", _names)
        _, body = await _prune(state, [OTHER])
        assert held == [True]
        assert body["deleted"] == []

    @pytest.mark.asyncio
    async def test_a_session_closed_during_the_scan_keeps_its_folder(self) -> None:
        """Filed after the scan read the disk, then closed before the lock.

        The session is in neither the scan nor the live slots, so only the close
        record can keep its folder.
        """
        state = _state()

        def _scan_while_a_session_closes() -> list[dict[str, Any]]:
            close_began(state)
            note_slot_closed(state, "dashboard:gone", folder_id=LEAF)
            close_ended(state)
            return []

        state.conversation_log.list_sessions.side_effect = _scan_while_a_session_closes
        _, body = await _prune(state, [TOP, OTHER])
        assert body["deleted"] == [OTHER]
        assert [s["id"] for s in body["skipped"]] == [TOP]
        assert body["skipped"][0]["reason"].startswith("busy")
        assert _ids(state) == {TOP, MID, LEAF}

    @pytest.mark.asyncio
    async def test_a_close_still_saving_at_scan_time_keeps_its_folder(self) -> None:
        """The close left the live slots before the scan but has not written yet."""
        state = _state()
        close_began(state)
        note_slot_closed(state, "dashboard:gone", folder_id=LEAF)
        try:
            _, body = await _prune(state, [TOP])
        finally:
            close_ended(state)
        assert body["deleted"] == []

    @pytest.mark.asyncio
    async def test_a_finished_close_does_not_pin_the_folder(self) -> None:
        """Once no close is in flight, the scan sees every earlier close's save."""
        state = _state()
        close_began(state)
        note_slot_closed(state, "dashboard:gone", folder_id=LEAF)
        close_ended(state)
        _, body = await _prune(state, [TOP])
        assert body["deleted"] == [LEAF, MID, TOP]


def test_folder_close_records_are_dropped_once_nothing_can_read_them() -> None:
    """A record is kept only while a close or a prune that may read it is in flight."""
    state = _state()
    since = channel_slots.prune_began(state)
    for n in range(50):
        close_began(state)
        note_slot_closed(state, f"dashboard:s{n}", folder_id=f"gone{n:08d}")
        close_ended(state)
    assert len(channel_slots.folders_closed_since(state, since)) == 50
    channel_slots.prune_ended(state, since)
    assert not channel_slots._RECENT_FOLDER_CLOSES.get(state)

    close_began(state)
    note_slot_closed(state, "dashboard:x", folder_id=LEAF)
    assert set(channel_slots._RECENT_FOLDER_CLOSES[state]) == {LEAF}
    close_ended(state)
    assert not channel_slots._RECENT_FOLDER_CLOSES.get(state)


def test_every_close_records_its_folder() -> None:
    """Prune relies on each tab close naming the folder it left."""
    import pathlib

    root = pathlib.Path(chat_folders.__file__).resolve().parents[1]
    calls = [
        (path, line)
        for path in root.rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if "note_slot_closed(" in line and "def note_slot_closed" not in line
    ]
    assert calls, "no close records found; update this pin"
    missing = [f"{p.name}: {line.strip()}" for p, line in calls if "folder_id=" not in line]
    assert not missing, f"a close must pass folder_id=: {missing}"


def test_the_single_tab_close_records_its_folder_at_the_pop() -> None:
    """The slot can be refiled between the close's start and its pop."""
    from kiro_crew.dashboard import chat_handlers

    src = inspect.getsource(chat_handlers._close_slot)
    pop = src.index("state._slots.pop(name, None)")
    assert src.index("note_folder_left(", pop) - pop < 120


def test_both_close_entry_points_bracket_the_close() -> None:
    """Each caller of note_slot_closed runs inside close_began / close_ended."""
    from kiro_crew.dashboard import chat_handlers

    src = inspect.getsource(chat_handlers.close_slot)
    assert "close_began(" in src and "close_ended(" in src
    bracket = inspect.getsource(chat_handlers._brackets_closes)
    assert "close_began(" in bracket and "close_ended(" in bracket
    cleanup = inspect.getsource(chat_handlers.api_chat_slots_cleanup)
    assert cleanup.startswith("@_brackets_closes\n")


#: Row fields that are NOT settings prune must keep: identity, where the folder
#: sits, view state, and bookkeeping.
_IGNORABLE = {
    "id",
    "name",
    "collapsed",
    "hidden",
    "order",
    "parent_id",
    "created_at",
    "arrival_adopted",
}


def _row_builders() -> list[str]:
    """Sources that build a whole folder row as a dict literal."""
    from kiro_crew.dashboard import arrival_folders, channel_folders

    return [
        inspect.getsource(chat_folders.create_folder_record),
        inspect.getsource(channel_folders.ensure_channel_folder),
        inspect.getsource(arrival_folders._new_folder),
    ]


def test_every_folder_row_field_is_classified_for_prune() -> None:
    """Every field a folder writer stores must be sorted into keep or ignorable.

    Otherwise a folder carrying only that new field would read as empty and be
    pruned with it.
    """
    update = inspect.getsource(chat_folders.api_chat_folder_update)
    fields = set(re.findall(r'if "([a-z_]+)" in body', update))
    for source in _row_builders():
        fields |= set(re.findall(r'folder\["([a-z_]+)"\]\s*=', source))
        fields |= set(re.findall(r'^\s+"([a-z_]+)": ', source, re.MULTILINE))
    fields.add("arrival_adopted")
    assert {"project_dir", "channel", "tags"} <= fields, "writers moved; update this pin"
    unclassified = fields - set(chat_folders._PRUNE_KEEP_FIELDS) - _IGNORABLE
    assert not unclassified, f"classify for prune: {sorted(unclassified)}"


class TestRefusals:
    @pytest.mark.asyncio
    async def test_an_app_is_refused_and_nothing_changes(self) -> None:
        state = _state()
        status, body = await _prune(state, [TOP], app_scope="issue-radar")
        assert status == 403
        assert body["code"] == "folder_delete_forbidden"
        assert _ids(state) == {TOP, MID, LEAF, OTHER}

    @pytest.mark.asyncio
    async def test_unknown_id_is_reported_not_fatal(self) -> None:
        state = _state()
        _, body = await _prune(state, ["fldr_missing", OTHER])
        assert body["deleted"] == [OTHER]
        assert body["skipped"] == [{"id": "fldr_missing", "reason": "not found"}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ids", [[], "x", [1], [""], ["a"] * 101])
    async def test_malformed_ids_are_a_400(self, ids: Any) -> None:
        state = _state()
        status, _ = await _prune(state, ids)
        assert status == 400
        assert _ids(state) == {TOP, MID, LEAF, OTHER}
