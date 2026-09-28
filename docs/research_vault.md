# Research journal confidentiality

This is generic security tooling, not a strategy report. No new empirical findings, cohort addresses, strategy thresholds or account data are published by this change.

## Status and boundary

A personal recipient has NOT been initialized. Until it is, do not publish new private journal content. A four-digit PIN alone has only 10,000 possible values and is not suitable to protect public ciphertext. This design uses a random X25519 private identity; the two local recovery files and an interactively entered PIN are required to unlock it. The PIN is never hardcoded or committed. Public-key encryption lets a later authorized process seal a new entry with only the public recipient, without accessing any private identity.

Existing plaintext Git history, reports, PR descriptions, workflow output, release assets and downloaded copies are NOT retroactively hidden by adding encryption. No destructive history rewrite or repository-visibility change is performed here. Treat previously published material as public. Encryption does not erase it.

## One local initialization (run from the repository root)

```bash
python3 -m pip install -r requirements-vault.txt
python3 tools/research_vault.py init --identity-dir "$HOME/.config/poly-research-vault"
```

Enter the user-selected PIN at the hidden prompt. Keep `identity.keybundle`, `recovery.key` and the public fingerprint in that directory, outside Git. Back up the two private files offline. Losing either private file or the PIN makes recovery impossible. Compromise of both private files allows offline guessing of a short PIN; whole-device protection and an offline recovery backup still matter. Never initialize a real identity in a public GitHub runner. Only `research_vault/recipient.json` may be committed. Independently verify its fingerprint before reuse or key rotation.

## Seal a work journal

Create notes outside the working tree using the generic JSON template. The template records work summaries, sources, experiments, failures, causal limitations and next actions; it must not contain private reasoning traces, credentials or personal financial secrets.

```bash
RECIPIENT_ID=$(cat "$HOME/.config/poly-research-vault/recipient-id.txt")
python3 tools/research_vault.py seal \
  --input "$HOME/private-research/entry.json" \
  --output research_vault/entries/opaque-id.vault \
  --recipient-id "$RECIPIENT_ID"
```

Use neutral entry names. Only publish ciphertext and neutral operational metadata. Do not disclose the plaintext filename, wallet cohort, strategy name or findings in commits, PR bodies, issues, logs, artifact names or step summaries. Successful syntax inspection is NOT authentication; successful decryption checks AEAD integrity. Anyone possessing a public recipient can generate a new ciphertext, so author authenticity and ordering still depend on trusted repository access and verified log references.

## Open locally

```bash
python3 tools/research_vault.py open \
  --input research_vault/entries/opaque-id.vault \
  --identity-dir "$HOME/.config/poly-research-vault" \
  --output "$HOME/private-research/opened-entry.json"
```

The PIN is requested interactively. Plaintext is written outside the working tree with owner-only permissions, not printed. An existing file is never overwritten. Do not pass a PIN on the command line or put it in an environment variable, GitHub variable, issue or workflow log. Do not send account private keys to initialize this unrelated journal identity.

## Limitations

The envelope uses PyCA X25519, HKDF-SHA256 and AES-GCM. The local identity uses a fixed Scrypt work factor and AES-GCM with a random recovery secret plus PIN. This is application-level integration, not a separately audited cryptosystem. Ciphertext sizes and commit times remain visible; no anonymity or secure deletion guarantee is made. `.vault` path/schema checks are heuristics, not a full semantic secret scanner. In an unprotected branch, post-push CI detects a leak only after publication; the publisher must seal BEFORE pushing. This workflow does not add branch protection or police all legacy Actions.

Encryption must never be used to upload code or content that was blocked for security or authorization reasons. It is for authorized work summaries and evidence, not a bypass mechanism.

## Tests

The dedicated workflow uses only synthetic identities and synthetic observations. It never generates or publishes a personal decryption key and never fetches an account. The downloadable artifact contains only tools, templates, tests and this guide. Passing tests is engineering evidence, not a profitability claim.

Primary implementation references:
- https://cryptography.io/en/latest/hazmat/primitives/asymmetric/x25519/
- https://cryptography.io/en/latest/hazmat/primitives/aead/
- https://cryptography.io/en/46.0.7/hazmat/primitives/key-derivation-functions/
