# Senior-style code review: mxl-switcher-core

**Repository:** `guycochran/mxl-switcher-core` (headless, frame-accurate v210 MXL switcher core, C++20)
**Reviewer:** Muse (AI assistant), at the author's request — written in the voice of a skeptical senior engineer deciding whether to bet a production on this code.
**Date:** 2026-10-16/17
**Scope:** Three files, read through the repo connection: `src/core.cpp`, `src/mxl_backend.cpp`, `include/mxl_core/read_window.hpp` (with its implementation in `src/read_window.cpp`). The header files and the live harness were not read; findings below note where that matters.

---

## Overall verdict

Three files in: the policy layer is clean, the backend is battle-hardened, and the selection algorithm is genuinely well-reasoned. The critiques that remain are second-order — clarity, comments, one threshold justification. This reads like code written by someone who understands the broadcast problem deeply: the hard parts (failure taxonomy, hold-last semantics, deterministic delay across cuts) are where the care went, which is the right priority order.

---

## 1. `src/core.cpp` — the switching engine

This is where the "frame-accurate" claim lives or dies.

**Verdict: genuinely well-structured.** The core design decision — commands enqueue with a receipt and only take effect inside `process()`, at the grain boundary — is the correct pattern, and it's implemented cleanly. Output flows are created exactly once in the constructor, so the stable PGM/PVW identity claim is structural, not aspirational. The guard rails are the things most people forget: you can't remove a source that's on program/preview or has pending commands, the output index must be monotonic or it throws, and the source snapshot is copied under the lock so the media path never holds a dangling reference. The `rebind_source` path (flow UUID rotation without changing source IDs) shows the lesson from the other repo's reviewer issue #2 got internalized here. A senior deciding whether to bet production on this would keep reading past this file, which is the real compliment.

**Sharp questions:**

1. **Is `process()` on a real-time thread, and does the control path allocate under the same mutex?** `register_source` does `sources_.push_back` while holding the lock that `process()` also takes. Vector growth allocates. If the media tick ever blocks on a control-thread allocation, that's jitter on the 30fps cadence. At 8 sources it's unlikely to bite, but a senior would want `reserve()` up front or the source table behind copy-on-write.
2. **Who catches the throw?** `process()` throws on a non-monotonic output index. Fail-fast is right, but if that exception unwinds through the media loop — say after a domain restart reorders a grain — a recoverable hiccup becomes a dead switcher. The catch site in the live harness needs to be visible before sign-off.
3. **The pending queue is unbounded.** A misbehaving control client can `enqueue` forever. Loopback-only makes it theoretical, but a bound (or coalescing) is cheap insurance.

**Not verified in this file:** the header (mutex type — hoping it's not recursive). The reader-replacement detection and hold-last policy live in `src/mxl_backend.cpp` (see §2).

---

## 2. `src/mxl_backend.cpp` — reader management and the hold-last policy

This is the file where the "zero drops" claims are actually earned or broken — and it's the most battle-hardened of the three.

**Verdict: this is where the field experience shows.** The error taxonomy is the standout: `MXL_ERR_FLOW_INVALID` → discard and reopen the reader; too-early/too-late/timeout → hold last good frame and count it; anything else → throw. The switcher never dies because a source misbehaves — exactly the failure mode that wedged the CBC selector in the other repo. Three details worth praising in a review:

- **The copy-overwrite guard.** After `memcpy` from the source ring, the code re-reads the runtime info and checks whether the writer lapped the slot *during the copy* — publishing hold instead of a mislabeled grain. Most people never think of that race. Senior-level paranoia, in the good sense.
- **The cadence-hold source-identity guard.** Re-emitting the last picture on a repeated input index is correct, but the comment calling out *why* the guard matters across CUTs (stale cache from the prior source must never go on air as a valid hold) shows someone got burned by this or thought hard enough to preempt it.
- **Reader replacement via inode identity.** Detecting flow recreation through the domain file's dev+ino is a pragmatic answer to the exact UUID-rotation problem from the other repo's review.

**Sharp questions:**

1. **The sync group is created and reset but never populated in this file.** `grouped_flows_` is only ever cleared and checked — nothing inserts into it, and there is no call adding flows to `sync_group_`. Dead code, or load-bearing elsewhere? If vestigial, a senior will flag it; if it matters elsewhere, it needs a comment saying so.
2. **Thread affinity of `Impl` is implicit.** `readers_`, `writers_`, and `timelines_` have no mutex — correct *only* if every method runs on the single media thread. `stats()` is properly locked, which suggests the author knows where the boundary is, but nothing documents or asserts it. One future contributor calling `route_grain` from a callback thread gets silent corruption.
3. **The `.mxl-flow` path coupling.** `flow_identity()` depends on the SDK's domain directory layout (`domain/flow_id.mxl-flow`). If a future SDK changes that layout, `stat()` returns ENOENT, identity becomes nullopt, and replacement detection silently stops working — the exact failure this code exists to prevent. Worth a comment, or better, a startup self-test.
4. **The selection algorithm itself** — `select_common_read_index`, `timelines_.select` with the forward-only recovery floors — lives in `include/mxl_core/read_window.hpp` (see §3). Everything above is scaffolding around *which grain gets picked*.

**Minor nits:** no abort path if `mxlFlowWriterCommitGrain` throws after a successful open (leaked open grain); the `+2U` fencepost in the `oldest` computation deserves a one-line comment.

---

## 3. `include/mxl_core/read_window.hpp` + `src/read_window.cpp` — the read-index selection algorithm

The algorithmic heart, and the strongest file of the three.

**Verdict: the output-clock-anchored cursor is a real insight, not ceremony.** Most switcher code chases the source head and inherits whatever phase the sampling happened to catch — which is how presentation delay drifts across CUTs. Deriving the read index from the *output* index (`N - (margin+1)`, with the +1 accounting for a same-tick source commit) makes delay deterministic by construction. That's the design decision the "constant offset, zero drift" A/V numbers rest on, and it's the right one. The epoch-reset handling is the other standout: a recreated writer briefly exposes a reset ring, and comparing that new epoch against the old cursor would manufacture a giant synthetic skip — resetting the cursor instead shows someone got burned by this in the field or thought hard enough to preempt it. The fallback hysteresis is also carefully done: rejoining the deterministic path requires the target to be strictly *inside* the readable window, not at its edge, so it can't flap modes every frame.

**Sharp questions:**

1. **The common-window computation in `prepare_frame` looks like dead logic.** It computes `select_common_read_index` across the PGM+PVW sources, but the selected index is never consumed — only the window width and skew feed stats. The actual read index comes from the independent per-output timeline in `route_grain`. The hpp comment ("unlike the common-window selector, this is the normal CUT path") suggests the common selector is a superseded design kept as a health probe. If that's intentional, it should say so in one line — right now it reads as either a diagnostic or a leftover, and a reviewer can't tell which. It's cheap (the runtime queries are already done), so it's not a perf issue, just a clarity one.
2. **The implausible-jump allowance is generous.** Accepting a forward jump of up to `elapsed + grain_count` means a source can leap by a full ring buffer and still be treated as plausible. Probably fine for burst catch-up, but the threshold is doing real work in distinguishing "recreated writer" from "jittery source" — it deserves a comment explaining why a full ring of slack is the right number.
3. **The disable sentinel.** `safety_margin_frames == uint64_max` → return nullopt. It works, but a magic max-value sentinel for "disabled" will confuse the next reader; a named constant or an optional would be kinder.

**Signed off on:** the monotonicity preservation (never regress the selected index even if the output clock does — defense in depth behind core.cpp's own throw), the overflow guard on the allowance computation, and the retiring-edge attribution that distinguishes "grain aged out at the ring edge" from other too-late causes. That last one is diagnostic precision most codebases never bother with.

---

## Recommended follow-ups (ordered by reviewer impact)

1. **Document or assert the media-thread affinity** of the backend `Impl` (§2 Q2) — the highest-risk item; silent corruption is the failure a senior fears most.
2. **Resolve the sync-group question** (§2 Q1) — remove it or comment its purpose; dead-looking code erodes reviewer trust disproportionately.
3. **Clarify the common-window selector's role** (§3 Q1) — one line distinguishing health probe from leftover.
4. **Add the startup self-test or comment for `flow_identity()`'s path coupling** (§2 Q3).
5. **Justify the implausible-jump allowance** (§3 Q2) and **name the disable sentinel** (§3 Q3) — one comment each.
6. **Confirm the catch site** for `process()`'s monotonicity throw in the live harness (§1 Q2); consider `reserve()` on the source table (§1 Q1) and a bound on the pending queue (§1 Q3).
7. **Minor:** abort path for a throwing `mxlFlowWriterCommitGrain`; comment the `+2U` fencepost.

None of these change the overall verdict. Items 1–3 are what a senior would ask about first in a follow-up review.
