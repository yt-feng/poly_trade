# Reproducible strategy research records

`research/strategy/` is the public ledger for strategy hypotheses, replay
protocols, and canary-readiness evidence. Every experiment is registered before
its data is inspected, and every run records the source commit, UTC windows and
dates, exact command, environment, outputs, and checksums. This repository owns
strategy/research and paper execution; public capture remains in `yt-feng/poly`.

Keep private reasoning, account exports, order receipts, and any personally
identifying material outside GitHub or as ciphertext only. The selected user
passphrase is supplied at runtime through `ARCHIVE_KEY`; the value supplied out
of band is never stored in the repository, an envelope, a workflow, or a log:

```bash
python -m pip install -r requirements-research.txt
export ARCHIVE_KEY='<user-supplied-runtime-value>'
python tools/archive_crypto.py encrypt private-notes.json research/strategy/private/private-notes.json.enc
python tools/archive_crypto.py decrypt research/strategy/private/private-notes.json.enc /tmp/private-notes.json
unset ARCHIVE_KEY
```

The utility uses scrypt and AES-256-GCM authenticated encryption and performs no
network or trading action. A four-digit PIN is weak against offline guessing
and must not be described as strong confidentiality; use a long random
`ARCHIVE_KEY` for meaningful protection. Encryption prevents accidental
plaintext publication but does not erase public Git history. Public quotes and
paper fills remain observations or simulations; they cannot satisfy real-fill
canary gates.
