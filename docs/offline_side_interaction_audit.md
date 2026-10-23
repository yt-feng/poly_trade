# Private side-conditioned model comparison

This runner changes one fixed prediction rule on an unchanged historical
cohort. The reference is a previously audited model's cached score metrics and
frozen execution candidates. Only the treatment is fitted in the new run.

The treatment fits a signed physical price residual using the original
train-only feature scaler. Its output, including the intercept, is mapped back
by the decision-side sign before adding the decision bid and clipping the price.
This is a different linear model class. It does not establish complementary bid
prices, exact token-renaming symmetry or a profitability improvement.

Before any fit, the runner verifies immutable adapter hashes, the complete
reconstructed feature CSV, temporal training membership and the scored
known/unknown opportunity identities. It then replays the frozen control
candidates and compares every registered full execution-result fingerprint,
including attempts, inventory and cash. An invalid binding prevents treatment
training. Reproducing control execution does not regenerate or refit the old
model's complete prediction stream.

Physical-target errors are paired with both the cached model and unchanged-bid
persistence. Economic results retain the same costs, quote selection, fixed
holding period and one evolving cash ledger. Separate diagnostic blocks cannot
be spliced into a sequence. A stream with fewer candidates than required entries
cannot pass by changing execution or sizing after selection. Order constraints,
cash release and quoted fills remain explicit simulation assumptions.

`offline-side-interaction-audit.yml` reads gzip-compressed, base64-encoded JSON
from its dedicated `OFFLINE_SIDE_INTERACTION_CONFIG_B64` secret. The dispatch
digest binds the exact expanded bytes. Encoded, compressed and expanded lengths
are bounded; truncated, concatenated and trailing streams are rejected. Success
and failure evidence stays private until sealing, ciphertext is retained before
publication, and runner-local plaintext is always removed. Model fit evidence
and successful paired evaluation are separate status fields.
