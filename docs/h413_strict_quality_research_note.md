# H413 strict-quality H30 rolling stability

H413 repairs the evidence boundary of the earlier rolling-window diagnostic.
It replays the frozen `h30_move_uncertainty` signal on the pinned old-clean
source after applying the same strict-quality filter used by H406. It does not
collect new data, refit a model, change thresholds, or select on holdout.

Each predeclared overlapping window starts one fresh `$10` diagnostic ledger.
The required ten entries are chronological and funded by the previous ledger
state; windows are never concatenated. Window rows are paper evidence only and
do not count as authenticated Canary fills.
