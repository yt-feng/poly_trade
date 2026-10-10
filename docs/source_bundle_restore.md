# Restore an encrypted source bundle

The ordered manifest is `research/strategy/runs/source-preservation-20261010.json`.
It references actual encrypted data committed alongside the index. This bundle
preserves independently recovered original inputs. It is **not** a byte-identical
copy of the earlier workflow ZIP and does not preserve every original run output.
The sealed `provenance/preservation-status.json` lists the exact recovered scope,
source versions, verification, reconstruction assumptions and missing content.
The original per-file run manifest was unavailable; matching historical totals
does not independently establish the identity of every historically selected file.

Each plaintext part is at most 16 MiB. Existing X25519/HKDF/AES-GCM envelopes
produce files below 24 MiB. The index records part order, plaintext and ciphertext
lengths and SHA-256 hashes, plus the complete tar hash. Source asset digests,
checksum sidecars, gzip/JSONL decoding, tar member hashes, split/reassembly and
a full-bundle round-trip with a temporary synthetic identity were checked locally.
The existing recipient was compared with its pinned historical public key and
prior envelopes. No personal private identity was read: these checks are **not**
a successful decryption test with the user's recovery files.

## Passphrase compatibility

The existing public-key format requires **both matching private recovery files
and the identity's PIN**. The originally selected passphrase alone cannot decrypt
this package. A committed public recipient does not establish that the user has
received or retained `identity.keybundle` and `recovery.key`, or which PIN protects
them. Their possession and a real user-identity restore remain unverified. The
temporary synthetic identity used for testing has no ability to open production
ciphertext. This work does not change the protocol or generate replacement keys.

## Recover locally

Run from the repository root with Python 3.11 or later. Use the existing identity
and recovery files described in [the vault guide](research_vault.md). Do not create
a replacement identity, commit private files, or enter a PIN in a command line.
The output directory must be new and outside the checkout. The command prompts
once, authenticates every envelope, verifies the complete tar and all member
hashes, then extracts only regular files under the selected directory.

```bash
python3 -m pip install -r requirements-vault.txt
export BUNDLE_IDENTITY_DIR="$HOME/.config/poly-research-vault"
export BUNDLE_OUTPUT_DIR="$HOME/private-research/restored-source-bundle"
python3 - <<'PY'
import getpass, hashlib, json, os, sys, tarfile
from pathlib import Path, PurePosixPath
sys.path.insert(0, 'tools')
import research_vault as vault

manifest = json.loads(Path('research/strategy/runs/source-preservation-20261010.json').read_bytes())
identity = vault.require_private_location(os.environ['BUNDLE_IDENTITY_DIR'])
output = vault.require_private_location(os.environ['BUNDLE_OUTPUT_DIR'])
if output.exists():
    raise ValueError('Output directory must be new')
parts = manifest['parts']
if [p['sequence'] for p in parts] != list(range(1, len(parts) + 1)):
    raise ValueError('Invalid part order')
for part in parts:
    path = Path(part['path'])
    if path.is_absolute() or '..' in path.parts or path.is_symlink():
        raise ValueError('Unsafe envelope path')
    data = vault.read_bounded(path, vault.MAX_ENVELOPE)
    if len(data) != part['ciphertext_bytes'] or hashlib.sha256(data).hexdigest() != part['ciphertext_sha256']:
        raise ValueError('Ciphertext checksum mismatch')
private = vault.unlock_identity(
    vault.read_bounded(identity / 'identity.keybundle', 4096),
    vault.read_bounded(identity / 'recovery.key', 32),
    getpass.getpass('Local PIN: '))
if vault.recipient_for(private)['recipient_id'] != manifest['recipient_id']:
    raise ValueError('Wrong identity')
output.mkdir(parents=True, mode=0o700)
archive = output / 'verified-source-bundle.tar'
try:
    whole = hashlib.sha256()
    size = 0
    with archive.open('xb') as stream:
        os.chmod(archive, 0o600)
        for part in parts:
            block = vault.unseal(vault.read_bounded(part['path'], vault.MAX_ENVELOPE), private)
            if len(block) != part['plaintext_bytes'] or hashlib.sha256(block).hexdigest() != part['plaintext_sha256']:
                raise ValueError('Plaintext part checksum mismatch')
            stream.write(block)
            whole.update(block)
            size += len(block)
    if size != manifest['plaintext_bytes'] or whole.hexdigest() != manifest['plaintext_sha256']:
        raise ValueError('Complete bundle checksum mismatch')
    with tarfile.open(archive, 'r:') as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)):
            raise ValueError('Duplicate tar member')
        if any(not m.isfile() or PurePosixPath(m.name).is_absolute() or '..' in PurePosixPath(m.name).parts for m in members):
            raise ValueError('Unsafe tar member')
        inventory = json.load(tar.extractfile('inventory.json'))['files']
        if set(names) != {'inventory.json'} | {x['path'] for x in inventory}:
            raise ValueError('Inventory mismatch')
        for item in inventory:
            member = tar.getmember(item['path'])
            if member.size != item['bytes'] or hashlib.file_digest(tar.extractfile(member), 'sha256').hexdigest() != item['sha256']:
                raise ValueError('Member checksum mismatch')
        tar.extractall(output / 'files', filter='data')
except Exception:
    archive.unlink(missing_ok=True)
    raise
print('Authenticated, reassembled and verified locally. Review sealed provenance for missing original outputs.')
PY
```

Do not publish extracted source selections, manifests, journals or findings.
The manifest's full bundle hash identifies this reconstruction, not the old ZIP.
No source observation becomes a confirmed fill or unseen evaluation by being
preserved. The original run remains incomplete until its missing outputs are
independently recovered.

An additive encrypted audit, referenced by the index's `supplements` list, records
the source search, available-input inventory and precise reproduction limits.
It is a standalone vault envelope containing UTF-8 JSON, not another tar part.
Open a supplement with the existing `tools/research_vault.py open` command and
the same identity, writing outside the checkout. Do not append it to the ordered
tar parts. This audit does not replace missing observations with inferred winners.

## Repository and workflow scope

Only encrypted data, a neutral manifest and recovery documentation are added or updated. No
collector, trading code, dependency or workflow is changed. Under the inspected
main workflow configuration, these paths trigger only `research-vault-contract`:
its existing offline synthetic tests and existing tooling-only artifact (14-day
retention), not an upload of this source bundle. No new paid service, budget or
retention setting is introduced. Full source bytes live in Git as bounded files.
