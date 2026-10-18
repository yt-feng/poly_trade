# Continuous research operating contract

The research objective is small-account net growth with repeatable cash reuse. A target balance is an objective to evaluate, never an assumed achievable return. Count retrospective quote replays, prospective paper episodes and authenticated actual fills separately. Ten observations or paper exits do not establish ten actual fills.

## The ten-fill compound Canary definition

The ten-fill criterion is one chronological sequence on one evolving account cash ledger. The first fill starts with the declared initial cash; each later entry is funded only by the cash still available after earlier fees, losses, locked positions and released settlement proceeds. Cash is released at the declared settlement/reuse time, and the ledger never resets between fills. A candidate therefore fails at the first consecutive entry that cannot be funded, even if ten separately reset simulations would each appear to work.

An authenticated bundle must make that identity check explicit. The v2 admission contract fixes the count at exactly ten, carries one `ledger_id` and `chain_id`, links each `previous_checkpoint_id` to the preceding balance checkpoint, requires sequence numbers `1..10`, reconciles numeric `balance_before`/`balance_after` amounts and release receipts, and binds every fill to a source capture/sequence/hash that arrived before entry. A bundle that concatenates two independently reset five-fill ledgers is rejected even when a fresh $10 replay of its rows would appear profitable.

For paper research, report the first-ten prefix, the full selection/holdout chain and independent time blocks separately. The first-ten prefix is a paper upper bound only: it can show that the frozen data and execution assumptions produce a continuous compound chain, but it cannot become a real Canary without ten authenticated fills, settlement receipts and independently reconciled cash checkpoints.

## Each research cycle

1. Read the current code, in-flight runs, sealed journal and source watermarks. Do not duplicate an active job. A new release can revise old data; compare content hashes and observation times before claiming fresh evidence.
2. Review primary sources: official venue rules, original papers, author repositories and pseudonymous public settlement events. Convert a mechanism into a falsifiable hypothesis, not an appeal to a new model or famous trader.
3. Register the hypothesis, sample cutoff, eligible population, comparisons, execution assumptions and stopping rule inside an encrypted journal before evaluation. Keep exploration separate from confirmation. A threshold changed after seeing results starts a new discovery version.
4. Prioritize high-quality contiguous training segments. Evaluate executable bid/ask, independent outcome books, current fee configuration, discrete minimum quantities, observed depth, delays, inventory and cash availability. Missing observations are unknown, not favorable fills or reusable cash.
5. Run one bounded cloud experiment per cycle by default, with at most two new hypotheses. Local work is inspection and lightweight synthetic tests. Increase a budget only for a concrete evidence need; repeated unchanged tests do not add sample size.
6. Report net account outcomes, concentration of profits, cash locked, turnover and independent market counts. Compare against the book and a no-position baseline. Keep all failed candidates and denominator counts.
7. Classify explanations as reproduced implementation defects, controlled replay counterfactuals, observational associations or unidentified causes. Source clocks, receipt clocks and snapshot clocks are distinct. A sampled quote cannot certify the exchange state at an unobserved arrival instant.
8. Seal the work summary, citations, input identities, experiment registry, results, failure explanation and next action before GitHub publication. This is an operational evidence log, not a transcript of hidden reasoning. Verify envelope identity and remote bytes; periodically test private recovery.
9. Advance only a frozen supported candidate into a prospective episode. Preserve model and protocol hashes, first-observation times, episode identity, deduplication and cash checkpoints. Replaying an episode replaces its state rather than adding its fills again.

## Persistence and scheduling

The thread heartbeat checks progress every two hours. It orchestrates bounded cloud jobs when new data or a new preregistered diagnostic justifies them. Unchanged state is quiet; meaningful results, failures and necessary user actions are surfaced. This is not a guarantee of uninterrupted execution.

The manual `canary-readiness-research` workflow defaults to discovery. Explicit `challenger` mode additionally exports a fixed research-only model from the same training fit, separately from the normal promotion result. Export does not promote a failed candidate. Its episode starts after freezing and all consumed observations, lasts 24 hours, and binds the model and execution protocol. Explicit `forward` mode consumes the locally verified model supplied through the GitHub Actions secret `QUALITY_FIRST_FROZEN_INPUT_B64`; it does not train again. Missing future observations are reported as awaiting data, and observations outside the frozen episode are excluded. Repeated runs replace the same sampled episode and are not independent trials. A finished episode can still be replayed to collect its final evidence.

Frozen research episodes restrict archive acquisition to the UTC dates intersecting their half-open 24-hour window. Missing dates retain their byte quota; no matching dates stop acquisition with a sealed diagnostic instead of falling back to old history. The caller must explicitly choose the archive cap; forward collection uses 192 when requested, within the same 1 GiB byte budget. Date-based selection records partial coverage and does not itself certify fresh observations.

Result artifacts contain only ciphertext and a fixed operational status. Completed main-branch manual runs trigger `sealed-journal-persist`, which verifies this allowlist and persists the exact envelopes on the dedicated `sealed-research-journal` branch under opaque episode identities. This branch contains no executable workflow, plaintext findings or personal decryption key. An uncertain write is read back before retrying. Forward mode is paper research: source archive sampling does not certify continuous exchange-event coverage, actual orders, fills or cash receipts.

Public source code and generic methodological contracts remain reviewable. Per-run hypotheses, findings, selected wallet cohorts and empirical results belong only inside encrypted payloads. The local identity can be recovered with the user's private unlock code plus a random recovery file; the short code alone is not the public-data encryption key. Both recovery files stay outside the repository. Existing owner-only legacy key backups must also remain private.

The execution boundary remains unchanged: research does not submit orders, move account funds, resume paused buying or treat an evaluation pass as authorization. A real-money canary requires a separately defined and authorized execution episode.
