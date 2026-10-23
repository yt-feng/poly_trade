# Private chronological quote-forecast audit

This runner evaluates a registered price-residual forecast against unchanged-bid
persistence on the same historical opportunities. It uses archived best quotes
and sizes, reconstructs prefix features and rejects incomplete or conflicting
prefixes. Terminal prices, resolution labels and future book quality cannot
admit test markets. Future quote targets may be unknown without removing the
corresponding test opportunity.

Each fit uses earlier source runs with labels available before the first
decision of the evaluated run. Scaling is learned from those training rows
only. Results retain training membership, fitted coefficients, preprocessing
parameters, predictions and paired known-target errors for independent checks.
Repeatedly examined archives remain retrospective evidence.

Trading intent is determined from decision quotes and predicted proceeds after
both sides' declared costs. Arrival quotes must also satisfy the frozen cost
ceiling. Execution selects the first price-valid quote in a bounded window,
then checks size; it never searches for profitable or deep future quotes.
Fixed-horizon losses are retained. A missing or insufficient exit leaves the
debited position unresolved and stops that ledger.

Each primary path starts with one cash ledger and sizes subsequent entries from
its available proceeds. Separate diagnostic blocks reset cash and cannot be
joined into a claimed sequence. Completing a sequence and achieving strict net
growth are reported separately. Quote timestamps, fees, available depth and cash
release remain explicit simulation assumptions, not authenticated fills or cash
receipts. No order submission is available in this runner.

`offline-bid-residual-audit.yml` reads the exact base64-encoded JSON input from
the dedicated `OFFLINE_BID_RESIDUAL_CONFIG_B64` secret. Dispatch provides only its
SHA-256 digest. Source and code commits, data order and computation bounds must
match the private input. The workflow seals configuration, stdout, stderr and
results, retains ciphertext before journal publication and always removes local
plaintext. Failed or empty evaluations cannot claim strategy evaluation; model
fit evidence is recorded separately from successful policy evaluation.
