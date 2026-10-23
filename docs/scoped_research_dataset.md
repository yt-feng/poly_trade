# Scoped saved research data

`tools/scoped_research_dataset.py` reuses the journal envelope format to move
one explicitly registered, saved dataset into a later private cloud job. It
does not acquire market data, fit models, replay policies, unlock a journal
identity, publish files, execute processes, or make network requests.

The local caller supplies an allowlist of data files and, optionally, an origin
manifest as another file. `pack` generates `dataset-manifest.json`, which lists
each caller file's byte length and SHA-256. No directory scan or implicit file
inclusion occurs. The resulting package contains at most eight regular files,
including that generated manifest. Each file is at most 32 MiB, file contents
and the expanded tar are separately bounded by 64 MiB, the one gzip stream is
at most 8 MiB, and its encrypted JSON envelope is at most 24 MiB. Tar padding
and manifest overhead count toward the expanded-tar bound.

```python
from pathlib import Path
from tools import scoped_research_dataset as saved

envelope, capability, descriptor = saved.pack(
    {"features.csv": Path("/private-input/features.csv"),
     "origin.json": Path("/private-input/origin.json")},
    verified_public_recipient, verified_recipient_id,
    expected_files=previously_verified_file_lengths_and_sha256,
)
registered_descriptor_sha256 = saved.descriptor_sha256(descriptor)
saved.write_capability(
    Path("/private-operations/saved-dataset-capability.json"), capability,
    working_tree=Path("/repository-checkout"),
)
```

`expected_files`, when supplied, must contain exactly the caller file names;
each value has exactly `bytes` and `sha256`. The packer rejects mismatches
before encryption. Input opens are nonblocking and do not follow symlinks;
only regular files with one hard link are accepted. File size, modification
and change times, link count, device and inode must remain unchanged across
each bounded read. The capability writer creates a new mode-0600 file outside
the specified checkout and refuses existing paths. The API otherwise returns
objects in memory and writes nothing: the caller must keep the capability
outside version control, public artifacts, logs and research-output bundles.

The public descriptor has exactly these fields:

| Field | Meaning |
| --- | --- |
| `version` | Integer `1` |
| `format` | `scoped-research-dataset-v1` |
| `cipher_sha256`, `cipher_bytes` | Exact encrypted-envelope bytes |
| `bundle_sha256`, `bundle_bytes` | Exact compressed tar bytes |
| `files` | Sorted entries with exactly `path`, `bytes`, `sha256` |

The separate capability has all descriptor fields and one additional field,
`content_key_b64`, containing only this envelope's 32-byte AES key. Descriptor
SHA-256 uses `research_vault.canonical`; it excludes the key. File names,
sizes, hashes and the descriptor can themselves disclose research metadata,
so public here means non-key material, not permission to publish a private
dataset's descriptor. Store the registered descriptor digest and source
locator through the existing private experiment configuration channel.

Each pack operation makes a new ephemeral X25519 key and salt, derives a
content key with the existing HKDF-SHA256 info value, and uses the exact
AES-256-GCM header/AAD contract in `research_vault`. Existing local journal
decryption can open the envelope. A scoped capability opens only the exact
envelope registered by its descriptor; it contains no recipient private key,
recovery file, identity bundle, password, or general journal access. Giving a
job this capability necessarily gives that job access to those dataset files.

The plaintext envelope retains the established single-part
`quality-first-research-tar-gzip-part-v1` wrapper (`index=0`, `total=1`), so
existing ciphertext publication does not need a second encryption protocol.
The caller remains responsible for supplying generic publication status and
for excluding the capability and restored plaintext from published artifacts.

Before fetching ciphertext, a runner should call
`validate_capability(capability, expected_descriptor_sha256)`. The caller
must separately restrict any source locator to an allowed repository/path
and a previously frozen immutable commit, verify actual remote bytes, and
apply a bounded download. This module accepts only already-acquired envelope
bytes and never follows a URL or a path from inside the dataset.

```python
saved.validate_capability(capability, registered_descriptor_sha256)
report = saved.restore(
    already_acquired_envelope_bytes, capability,
    registered_descriptor_sha256, Path("/runner-private/restored-dataset"),
)
```

The command-line equivalent is:

```text
python tools/scoped_research_dataset.py restore \
  --envelope /runner-private/part-0000.vault \
  --capability /runner-private/capability.json \
  --expected-descriptor-sha256 REGISTERED_DESCRIPTOR_SHA256 \
  --output /runner-private/restored-dataset
```

Restoration verifies the registered descriptor and ciphertext digest before
AES decryption, then enforces strict JSON schemas, canonical base64, duplicate
key rejection, authenticated header fields, one bounded gzip stream, and only
the exact allowlisted tar members. It rejects extension headers, directories,
links, devices, traversal, duplicate/missing/extra members, nonzero padding,
trailing archives, and per-file length/hash or manifest mismatches. It never
uses `extractall`. After all checks pass, it reserves a new output directory,
writes a sibling private staging directory, then replaces the empty
reservation. Output directories use mode 0700 and files mode 0600. Verification
failure creates no output; an I/O failure removes this operation's staging and
reservation. Existing output files, directories and symlinks are never
replaced. An interrupted process can leave a reservation/stage, which must be
inspected before an explicit cleanup and retry; their presence is not proof
that restoration finished.

The returned report and CLI messages contain no key or dataset content.
`plaintext_published=false` refers to public publication, not to the private
restored files. `strategy_evaluated=false` and `live_orders_enabled=false`
describe this transport operation; restoring saved data is not a trading or
model result. The surrounding workflow must redirect detailed job output to
private storage, exclude capabilities/restored data from its sealed evidence
bundle, and clean runner-private files even when a later step fails.

The manual `scoped-saved-input-verification` workflow exercises that boundary.
Its separate configuration and capability secrets are materialized into
different private directories. `verify_saved_research_input.py` accepts only
an immutable commit and an allowlisted journal path in the calling repository;
it downloads bounded ciphertext with GitHub's raw contents media type, refuses
redirects, and verifies the capability before fetching. It checks each CSV's
registered columns, unique cohort identities and ordered cross-table joins.
Timezone-aware timestamps are normalized without silently discarding nonzero
submicrosecond precision. It deletes the capability and decoded dataset before
sealing its verification report. Success proves saved-input integrity and
joins only; it does not re-establish raw quote provenance or strategy returns.
