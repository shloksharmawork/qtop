# `poh/` - Proof-of-Humanity state

This directory holds the machine-readable half of the qtop PoH round described in
[issue #551](https://github.com/qtop/qtop/issues/551). The reasoning behind the
design is in [`docs/proof-of-humanity.md`](../docs/proof-of-humanity.md).

| File | Who writes it | What it is |
| --- | --- | --- |
| `claims.yaml` | contributors (via PR) | one entry per contributor: identity claims + the ssh public key that signs their commits |
| `nonces.json` | maintainers (via commit) | the single-use challenges that have been issued, with expiry |

Nothing here is secret. Nonces are random, short-lived, single-use values, and
the public keys are meant to be public - being on the record is the whole point.

## Contributor: three commands

```console
$ python3 tools/poh.py init --handle <your-github-handle> --email <you@example.org>
# paste the output under `contributors:` in claims.yaml, and put your public key in `signing.key`

$ git commit -S -m "fix: ...

Proof-of-Humanity-Nonce: <the nonce a maintainer sent you>"

$ python3 tools/poh.py verify --handle <your-github-handle> --commit HEAD
```

`verify` prints a report and a tier. Tier 1 is what a normal contribution needs.

## Maintainer: issuing a challenge

```console
$ python3 tools/poh.py challenge --handle <contributor-handle>   # writes poh/nonces.json, prints the nonce
$ git add poh/nonces.json && git commit -m "poh: challenge for <handle>"
```

Send the nonce over any channel that reaches a human (the issue thread is fine).
The contributor signs it into a commit; `tools/poh.py verify` accepts it once.

## Why not email

Issue #551 suggests delivering the nonce by email. Delivery is deliberately kept
out of the checker: the tool stays offline and testable, and the maintainer can
use whatever channel already reaches a real person. Wiring an SMTP sender in is a
ten-line addition to `cmd_challenge` if the project wants it.
