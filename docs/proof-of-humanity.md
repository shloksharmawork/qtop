# Proof of Humanity, made lighter

Status: proposal + working PoC for [#551](https://github.com/qtop/qtop/issues/551).
Reference implementation: [`tools/poh.py`](../tools/poh.py), state in [`poh/`](../poh/),
tested in [`tests/test_poh.py`](../tests/test_poh.py).

## The problem

`CONTRIBUTING.md` currently asks contributors to advertise their good faith by
adding tags to the PR subject - `(ORCID)`, `(SCHOLAR)`, `(LI)`, `(SDP)`, `(PRO)`,
`(human)`. That works, but it puts the burden in the wrong place: the tags are a
free-text claim, nothing checks them, and a maintainer still has to judge each
one by hand. Meanwhile the PR queue keeps filling up.

#551 asks for something *lighter* and *more rewarding*, and sketches a route
through Keybase / keyoxide plus a signed commit and an emailed nonce.

## Design goals

1. **Machine-checkable.** A maintainer should not have to read a tag and trust it.
2. **Lighter than the alternatives.** No new third-party account, no PGP
   keyserver, no `ssh-keygen -Y sign` ceremony, no email infrastructure.
3. **Offline and testable.** It has to run in CI without network access.
4. **Welcoming.** It must be advisory, never a wall. Nobody gets blocked for
   being new - they just get a lower tier until they complete a round.
5. **Expensive for puppets.** A bot must not be able to pass by generating text.

## Options considered

| Option | Verdict |
| --- | --- |
| **Keep the subject tags** | Free text, unverifiable, hand-judged. This is what #551 wants to move away from. |
| **Keybase / keyoxide proofs** | Strong, but the contributor has to create and maintain a third-party identity, and keyoxide proofs are not machine-checkable from a git commit. Heavier than the problem. |
| **OAuth-style "login with GitHub"** | Needs a server, secrets and a database. qtop is a CLI project with no service to host it. |
| **Manually verify `ssh-keygen -Y sign` output** | Correct, but it is a second signing tool on top of `git commit -S`, and it produces a detached artefact that has to be carried alongside the commit. |
| **Signed commit + nonce + pinned key in one yaml file** (chosen) | Reuses what git already does, one round-trip, one file, no service, and the key is on the record so the nonce cannot be answered by a stranger. |

## The flow

```
contributor                         maintainer                      CI
     |                                   |                          |
     |  PR with claims.yaml entry        |                          |
     |---------------------------------->|                          |
     |                                   |  poh challenge --handle  |
     |<----------------------------------|  (nonce, single use)     |
     |                                   |                          |
     |  git commit -S  with trailer      |                          |
     |  Proof-of-Humanity-Nonce: <nonce> |                          |
     |---------------------------------->|------------------------->|
     |                                   |        poh verify -> TIER 1
```

Three commands, one round-trip, one file each side.

## Why the nonce has to be *signed*

A nonce alone proves nothing: anyone reading the issue thread can copy it. The
value comes from the combination:

- the nonce is **single-use** (spent on a passing verification) and **short-lived**
  (`--ttl`, default 30m), so it cannot be replayed or stockpiled;
- the nonce must be inside a **signed** commit, so answering it requires a private
  key;
- the matching public key is **pinned in `claims.yaml`**, which lands in the repo
  through a reviewable pull request;
- the signing identity must equal the **email claim**, so an unrelated key cannot
  answer for someone else's entry.

A sock puppet therefore needs a real key pair, a reviewed entry in the repo, and
a fresh challenge per attempt - which is exactly the "fence" #551 is asking for,
without any of the ceremonies it hoped to avoid.

## Tiers

| Tier | Requirements | Meaning |
| --- | --- | --- |
| 0 | a signed commit | you exist and control a key |
| 1 | 3+ valid claims from distinct providers, signature bound to the email claim, and a fresh nonce in the signed commit | an established contributor |
| 2 | tier 1 plus a recorded live attestation (`attestations:` in the yaml) | you accepted the "any challenge" option, e.g. a live demo of coloured qtop output |

`--require-tier` makes the gate explicit; `--json` emits the same report for
tooling. CI runs at tier 1 and stays **advisory** (see
[`.github/workflows/poh.yml`](../.github/workflows/poh.yml)): a failed round adds
a notice, it does not close the PR.

## What is actually checked

Claims (format-validated offline, no network):

- **ORCID** - ISO 7064 MOD 11-2 checksum, not just the shape
- **email** - shape, plus a small disposable-inbox fence
- **github / gitlab** - handle grammar, and the github handle must match the entry
- **linkedin** - canonical `/in/<slug>` profile URL
- **matrix** - `@user:server` grammar
- **minimum count** - `MIN_CLAIMS` (3) is the gate, `IDEAL_CLAIMS` (5) is what #551 asks for

Cross-checks in the file as a whole:

- no email or ORCID claimed by two handles
- no ssh key shared by two handles

Signature checks:

- `git verify-commit` against an `allowed_signers` file the tool **builds from
  `claims.yaml`**, so verification needs no local git configuration
- the commit's **author email** must equal that entry's email claim

That last point is subtler than it looks. `git verify-commit` prints
`Good signature for <principal>`, but that principal is the one *we* supplied
through `allowed_signers` - comparing it to the claim would be circular and would
always pass. The binding therefore comes from the author email recorded inside
the commit, which the signature covers.

## The `card` command

`poh card --handle <handle>` prints a small contributor card. #551 also asks for
the process to be *more engaging*, and a printable artefact costs nothing:

```console
$ python3 tools/poh.py card --handle qtop-contributor-example
+----------------------------------------------------------+
|  qtop proof-of-humanity contributor card                 |
+----------------------------------------------------------+
|  @qtop-contributor-example                               |
|  Example Contributor                                     |
|  tier 1   claims x.xxxx                                  |
|  providers: email, github, linkedin, matrix, orcid       |
+----------------------------------------------------------+
```

## Demo: the whole round, locally

```console
$ ssh-keygen -q -t ed25519 -N "" -C poh-demo -f /tmp/poh/id_ed25519
$ python3 tools/poh.py init --handle alice --email alice@example.org    # paste into poh/claims.yaml
$ python3 tools/poh.py challenge --handle alice                         # -> qp-....
$ git commit -S -m "fix: something

Proof-of-Humanity-Nonce: qp-...."
$ python3 tools/poh.py verify --handle alice --commit HEAD --require-tier 1
```

Expected report:

```
Proof of Humanity report - @alice
========================================================================
  claims               OK  5 valid (email, github, linkedin, matrix, orcid); minimum 3
  cross-checks         OK  no duplicate identity or shared key
  commit signature     OK  SHA256:...
  signature binding    OK  signer alice@example.org matches the email claim
  nonce                OK  nonce accepted
------------------------------------------------------------------------
TIER 1 (claims + signature + nonce) - required 1 -> PASS
```

Running `verify` a second time with the same commit reports
`no nonce was issued for alice` - the replay protection working.

## Limitations, stated honestly

- **SSH signatures only.** The tool builds `allowed_signers` for ssh keys. GPG
  keys would need a `git verify-commit` path with a keyring; a two-line addition
  once someone asks for it.
- **Format, not truth, offline.** A well-formed ORCID is not proof the profile
  exists. An optional `--online` mode could resolve the claim URLs and compare
  the linked github handle - deliberately left out so CI needs no network.
- **Nonces are committed.** They are single-use and expire, so publishing them is
  fine, but a maintainer who prefers them out of git can point `--state` anywhere.
- **Email delivery is out of scope.** See `poh/README.md` for why, and how to add it.
- **It is a fence, not a wall.** Someone determined with a real identity and a
  real key can still pass tier 1 - that is a *human*, which is the point.
