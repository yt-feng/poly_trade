# BTC5m v2.75 bounded 12-hour canary contract

Status: **candidate source only; disabled until AWS compile, dynamic suites,
attack matrix, exact future authorization, and root receipt all pass.**  v2.74
source and its three units remain byte-for-byte frozen.

## Exact economic contract

- Strategy: `A_v271_threshold`.
- Horizon: exactly 12 hours, 144 contiguous BTC 5-minute windows, indexed
  `0..143`; no arbitrary start, skip, window 145, extension, or rolling renewal.
- Cash/no-signal windows write a complete chained receipt and continue to the
  strict next index.
- The first eligible signal is terminal for the horizon.  It may cause at most
  one `BUY FOK` POST, exactly 5 shares, first eligible entry ask + $0.01 rounded
  upward to the observed tick, limit no greater than $0.40, total reserve no
  greater than 2.50 USDC.
- There is no transport retry.  Filled, FOK-cancelled, venue error, ambiguous
  submission, wrapper crash, or consuming-delegation ambiguity all terminate
  the horizon.  A consuming delegation without a proven response is reported
  conservatively as one possible submission, never as zero.
- If all 144 complete rows are cash, the terminal is
  `horizon_completed_144_cash_no_trade` with zero attempts.

## Acyclic authorization graph

The bounded parent is the only explicit user authorization.  A window child is
never labelled as a direct one-window user authorization.

1. `schedule.json` seals the exact 144 contiguous windows.
2. The parent authorization binds `schedule_sha256`, fixed economics, the AWS
   qualification snapshot, account identity, the v275/v274/v2733/v273/v271
   release identities, all four v275 unit-file SHAs, the exact four-key
   non-secret environment SHA, and the exact start/end.
3. The child manifest binds the raw parent authorization SHA, schedule SHA, and
   a chain of 144 `child_commitment_sha256` values.  Each commitment is the hash
   of an acyclic child projection core.
4. A root pre-start horizon receipt binds the raw parent, manifest, schedule,
   release contract, raw public-identity artifact SHA, accepted shared-lock
   inode, and normalized manager-loaded systemd boundary.
5. The final materialized child binds parent + manifest + root receipt +
   schedule + index + exact start/end + its committed projection.  Its raw file
   SHA is the execution `authorization_sha256` used by the frozen verifier.

This two-hash child design is intentional: putting the raw child SHA in a
manifest that is itself inside the child would create an unsatisfiable hash
cycle.  The manifest seals the acyclic commitment; the final child and every
window receipt seal the manifest and root receipt.

## Process and credential split

| Process | Network | `live.env` | Responsibility |
|---|---:|---:|---|
| one-time identity provisioner | no (`PrivateNetwork=true`) | inaccessible | project the public account tuple from one exact sealed v274 authorization |
| v275 coordinator | no (`PrivateNetwork=true`) | inaccessible | strict next index, request, consuming delegation, terminal |
| v275 prepare child | yes, short-lived | inaccessible | exact live v271 row, full cash/eligible decision, candidate/permit |
| v275 execute child | yes, short-lived | read-only | full frozen verifier, global attempt, frozen execution |

The coordinator source has no top-level v274 or broker import.  The prepare
process lazily loads exact v274 bytes only after checking SHA
`32732ea77e77116680d7a1ce2a65b00c2cc1f24512a2e6f387c9e5a543c634a1`;
its systemd sandbox makes `live.env` inaccessible.  Only the execute unit loads
the existing root-owned `live.env`.  Before consuming `execute_invocation.json`,
execute verifies that path is the canonical, regular, non-symlink, single-link
`root:root 0600` inode.  v275 reads no credential-file bytes, computes no
credential-file hash, and never includes credential material in output or logs.

Before any v275 parent authorization exists, a manually started, static,
non-restarting, credentialless provisioner reads only the canonical root-owned
`/etc/poly-bot-btc5m/v274-conditional-preauthorization.json`.  It requires the
exact v274 split-identity schema and raw SHA
`87cd489b985a7a9225cd758ca14a55ca8c2d7dc29292b292b4230744161ec16c`,
projects only signer, funder and signature type plus provenance, and O_EXCL+
fsyncs
`/etc/poly-bot-btc5m/v275-public-identity/public-execution-identity.json` as
one canonical `root:root 0600`, single-link file.  Its parent is a separately
precreated, exact `root:root 0700`
`/etc/poly-bot-btc5m/v275-public-identity/` directory; the provisioner has
write access only to that directory, never the surrounding `/etc` tree.  There
is no private-key or
`live.env` fallback.  The parent, every child commitment/projection, candidate,
permit, and root receipt bind its raw artifact SHA.  Registration and every
production contract load re-read the exact canonical v274 authorization,
re-prove its `root:root 0600`, single-link metadata/schema/raw SHA, derive the
actual signer/funder/signature-type tuple from those bytes, and compare the
installed public identity plus its raw SHA.  All four units condition on and
mount that v274 provenance read-only.  Coordinator rechecks it before
delegation and execute rechecks it immediately before consuming
`execute_invocation.json`, immediately before the global attempt, and again
after the attempt but before frozen `execute_once`.

Every production process requires the source at the canonical immutable
`/usr/local/lib/poly-bot-btc5m-v275/runtime-v1/app` snapshot, recomputes the
authorized source/unit/environment hashes, and queries systemd's effective
loaded properties.  It refuses a stale fragment, any drop-in, changed
`ExecStart`, `EnvironmentFiles`, network/Restart/RestartSec/StartLimit/path
sandbox, trigger/timer, reverse dependency, or non-static unit state.  The
reverse manager properties `WantedBy`, `RequiredBy`, `UpheldBy`, `BoundBy`, and
`OnFailureOf` must all be empty.  AWS systemd may omit `EnvironmentFiles` from
`systemctl show` when the effective list is empty; only that one missing
property is normalized to `EnvironmentFiles=""`.  When the property is
present for a unit whose sealed list is empty, its raw value must be exactly
the empty string; garbage, relative text, or an option marker without an
absolute path refuses.  Any other omitted property, an `EnvironmentFiles`
omission combined with any other missing property, or an omitted
`EnvironmentFiles` for a unit whose sealed contract requires a file also
refuses.  Thus a matching unit file on disk is not by itself accepted evidence.

## Frozen verifier/executor bridge

For an eligible child, prepare persists a root-owned v274 verifier projection
receipt.  It then uses exact v274 candidate, permit, qualification-anchor and
live-row schemas and calls the complete frozen
`verify_candidate_and_permit(...)`.  Horizon parent/manifest/receipt/index
bindings are extra immutable fields; the child remains the authorization axis.

The execute-only process reads that same persisted projection receipt and the
same candidate/permit bytes.  At entry, after validating the exact delegation
and its child/receipt/permit hashes but before importing v274, it
O_EXCL+fsyncs `execute_invocation.json`; this consumes the sole execute-unit
invocation even if the process then fails before a POST attempt.  It calls the
full frozen verifier and only then O_EXCL+fsyncs `global_attempt.json`.  It
passes the same permit object, artifact SHA and path object to unchanged frozen
`execute_once(...)`.  That function retains the accepted shared-lock, quote,
signature, reserve, FOK, durable intent/transport/result, and no-retry behavior.
Only an exact response chain is classified as recorded: eligible receipt ->
consuming delegation -> one execute invocation -> one global attempt -> v274
intent -> transport-started -> full execution result, with every raw SHA and
causal timestamp bound.  A missing, truncated, transplanted, or backdated member
is conservatively terminal unknown.

The root receipt seals the already-accepted persistent shared lock at
`/var/lib/poly-bot-btc5m/canary/v264/execution.lock`: exact device/inode,
single link, `root:ubuntu`, mode `0660`, and protocol
`v264-persistent-block-v1`.  Registration, prepare, coordinator immediately
before delegation, and execute immediately before the global attempt and again
before unchanged v274 execution all reject replacement.  The file is never
chmod/chown'd or recreated by v275.

Before starting execute, the coordinator O_EXCL+fsyncs a globally consuming
`first_eligible_delegation.json`.  Its creation ends window iteration.  This
closes the verifier/delegation race: after that marker, absent a fully validated
venue response, recovery reports `one_fok_submission_unknown` and cannot
finalize a false zero.

## Restart contract

- The only initial claim band is `H0-120000ms .. H0-1ms`, inclusive.  Starting
  earlier or at/after H0 cannot create a claim; this keeps the 12h15m service
  runtime bound over the exact H0..H1 horizon.
- Every coordinator holds a non-blocking exclusive flock on the validated
  immutable claim inode for its entire state-transition lifetime.  Concurrent
  starts cannot interleave a zero-attempt terminal with a consuming
  delegation; a process crash releases the kernel lease.
- A horizon claim records the kernel boot ID.  Boot drift, wall-clock rollback,
  or an expired incomplete resume writes an auditable durable terminal.  Only
  the coordinator uses bounded same-boot recovery:
  `Restart=on-failure`, `RestartSec=10s`,
  `StartLimitIntervalSec=120s`, `StartLimitBurst=3`. Prepare and execute use
  `Restart=no`; recovery may resume only a complete cash boundary.
- Resume is allowed only after a complete, strictly chained cash receipt.  An
  eligible receipt that already exists when a recovered coordinator enters,
  without a consuming delegation, terminates at zero attempts; only an
  eligible receipt freshly verified after this same coordinator's prepare
  child returns may proceed to delegation.
- The application checks the sealed absolute H1 before each request, after
  every prepare return, after eligible observation, and immediately before
  delegation.  Restarting systemd cannot extend the authorization even though
  its per-process `RuntimeMaxSec` clock resets.
- A request/child/anchor/live row/candidate/permit without the final window
  receipt is an incomplete boundary and terminates; it is never replayed.
- Receipt scanning starts at index 0 and stops at the first missing index.  A
  later receipt after a gap, or artifacts after the first eligible index, are
  terminal corruption.
- The root receipt cannot predate the manifest.  Window 0's request cannot
  predate the claim; every later request cannot predate the prior complete cash
  receipt.  Delegation, execute invocation, global attempt, intent, transport,
  and response timestamps must then be monotonic.
- A consuming delegation, global attempt, intent, transport marker, response,
  or ambiguous process exit can never return to the window loop.
- Every all-window scan treats `completion`, intent, transport-started,
  submission-unknown, result, wrapper, and broken-link preseeds as execution
  stages.  If any such per-window stage exists without its global delegation or
  attempt, recovery is still conservative one/unknown; neither cash corruption
  nor an existing eligible receipt may wrap it in a zero-attempt terminal.  The
  coordinate CLI exception fallback repeats the same exact 144-window `lexists`
  scan before it is permitted to print a zero-attempt status.
- All claims, requests, projection receipts, candidate/permit artifacts,
  receipts, execute-invocation claims, attempts, transport summaries and
  terminals use canonical JSON, `O_EXCL`, `O_NOFOLLOW`, file fsync and
  parent-directory fsync.

## Deployment materialization order (AWS only)

Heavy tests and all dynamic execution belong on AWS.  Local work is limited to
editing, static review, and SHA calculation.

1. Copy a root-owned, non-writable runtime snapshot to
   `/usr/local/lib/poly-bot-btc5m-v275/runtime-v1`; independently confirm the
   frozen v274 and every transitive dependency SHA.
2. Preserve the accepted `/var/lib/poly-bot-btc5m` data root as exactly
   `root:ubuntu 01770`; this is the sole group-writable ancestry exception.
   Require every descendant v275 state directory to be root-owned and
   non-group/world-writable.  Inspect,
   but never replace/chmod/chown, the accepted `root:ubuntu 0660` shared-lock
   inode.
3. Precreate `/etc/poly-bot-btc5m/v275-public-identity/` as exact
   `root:root 0700`; do not let the provisioner create or replace it.  Install
   the four static service files and the exact root `0600`, four-line,
   four-key non-secret environment file.  Copy the source `deploy/` directory
   into the immutable runtime snapshot because its exact unit bytes are part of
   the release contract.  Run `daemon-reload`; require exact FragmentPath,
   empty DropInPaths, exact effective ExecStart/EnvironmentFiles/network/
   Restart/read-only/inaccessible properties, static state, empty WantedBy/
   RequiredBy/UpheldBy/BoundBy/OnFailureOf, and no trigger, wants symlink, or
   matching timer.  Treat an omitted empty EnvironmentFiles property as the
   AWS-compatible empty representation, but reject every other missing
   property.  Verify the pre-existing live credential path is one canonical
   `root:root 0600`, single-link regular file without reading or printing its
   contents.  Do not enable a timer.
4. With all units daemon-reloaded and the v275 parent still absent, manually
   start the static identity provisioner exactly once.  Require PrivateNetwork,
   `identity_provisioner_loads_live_env=false`, no credential variables, the
   exact sealed v274 provenance path/schema/SHA, write access only to the
   dedicated identity directory, and a new canonical v275 public identity
   file; do not enable or schedule the provisioner.
5. Choose an exact aligned future start.  Generate canonical artifacts in this
   order: `schedule`, parent `authorization`, `manifest`, root `receipt`.
   Registration may occur earlier, but the single coordinator start must be
   scheduled inside the sealed H0-120s..H0-1ms claim band.
6. Arrange one non-persistent server-side wakeup whose only action is
   `systemctl start` of the static coordinator inside that band; do not attach a
   timer/drop-in/wants edge to the v275 unit itself.  The one-shot wakeup is
   removed after firing.  The coordinator sleeps inside
   systemd/child collectors; no
   Codex polling or token-consuming watch loop is needed.

The CLI emits compact canonical JSON plus a final newline, suitable for a
root-owned temporary file followed by an inspected atomic installation:

```text
bounded_horizon_canary_v275.py provision-public-identity-from-v274
bounded_horizon_canary_v275.py schedule --horizon-start EPOCH
bounded_horizon_canary_v275.py authorization --schedule SCHEDULE \
  --authorized-at-epoch-millis MS --expected-signer 0x... \
  --expected-funder 0x... --signature-type N \
  --qualification-manifest-sha256 SHA
bounded_horizon_canary_v275.py manifest --authorization AUTH \
  --schedule SCHEDULE --created-at-epoch-millis MS
bounded_horizon_canary_v275.py register --authorization AUTH \
  --schedule SCHEDULE --manifest MANIFEST --output RECEIPT
```

## Required AWS attack/acceptance matrix

The candidate is not deployable until AWS passes, at minimum:

1. exact 144-window continuity; reject index 144/window 145/extension;
2. schedule/parent/manifest/root-receipt hash-cycle and mutation attacks;
3. child transplant across index, parent, manifest, receipt, start or account;
4. strict-next gap, arbitrary start, skipped cash, and later-receipt attacks;
5. eligible/buy bundle relabelled as cash;
6. concurrent coordinator, concurrent prepare, concurrent execute, duplicate
   claim, duplicate delegation and duplicate attempt;
7. crash after request/child/projection receipt/anchor/live row/candidate/permit
   and before window receipt;
8. race `terminal check -> finalizer -> global attempt -> POST`; terminal must be
   conservative unknown and there must never be a false zero;
9. execute exit 0/nonzero/timeout with no stage, intent only, damaged transport,
   damaged result, FOK cancellation, venue error and submission unknown;
10. v274 import-path substitution, exact-source mutation, dependency mutation,
    symlink/hardlink/wrong-owner/group-writable ancestry and contract files;
11. credential scan proving coordinator/prepare never receive live key names or
    read `live.env`, while execute is the sole short-lived credential process;
12. shared-lock inode replacement and bidirectional root/ubuntu lock exclusion;
13. same-boot complete-cash resume versus incomplete-child and boot-drift
    termination, plus wall-clock rollback and exact launch-band boundaries;
14. 144 cash receipts produce no candidate after cash, no delegation, no
    attempt, no signing, no POST and an exact no-trade terminal;
15. first eligible calls the full frozen verifier before attempt and passes the
    same permit object/path/SHA to frozen `execute_once` exactly once; concurrent
    or sequential duplicate execute starts lose the pre-verifier invocation
    O_EXCL and cannot reach the verifier/attempt/POST path.
16. daemon-reload then effective systemd inspection: exact FragmentPath,
    DropInPaths empty, normalized ExecStart/EnvironmentFiles/PrivateNetwork/
    Restart/RestartSec/StartLimit/path sandbox exact, no stale loaded fragment,
    no trigger, empty WantedBy/RequiredBy/UpheldBy/BoundBy/OnFailureOf, no wants
    symlink, and no matching timer; accept only AWS systemd's omission of an
    otherwise empty EnvironmentFiles property, inject each other drift or
    missing property, and require refusal.
17. malformed/unreadable delegation, damaged receipt after delegation, wrapper
    fsync failure, and process crash after global attempt each produce a durable
    enumerated one/unknown terminal and never a false zero or restart loop.
18. crash immediately after an eligible receipt but before delegation restarts
    only the coordinator, writes the enumerated zero-attempt eligible-recovery
    terminal, and never starts execute; H1 remains absolute across restarts.
19. backdate root receipt before manifest, request before claim/prior cash
    completion, delegation before eligible receipt, invocation before
    delegation, or attempt before invocation; every forged causal chain refuses.
20. inject broken symlinks at every global and cash-forbidden artifact path;
    `lexists`-based scans must refuse/return conservative unknown, never a false
    144-cash zero terminal.
21. prove `/var/lib/poly-bot-btc5m` exact `root:ubuntu 01770` ancestry succeeds,
    while missing sticky bit, non-root owner, wrong group, world-write, or any
    writable descendant refuses before window 0.
22. change `live.env` to mode 0644, wrong owner/group, hardlink, symlink, or
    replaced inode; execute must refuse before `execute_invocation.json`, and
    coordinator/prepare must remain unable to access it.  Scan process output
    and journal to prove no secret value or file bytes were emitted.
23. accept `one_fok_attempt` only for a full, exact v275 invocation/attempt plus
    v274 intent/transport/result SHA chain; remove, truncate, transplant, or
    backdate each member and require one/unknown with no retry.
24. provision public identity only from the exact root-owned v274 authorization;
    reject source SHA/schema/path spoofing, symlink, hardlink, wrong
    owner/group/mode, an existing output, output symlink/hardlink/mode/group
    attacks, identity-directory symlink/wrong owner/group/mode/writable
    ancestry, and post-provision replacement.  Before parent authorization,
    install a coherent alternate tuple while retaining self-declared provenance
    literals, then regenerate matching parent/manifest/root-receipt bytes;
    authorization/register/load must still reject because the tuple differs
    from the exact v274 raw bytes.  Replace the identity after prepare, before
    invocation, before global attempt, and after global attempt;
    every boundary must refuse before POST, with the post-attempt case recorded
    conservatively unknown.  Mutate the parent tuple with and without rehash,
    and transplant parent-to-child/candidate/permit identity bindings; all must
    refuse.
25. construct a deterministic complete stored v271 live row containing the
    Binance factor, frozen tail, signal quote, entry quote and sealed hashes;
    require real frozen v274 materialization plus the complete unmocked
    `verify_candidate_and_permit` to accept it.  Independently tamper `factor`
    and `binance_tail`, reseal the row, and require frozen v274 cash/refusal.

## Files

- `bounded_horizon_canary_v275.py`
- `tests/test_bounded_horizon_canary_v275.py`
- `deploy/poly-bot-bounded-horizon-canary-v275-provision-identity.service`
- `deploy/poly-bot-bounded-horizon-canary-v275-coordinator.service`
- `deploy/poly-bot-bounded-horizon-canary-v275-prepare.service`
- `deploy/poly-bot-bounded-horizon-canary-v275-execute.service`
- `deploy/v275-bounded-horizon.env.example`
