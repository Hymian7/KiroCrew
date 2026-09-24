"""Bringing the two authority files back, before the backend can overwrite them.

## Why this runs before the backend and not beside it

``session_map.json`` and ``open_slots.json`` are what turn a slot id back into a
conversation. The backend flushes them periodically from its own in-memory state, so
a backend that starts before they are on disk starts with an empty slot table and
then PERSISTS that emptiness over the restored files. The conversation list comes up
blank, the transcripts are still on disk, and nothing reports a fault.

So the restore is not "early for speed". Finishing before the backend starts is the
correctness rule, and this function is called from the supervisor's startup order
where that is enforced, not from the sidecar, which does not exist yet at that point.

Transcripts are deliberately NOT restored here. The front fetches the one transcript
a turn continues, on that turn, which keeps the property that a task only ever holds
the conversations it has itself served. Downloading them all at boot would undo that
and would need a bucket listing, which the front's reader cannot do.

## Why the bytes are validated before they are written

Both of the backend's own readers ignore an authority file they cannot parse and
carry on with an empty result. That is right for them and wrong for this step: a
malformed object written here would be read as "no conversations" and then replaced by
the flush, so the restore would look like it worked and the customer's list would be
empty. Refusing to boot instead turns a silent loss into a message an operator gets
before the task serves a turn.

The check is exactly as strict as those readers require -- the file must be a JSON
object, and ``open_slots.json``'s ``keys`` must be a list if it is present -- and no
stricter. A schema invented here would refuse a file the backend would have accepted.

## What an existing local file means

It is kept, and it is READ before it is kept. Nothing has started, so a file already at
the path did not come from this boot's backend: it came from a data home that outlived
the task, and that copy leads the bucket by up to one backup interval. Overwriting it
would roll a conversation list backwards. ``link_new`` makes that a filesystem guarantee
rather than a check.

Keeping it is not the same as trusting it. The same reading the bucket's bytes get is
applied to the local file, because the backend loads whichever copy is on that path and
loses it the same way -- so the branch that skips the write must not also skip the check.
A local file that would be read as no conversations refuses the boot, rather than being
replaced by the bucket's copy: replacing it would roll the list backwards, while a
refusal leaves both copies where an operator can repair either. That reading also covers
a name the bucket holds nothing for, since a file there boots the backend whether or not
anything was restored over it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ..common import Settings, keys, statefile
from ..common.config import MAX_OBJECT_BYTES
from . import generation
from .store import ObjectAbsent, ObjectStore, StoreUnusable

log = logging.getLogger("smc.sidecar.restore")

__all__ = [
    "RestoreFailed",
    "RestoreResult",
    "validate_authority",
    "restore_authority",
]


class RestoreFailed(RuntimeError):
    """The authority files could not be restored, so the task must not start.

    Fail-closed on purpose. Every alternative -- boot without them, boot with some of
    them, boot with bytes that did not parse -- ends the same way: the backend flushes
    an empty slot table over the real one and the customer's conversation list is gone
    with nothing to say so.
    """


@dataclass
class RestoreResult:
    """What the restore did, per authority file."""

    restored: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.restored)} restored, {len(self.absent)} not in the bucket, "
            f"{len(self.kept_local)} already on disk"
        )


def validate_authority(name: str, raw: bytes, *, origin: str = "in the bucket") -> None:
    """Refuse bytes the backend would silently ignore. Returns nothing on success.

    Whole-file shapes and INDIVIDUAL entries both, because the backend discards at both
    levels and the consequence is the same: what it does not load, its next flush deletes
    from the bucket. A file that parses and whose entries it drops one by one is not a
    safer state than a file that does not parse -- it is the same loss arriving quietly.

    A refusal is the recoverable choice and an acceptance is not, which is what settles
    every case here. Refusing leaves the bytes in the bucket for a repair to read;
    accepting spends them. That is also what separates this from refusing an authority
    file the bucket never held: there, a refusal protects nothing, because there are no
    bytes behind the absence.

    *origin* says WHERE these bytes came from, because the same reading is applied to two
    of them: an object fetched from the bucket, and a file already at the local path that
    this restore is keeping. Both are read by the same backend and lost the same way, so
    the check cannot differ -- only the sentence naming which copy is at fault.

    The one entry the backend drops that this does NOT refuse is an open-slot key holding
    a path separator. The backend rejects that deliberately and warns, as a screen against
    a key smuggled in to escape the sessions directory, so its removal is the designed
    outcome rather than a loss -- and refusing the boot on it would let one planted byte in
    the bucket deny every replacement task permanently.

    Raises :class:`RestoreFailed` naming the file and what was wrong with it.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RestoreFailed(
            f"{name} {origin} is not UTF-8 text ({exc}). The backend would read it "
            "as no conversations and then replace it, so the task refuses to start "
            "rather than boot into an empty slot table."
        ) from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise RestoreFailed(
            f"{name} {origin} is not valid JSON ({exc}). The backend would read it "
            "as no conversations and then replace it, so the task refuses to start "
            "rather than boot into an empty slot table."
        ) from exc
    if not isinstance(parsed, dict):
        raise RestoreFailed(
            f"{name} {origin} is a JSON {type(parsed).__name__}, not an object. "
            "The backend requires an object at the top level and ignores anything else, "
            "so this would boot the task with an empty slot table."
        )
    if name == "open_slots.json":
        _validate_open_slots(name, parsed, origin=origin)
    else:
        _validate_session_map(name, parsed, origin=origin)


def _validate_session_map(name: str, parsed: dict, *, origin: str) -> None:
    """Refuse a session-map entry the backend's loader would skip or its prune would drop.

    Its loader keeps a value that is a string (the legacy form it migrates) and a value
    that is an object carrying ``sid``. Every other value meets a bare ``continue``
    commented "skip corrupt entries" -- no log, no count -- and the map it then writes back
    omits that conversation.

    ``sid`` must itself be a STRING, because the loader's check is that the key is present
    and the startup prune's is that the value is truthy. A ``sid`` of ``[]`` passes the
    loader and reads as empty to the prune, which collects the entry as stale and removes
    it; a ``sid`` of ``5`` reads as truthy and sends the prune looking for ``5.json``,
    which is absent, so it is collected too. Either way the mapping is deleted and the next
    flush publishes the deletion. An EMPTY string is not a fault: the backend writes one
    itself when it stashes a session id that stopped resolving, keeping the entry.
    """
    for key, value in parsed.items():
        if isinstance(value, str):
            continue
        if isinstance(value, dict) and isinstance(value.get("sid"), str):
            continue
        raise RestoreFailed(
            f"{name} {origin} maps {key!r} to a "
            f"{type(value).__name__ if not isinstance(value, dict) else 'object whose sid is not a string'}"
            ", which the backend skips as corrupt without logging it. Loading this file "
            "would drop that conversation's pointer and the next flush would delete it "
            "from the bucket, so the task refuses to start while the bytes are still "
            "there to repair."
        )


def _validate_open_slots(name: str, parsed: dict, *, origin: str) -> None:
    """Refuse an open-slots member the backend's loader would silently drop.

    Its reader yields no slots at all when ``keys`` is not a list, which is the
    empty-list-that-looks-restored case, and an ABSENT ``keys`` is legal and means no open
    tabs. Each member is then folded by a screen that rejects a non-string and an empty
    string by returning nothing at all.

    A member holding a path separator is the one thing that screen rejects and this does
    not: it warns, and the rejection is the security outcome it exists for.
    """
    if "keys" not in parsed:
        return
    listed = parsed["keys"]
    if not isinstance(listed, list):
        raise RestoreFailed(
            f"{name} {origin} has a 'keys' field that is a "
            f"{type(listed).__name__}, not a list. The backend reads that as no "
            "open slots, so the task would come up with an empty conversation list."
        )
    for member in listed:
        if isinstance(member, str) and member:
            continue
        shown = "an empty string" if isinstance(member, str) else f"a {type(member).__name__}"
        raise RestoreFailed(
            f"{name} {origin} lists {shown} among its open slots, which the backend's "
            "own screen folds to nothing and drops without a warning. The tab would not be "
            "reopened and the next flush would remove it from the bucket, so the task "
            "refuses to start while the bytes are still there to repair."
        )


def _write(settings: Settings, name: str, raw: bytes) -> bool:
    """Put *raw* at the authority file's local path. ``False`` if one was already there.

    The parent is checked for a symlink first, for the reason the front checks the
    sessions directory: ``mkdir(exist_ok=True)`` succeeds on a link to a directory and
    every write then lands wherever the link points, which for these two files means
    the task's whole conversation index written outside the data home.
    """
    parent: Path = settings.config_dir
    if parent.is_symlink():
        raise RestoreFailed(
            f"the config directory is a symlink: {parent}. Writing {name} through it "
            "would put this task's conversation index outside the data home."
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RestoreFailed(
            f"the config directory could not be created at {parent} ({exc}), so {name} "
            "cannot be restored."
        ) from exc
    try:
        return statefile.link_new(parent / name, raw, prefix=f".smc-restore-{name}-")
    except OSError as exc:
        raise RestoreFailed(f"{name} could not be written to {parent} ({exc}).") from exc


#: Reading an authority file that is already on disk. The symlink at the final component
#: is REFUSED rather than followed, because these two names sit in a directory the agent
#: itself writes in. ``O_NONBLOCK`` is load-bearing and not a tidiness flag: a FIFO
#: planted under one of these names would otherwise park the boot forever on the open,
#: which is a denial where a refusal was available.
_LOCAL_READ_FLAGS: int = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _keep_existing_local(settings: Settings, name: str) -> bool:
    """Read and validate the authority file already at *name*'s local path.

    ``True`` when one was there and is fit for the backend to load, ``False`` when the
    path holds nothing. Raises :class:`RestoreFailed` for a file that is there and is not
    fit, which is the whole reason this exists. The bucket's copy is validated before it
    is written, and a local file is kept INSTEAD of writing that copy -- so without this
    reading, the one branch that skips the write is also the one branch that skips every
    check. A malformed local file then boots the backend to an empty slot table and its
    first flush spends the valid bucket copy: the loss this module exists to prevent,
    arriving through the branch meant to protect against it.

    Refusing rather than overwriting, for the same reason the file is kept at all. The
    local copy leads the bucket by up to one backup interval, so replacing it with the
    bucket's would roll a conversation list backwards -- trading a loud fault for a quiet
    one. An operator repairs or removes the file, and BOTH copies are still there when
    they do.

    The shape is read off the descriptor rather than the path, so what is checked and what
    is read are one inode.
    """
    path = settings.config_dir / name
    try:
        fd = os.open(path, _LOCAL_READ_FLAGS)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RestoreFailed(
            f"{name} is at {path} and could not be opened ({exc}). A file there is what "
            "the backend loads, so the task refuses to start rather than leave it to be "
            "read as no conversations and flushed over the bucket's copy."
        ) from exc
    try:
        shape = os.fstat(fd)
        if not stat.S_ISREG(shape.st_mode):
            raise RestoreFailed(
                f"{name} at {path} is not a regular file. The backend opens that NAME, so "
                "what it loads from there is not this task's conversation index; the task "
                "refuses to start rather than flush an empty one over the bucket's copy."
            )
        if shape.st_size > MAX_OBJECT_BYTES:
            raise RestoreFailed(
                f"{name} at {path} is {shape.st_size} B, above the {MAX_OBJECT_BYTES} B "
                "ceiling this side reads. Checking it means reading it whole, so the task "
                "refuses to start rather than keep an index it cannot check."
            )
        with os.fdopen(os.dup(fd), "rb") as fh:
            raw = fh.read()
    finally:
        os.close(fd)
    validate_authority(name, raw, origin=f"already on disk at {path}")
    return True


def _authority_source(settings: Settings, store: ObjectStore) -> tuple[str | None, frozenset[str]]:
    """Which generation to read, as ``(slot, names the commitment covers)``.

    ``(None, every authority name)`` is GENERATION 0: no pointer has been published, so
    the legacy ``data/`` keys are what this bucket has. They are never rewritten by the
    generation protocol, which is what lets a bucket be read without being migrated.

    A pointer that exists and cannot be used raises instead, because absent and unusable
    decide opposite things: absent says nothing is committed and the legacy keys are the
    truth, while unusable says a generation may be committed and this task cannot tell
    which, so reading the legacy keys would boot from the objects the pointer was steering
    away from.
    """
    try:
        pointer = generation.read_pointer(settings, store)
    except StoreUnusable as exc:
        raise RestoreFailed(
            f"the bucket itself could not be read ({exc}), so which generation is "
            "committed cannot be established. This is not the same as the pointer being "
            "absent, so the task refuses to start rather than boot into an empty slot "
            "table and flush it over the real one."
        ) from exc
    except generation.PointerUnusable as exc:
        raise RestoreFailed(
            f"{exc} The task refuses to start rather than boot from a generation it cannot "
            "confirm is the committed one."
        ) from exc
    if pointer is None:
        adopted = _uncommitted_generation(settings, store)
        if adopted is not None:
            log.warning(
                "restore: no generation pointer is committed, but generation %s holds a "
                "complete authority pair -- a cycle published it and only its pointer did "
                "not land. Booting from it, because the pointer is written last so a "
                "generation is whole before anything references it, and it is newer than "
                "the legacy keys this writer never writes",
                adopted,
            )
            return adopted, frozenset(keys.AUTHORITY_NAMES)
        log.info("restore: no generation pointer, so the legacy keys are generation 0")
        return None, frozenset(keys.AUTHORITY_NAMES)
    log.info("restore: generation slot %s is committed", pointer.slot)
    return pointer.slot, pointer.authority


def _slots_holding(
    settings: Settings, store: ObjectStore, names: list[str] | tuple[str, ...]
) -> dict[str, set[str]]:
    """Which of *names* each generation slot holds.

    Absence is the only answer that means "not there". A read that fails for any other
    reason is refused upward, because a slot object that cannot be read is not a slot object
    that is not there, and reading it as absence is the step every check here exists to stop.
    """
    held: dict[str, set[str]] = {}
    for slot in keys.AUTHORITY_SLOTS:
        present: set[str] = set()
        for name in names:
            key = keys.authority_slot_key(settings, slot, name)
            try:
                store.get(key, limit=MAX_OBJECT_BYTES)
            except ObjectAbsent:
                continue
            except StoreUnusable:
                # Not translated: the bucket itself cannot be read, so every later read
                # meets the same answer and the process must end on that.
                raise
            except Exception as exc:  # noqa: BLE001 - translated, never swallowed
                raise RestoreFailed(
                    f"{key} could not be read from the bucket ({exc}), so which generation "
                    "this bucket holds cannot be established. This is not the same as it "
                    "being absent, so the task refuses to start rather than boot into an "
                    "empty index and flush it over a real conversation list."
                ) from exc
            present.add(name)
        held[slot] = present
    return held


def _pair_is_coherent(settings: Settings, store: ObjectStore, slot: str) -> bool:
    """Whether *slot*'s two authority files agree with each other.

    Presence of both names proves the pair is COMPLETE, not that both halves came from one
    cycle. With no pointer there is nothing in the bucket that proves common origin: two
    consecutive pointerless cycles can land complementary halves into the same slot, because
    a failed pair PUT leaves the half that succeeded behind and the next cycle -- still
    pointerless, so still targeting the same slot -- can fail on the other half.

    So what is checked is the property the READER depends on, which is what the harm is made
    of: every open tab must be a conversation the session map knows. A single cycle's pair
    satisfies that by construction; a pair assembled from two cycles can violate it, and the
    violation IS the mismatched tabs and session bindings. Where it holds, the pair is
    coherent for the backend whatever its origin, which is the question that matters.

    This is a weaker statement than same-cycle origin, and deliberately: origin cannot be
    established from the bucket without the object that failed to be written.
    """
    tabs: set[str] = set()
    known: set[str] = set()
    for name in keys.AUTHORITY_NAMES:
        key = keys.authority_slot_key(settings, slot, name)
        try:
            raw = store.get(key, limit=MAX_OBJECT_BYTES)
            parsed = json.loads(raw.decode("utf-8"))
        except StoreUnusable:
            raise
        except Exception as exc:  # noqa: BLE001 - translated, never swallowed
            raise RestoreFailed(
                f"{key} could not be read or parsed ({exc}), so whether generation {slot} "
                "holds a coherent pair cannot be established. The task refuses to start "
                "rather than boot from an index it could not check."
            ) from exc
        if not isinstance(parsed, dict):
            return False
        if name == "open_slots.json":
            listed = parsed.get("keys")
            if isinstance(listed, list):
                tabs.update(member for member in listed if isinstance(member, str))
        else:
            known.update(str(entry) for entry in parsed)
    stranded = sorted(tabs - known)
    if stranded:
        log.error(
            "backup-free restore: generation %s names open tabs its session map does not "
            "know (%s), so its two files did not come from one publication",
            slot,
            ", ".join(stranded),
        )
        return False
    return True


def _uncommitted_generation(settings: Settings, store: ObjectStore) -> str | None:
    """The slot holding a COMPLETE pair when no pointer names one, or ``None``.

    This is the state a first-ever generation leaves when its pair uploaded and its pointer
    PUT did not: the pair is WHOLE, because the pointer is written last precisely so a
    generation is complete before anything references it. So a complete slot pair is a
    publication -- newer than the legacy keys, which no cycle of this writer ever writes --
    and booting from it is what the pointer would have said to do.

    Refusing it instead would be unrecoverable rather than safe. The restore runs in the
    supervisor BEFORE the backend and the sidecar start, so a refusal here means no cycle
    ever runs: there is no writer left to publish the pointer the refusal is waiting for,
    every replacement takes the identical path, and the bucket is repaired only by hand.

    Two complete pairs with no pointer is the one case that cannot be resolved: nothing in
    the bucket says which is later, and the protocol never produces it -- a second slot is
    written only once a pointer names the first. That refuses, and says so as manual repair
    rather than promising a cycle that would not run.
    """
    held = _slots_holding(settings, store, keys.AUTHORITY_NAMES)
    whole = set(keys.AUTHORITY_NAMES)
    complete = sorted(slot for slot, present in held.items() if present == whole)
    if len(complete) > 1:
        raise RestoreFailed(
            f"no generation pointer is committed, but generations {', '.join(complete)} "
            "each hold a complete authority pair, and nothing in the bucket says which of "
            "them is the later publication. The protocol does not produce this state -- a "
            "second slot is written only once a pointer names the first -- so the task "
            "refuses to start rather than pick one and let the backend flush its view over "
            "the other. Repair is manual: delete the older pair, or publish a pointer "
            "naming the one to keep."
        )
    if complete:
        if not _pair_is_coherent(settings, store, complete[0]):
            raise RestoreFailed(
                f"no generation pointer is committed and generation {complete[0]} holds both "
                "authority files, but they do not agree with each other: it names open tabs "
                "its own session map does not know. Presence of both names proves the pair is "
                "complete, not that one cycle wrote it -- a failed pair PUT leaves the half "
                "that landed behind, and the next cycle is still pointerless, so it targets "
                "the same slot and can fail on the other half. Booting from a pair assembled "
                "from two publications is the mismatched-binding state itself, so the task "
                "refuses to start. No cycle repairs this one, because no pointer ever names "
                "it: repair is manual -- delete that slot so the bucket reads as the first "
                "boot it otherwise is, or replace the mismatched half."
            )
        return complete[0]
    return None


def restore_authority(settings: Settings, store: ObjectStore) -> RestoreResult:
    """Fetch, validate and write both authority files. Raise if any of it fails.

    The pair is read from the COMMITTED generation and from nowhere else. A pointer object
    names the slot whose pair was published whole, a writer publishes into the other slot,
    and commitment is that one object -- so a publication interrupted halfway damages only
    a slot this function never looks at, and there is no state in which half a commitment
    is visible.

    Six cases, each decided here. No pointer: the generation slots are surveyed first, and a
    slot holding a COMPLETE pair is adopted -- the pointer is written last so a generation is
    whole before anything references it, which makes that pair a real publication and a newer
    one than the legacy keys. Refusing it instead would be unrecoverable: this runs before the
    sidecar starts, so no cycle would be left to publish the pointer a refusal waits for. With
    no complete slot pair the legacy keys are generation 0, and an authority file absent there
    is absent rather than torn -- unless a slot holds that very name, which is a pair torn
    before it was referenced and refuses, naming manual repair. A pointer whose generation
    holds every name it
    was committed with: the ordinary restore. A pointer whose generation is missing one of
    those names: refused, because that pair lost a member, and the next complete cycle
    republishes into the other slot and commits it, which repairs the bucket. A pointer
    present but unusable: refused, since unusable is not absent. A bucket with only legacy
    keys: generation 0 again. A bucket with legacy keys AND a committed generation: the
    pointer wins and the legacy keys are ignored, because they are the older publication.

    A read that fails for any OTHER reason is failure too, including a denial -- reading a
    denial as absence is the same route to booting with an empty slot table.
    """
    result = RestoreResult()
    fetched: dict[str, bytes] = {}
    slot, committed_names = _authority_source(settings, store)
    for name in keys.AUTHORITY_NAMES:
        key = (
            keys.authority_key(settings, name)
            if slot is None
            else keys.authority_slot_key(settings, slot, name)
        )
        try:
            raw = store.get(key, limit=MAX_OBJECT_BYTES)
        except ObjectAbsent:
            log.info("restore: %s is not in the bucket", name)
            result.absent.append(name)
            continue
        except Exception as exc:  # noqa: BLE001 - translated, never swallowed
            raise RestoreFailed(
                f"{name} could not be read from the bucket ({exc}). This is not the same "
                "as it being absent, so the task refuses to start rather than boot into "
                "an empty slot table and flush it over the real one."
            ) from exc
        validate_authority(name, raw)
        fetched[name] = raw
    if slot is None:
        if result.absent:
            # No slot held a complete pair, or this would be reading one. So anything the
            # slots DO hold is a torn publication -- and it only matters for a name the
            # legacy keys could not supply either, because then there is nothing to boot
            # from and booting anyway would flush an empty view over a real conversation.
            held = _slots_holding(settings, store, result.absent)
            torn = sorted({name for present in held.values() for name in present})
            if torn:
                raise RestoreFailed(
                    f"no generation is committed and {', '.join(result.absent)} is absent "
                    f"from the legacy keys, but the generation slots hold {', '.join(torn)} "
                    "-- a pair that lost a member before it was ever referenced. Booting "
                    "from what is there would let the backend flush its own empty view of "
                    "the rest over a real conversation list. No cycle can repair this one, "
                    "because a torn pair is never named by a pointer: repair is manual -- "
                    "restore the missing member into that slot, or delete the slot so the "
                    "bucket reads as the first boot it otherwise is."
                )
    else:
        missing = sorted(name for name in committed_names if name in result.absent)
        if missing:
            raise RestoreFailed(
                f"generation {slot} is committed with "
                f"{', '.join(sorted(committed_names))} but does not hold "
                f"{', '.join(missing)}. A committed generation that lost a member is not a "
                "first boot: starting from the rest would let the backend flush its own "
                "empty view of the missing one over a real conversation list. The task "
                "refuses to start, and the next complete cycle commits the other slot."
            )
    for name, raw in fetched.items():
        if _write(settings, name, raw):
            log.info("restore: %s restored, %d B", name, len(raw))
            result.restored.append(name)
        else:
            if not _keep_existing_local(settings, name):
                raise RestoreFailed(
                    f"{name} was already at its local path when the bucket's copy was "
                    "offered and was gone a moment later, so neither copy is on disk. The "
                    "backend would come up with no index and flush that over the bucket's, "
                    "so the task refuses to start. The bucket's copy is untouched and the "
                    "next boot restores it."
                )
            log.info(
                "restore: %s is already on disk and reads cleanly; keeping the local copy, "
                "which leads the bucket by up to one backup interval",
                name,
            )
            result.kept_local.append(name)
    # The bucket held nothing under these names, so the loop above never looked at their
    # local paths -- and a file there is loaded by the backend exactly as a restored one
    # is. Being absent from the bucket decides which copy is authoritative; it does not
    # decide whether what boots the task was ever read.
    #
    # ``absent`` is left alone: it is a fact about the BUCKET, and it stays true whatever
    # is on disk. Reclassifying it here would make one name's two independent facts
    # compete for one list.
    for name in result.absent:
        if _keep_existing_local(settings, name):
            log.info(
                "restore: %s is not in the bucket but is already on disk and reads "
                "cleanly; the local copy is what the backend loads",
                name,
            )
    log.info("restore: complete -- %s", result.summary())
    return result
