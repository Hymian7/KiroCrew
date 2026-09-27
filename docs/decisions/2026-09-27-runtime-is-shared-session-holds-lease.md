# A kiro-cli runtime is a pool-owned shared resource; a session holds a lease on it and never a pid

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-09-27

## Decision

One `kiro-cli` ACP runtime — one operating-system process — is owned by the pool
that spawned it, not by any session running on it. A session holds a **lease**: a
handle it acquires, may share with other sessions, and releases. A session never
holds, stores, publishes or derives the runtime's pid.

"One session, one process" is the **default layout** that falls out of a pool
whose cap is 1. It is not an invariant, and no code may assume it. A runtime with
several leases is the same object as a runtime with one.

Three consequences follow, and each is a rule:

1. **Deriving a pid from a session key is a defect.** Code that maps a session to
   a pid and then signals it, reads its RSS, tests whether it is alive, or names a
   file, directory, unit, log or browser profile after it is attributing a shared
   process to one of its tenants. Under a cap above 1 it signals, measures or
   names on behalf of every other session on that process. This holds whatever
   the cap is set to today: the reading is wrong at cap 1 too, merely
   indistinguishable from right.
2. **One chokepoint kills a runtime.** Exactly one function in the tree may end a
   runtime process. It asks the lease table first and refuses while any lease is
   outstanding, records the caller and the reason, and only then signals. The
   low-level kill primitives in `platform_compat` stay, as primitives, callable
   from that chokepoint alone. The number of places elsewhere that call them
   directly only ever decreases.
3. **The token is the identity; the pid is not.** The pid-keyed session sidecar
   retires. A pid identifies a process, and a process may carry many sessions, so
   a pid cannot answer "which session is this". The session token can, and it
   already does everywhere it is available.

## Why

- A pool that shares runtimes makes the blast radius of one process dying equal
  to the number of leases on it. Every place that reads a pid per session is a
  place that reports, bills, measures or kills on behalf of tenants it cannot
  see. The shape of the fix is ownership, not more careful pid handling: if a
  session cannot obtain a pid, none of these defects can be written.
- Dogfooding the shared-runtime path with `chat_runtime_sharing` enabled produced
  eight runtime deaths across an eight-hour window. Two were wide: one took
  fifteen sessions down with it, another at least ten. The killing signal was
  SIGTERM from Kiro Crew's own teardown paths — not a crash, not the backend, not
  the host. Sessions were tearing down processes they did not own, and the code
  doing it could not tell that anyone else was aboard. This is the evidence that
  the chokepoint in consequence 2 is load-bearing rather than tidy.
- Subagents have multiplexed sessions onto one parent process for a long time, so
  a runtime serving several sessions is not a new object in this codebase. What
  is new is top-level chat slots joining that demux, which is what makes the
  per-session pid readings reachable from the dashboard's main path.
- The alternative — keep pids per session and audit each reader for
  sharing-safety — was rejected. The readers are spread across liveness, metrics,
  cleanup, sweeps, recovery, identity resolution and resource naming, and each one
  is individually plausible. An audit leaves the next one free to be written.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/13459 — the change that introduces
  the runtime pool and its lease table (`acp/chat_runtime_pool.py`), behind a
  default-off flag, and whose Design Review asked for this record.
- https://github.com/kirodotdev/KiroCrew/pull/14346 — the pull request that adds
  this entry. Its description carries the request for the restatement this entry
  rests on; the maintainer's own comment is cited below, and this entry does not
  merge without it.
- PENDING: the `#issuecomment-` permalink of @iamwhatever's restatement on that
  pull request. Rule 4 makes this the entry's only evidence of authorship, so
  merging while this line still reads PENDING would record a decision nobody is
  on record as having made.
