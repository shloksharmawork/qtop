#!/usr/bin/env python3
##
## qtop is a tool to monitor queuing systems - https://github.com/qtop/qtop
##
## Copyright (c) 2026 qtop contributors
##
## SPDX-License-Identifier: MIT
##
"""Proof-of-Humanity (PoH) verification for qtop contributions (issue #551).

Goal: make the qtop PoH round *lighter*, while still raising a fence against
bots and sock puppets. It replaces the manual "(ORCID) / (LI) / (human) tags in
the PR subject" convention with one machine-checkable loop:

    1. the contributor keeps one entry in a single yaml file (``poh/claims.yaml``)
    2. the maintainer issues a nonce for that handle
    3. the contributor puts the nonce into a *signed* commit and pushes it
    4. ``poh verify`` checks claims + signature + nonce and reports a tier

Why this is lighter than the Keybase/keyoxide route suggested in #551:

- no third-party account, no PGP keyserver, no ``ssh-keygen -Y sign`` ceremony;
  ``git commit -S`` with an ssh key is enough
- exactly one round-trip (nonce), not a chain of proofs
- the whole state is one yaml file plus one nonce store, so it runs offline in CI
- the public key is pinned *inside* the claims file, so the tool builds its own
  ``allowed_signers`` file and needs no local git configuration

Design notes and the anti-bot reasoning live in ``docs/proof-of-humanity.md``.

Usage
-----

    python3 tools/poh.py init      --handle alice --email alice@example.org
    python3 tools/poh.py challenge --handle alice
    python3 tools/poh.py verify    --handle alice --commit HEAD --require-tier 1
    python3 tools/poh.py card      --handle alice

Exit status: 0 when the reported tier reaches ``--require-tier`` (default 1),
1 otherwise, so it can gate a pull request directly.
"""

import argparse
import datetime
import json
import os
import re
import secrets
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - yaml ships with the qtop test env
    yaml = None


DEFAULT_CLAIMS = os.path.join("poh", "claims.yaml")
DEFAULT_STATE = os.path.join("poh", "nonces.json")
NONCE_TRAILER = "Proof-of-Humanity-Nonce"
DEFAULT_TTL = "30m"
MIN_CLAIMS = 3
IDEAL_CLAIMS = 5  # what #551 asks for ("~5 claims per person")
TIER_NAMES = {0: "signed commit only", 1: "claims + signature + nonce", 2: "tier 1 + live attestation"}

## Claim providers we can check the *format* of offline. `email` doubles as the
## identity that must match the commit signature, so it is special-cased.
PROVIDERS = ("github", "gitlab", "email", "orcid", "linkedin", "matrix")

## Cheap fence: throwaway inboxes are the usual sock-puppet substrate. This is a
## deliberately small, easily extensible list, not a claim of completeness.
DISPOSABLE_DOMAINS = frozenset(
    (
        "10minutemail.com",
        "guerrillamail.com",
        "mailinator.com",
        "sharklasers.com",
        "temp-mail.org",
        "throwawaymail.com",
        "trashmail.com",
        "yopmail.com",
    )
)

GITHUB_HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
GITLAB_HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,254}$")
EMAIL = re.compile(r"^[^@\s]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+$")
LINKEDIN = re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]{3,100}/?$")
MATRIX = re.compile(r"^@[a-z0-9._=/+\-]+:[a-z0-9.\-]+(?::\d{1,5})?$")
ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
SSH_KEY = re.compile(r"^(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|sk-ssh-ed25519@openssh\.com)\s+[A-Za-z0-9+/=]{20,}")


# --------------------------------------------------------------------- claims


def orcid_is_valid(value):
    """ORCID iD check: ISO 7064 MOD 11-2 over the first 15 digits.

    Example: 0000-0002-1825-0097 -> the trailing 7 is the checksum.
    """
    if not isinstance(value, str) or not ORCID.match(value):
        return False
    digits = value.replace("-", "")
    total = 0
    for char in digits[:15]:
        total = (total + int(char)) * 2
    check = (12 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    return expected == digits[15]


def email_is_valid(value):
    """Plausible address on a domain that is not a known throwaway inbox."""
    if not isinstance(value, str) or not EMAIL.match(value):
        return False
    domain = value.rsplit("@", 1)[1].lower()
    return domain not in DISPOSABLE_DOMAINS


def linkedin_is_valid(value):
    return isinstance(value, str) and bool(LINKEDIN.match(value.strip()))


def matrix_is_valid(value):
    return isinstance(value, str) and bool(MATRIX.match(value.strip()))


def github_is_valid(value):
    return isinstance(value, str) and bool(GITHUB_HANDLE.match(value))


def gitlab_is_valid(value):
    return isinstance(value, str) and bool(GITLAB_HANDLE.match(value))


VALIDATORS = {
    "github": github_is_valid,
    "gitlab": gitlab_is_valid,
    "email": email_is_valid,
    "orcid": orcid_is_valid,
    "linkedin": linkedin_is_valid,
    "matrix": matrix_is_valid,
}


def check_claims(claims):
    """Return {provider: [errors]} for one contributor's claim mapping."""
    problems = {}
    if not isinstance(claims, dict):
        return {"claims": ["claims must be a mapping of provider: value"]}

    for provider, value in claims.items():
        if provider not in VALIDATORS:
            problems.setdefault(provider, []).append("unknown provider")
            continue
        if not VALIDATORS[provider](value):
            problems.setdefault(provider, []).append("value does not match the %s format" % provider)
    return problems


def load_claims(path):
    """Load and structurally validate the claims file.

    Returns (contributors, errors) where contributors is a list of dicts.
    """
    errors = []
    if yaml is None:
        return [], ["PyYAML is not importable, cannot read %s" % path]
    if not os.path.exists(path):
        return [], ["claims file not found: %s" % path]

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as error:
        return [], ["claims file is not valid yaml: %s" % error]

    if not isinstance(data, dict):
        return [], ["claims file must contain a mapping at the top level"]
    if data.get("version") != 1:
        errors.append("unsupported or missing `version` (expected 1)")

    contributors = data.get("contributors")
    if not isinstance(contributors, list) or not contributors:
        return [], errors + ["`contributors` must be a non-empty list"]

    for position, entry in enumerate(contributors):
        if not isinstance(entry, dict):
            errors.append("contributor #%s is not a mapping" % position)
            continue
        if not github_is_valid(entry.get("handle")):
            errors.append("contributor #%s has an invalid or missing `handle`" % position)
    return contributors, errors


def find_contributor(contributors, handle):
    for entry in contributors:
        if entry.get("handle") == handle:
            return entry
    return None


def cross_check(contributors):
    """Fence against sock puppets: no identity may appear twice in the file."""
    errors = []
    seen = {}
    for entry in contributors:
        handle = entry.get("handle", "?")
        claims = entry.get("claims") or {}
        signing = entry.get("signing") or {}
        for provider in ("email", "orcid"):
            value = claims.get(provider)
            if not value:
                continue
            key = (provider, value.lower())
            if key in seen:
                errors.append("%s %s is claimed by both %s and %s" % (provider, value, seen[key], handle))
            else:
                seen[key] = handle
        key = (signing.get("key") or "").strip()
        if key and SSH_KEY.match(key):  # ignore unmaterialised placeholders
            fingerprint = " ".join(key.split()[:2])
            lookup = ("key", fingerprint)
            if lookup in seen:
                errors.append("signing key %s... is shared by %s and %s" % (fingerprint[:24], seen[lookup], handle))
            else:
                seen[lookup] = handle
    return errors


# ----------------------------------------------------------------- signature


def allowed_signers_from(contributors):
    """Build an ssh `allowed_signers` file from the pinned public keys.

    The claims file is the source of truth for keys, so verification needs no
    local git configuration - one less manual step for a contributor.
    """
    lines = []
    for entry in contributors:
        signing = entry.get("signing") or {}
        key = signing.get("key")
        principal = signing.get("email") or (entry.get("claims") or {}).get("email")
        if key and principal and SSH_KEY.match(key.strip()):
            lines.append("%s %s" % (principal, key.strip()))
    return "\n".join(lines) + ("\n" if lines else "")


def verify_signature(repo, commit, allowed_signers_path):
    """Run `git verify-commit`. Returns (ok, signer, detail)."""
    if not allowed_signers_path:
        return False, None, "no pinned signing key available for this handle"

    command = [
        "git",
        "-C",
        repo,
        "-c",
        "gpg.ssh.allowedSignersFile=%s" % allowed_signers_path,
        "verify-commit",
        commit,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()

    if result.returncode != 0:
        if not output:
            return False, None, "commit %s carries no verifiable signature" % commit
        first = output.splitlines()[0]
        return False, None, first

    signer = None
    match = re.search(r"signature for (\S+)", output)
    if match:
        signer = match.group(1)
    fingerprint = None
    match = re.search(r"key (SHA256:\S+)", output)
    if match:
        fingerprint = match.group(1)
    return True, signer, fingerprint or "signature verified"


# ---------------------------------------------------------------------- nonce


def parse_duration(value):
    """'30m' / '2h' / '90s' -> seconds. Kept in step with fileutils.parse_time_input."""
    units = {"s": 1, "m": 60, "h": 3600}
    text = str(value).strip()
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise ValueError("duration %r must be a number followed by s, m or h, e.g. '30m'" % value)
    return int(text[:-1]) * units[text[-1]]


def _read_state(state_path):
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, OSError):
        return {}


def _write_state(state_path, state):
    directory = os.path.dirname(state_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
        handle.write("\n")


def issue_nonce(handle, state_path, ttl_seconds, now=None):
    """Create a fresh single-use nonce for a handle and persist it."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    nonce = "qp-%s" % secrets.token_hex(8)
    state = _read_state(state_path)
    state[handle] = {
        "nonce": nonce,
        "issued_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(seconds=ttl_seconds)).isoformat(),
    }
    _write_state(state_path, state)
    return nonce


def validate_nonce(handle, nonce, state_path, now=None):
    """Check a nonce without spending it, so a later failure cannot burn it."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    record = _read_state(state_path).get(handle)
    if not record:
        return False, "no nonce was issued for %s" % handle
    if record.get("nonce") != nonce:
        return False, "nonce does not match the one issued for %s" % handle
    expires = datetime.datetime.fromisoformat(record["expires_at"])
    if now > expires:
        return False, "nonce expired at %s" % record["expires_at"]
    return True, "nonce accepted"


def consume_nonce(handle, nonce, state_path, now=None):
    """Spend a nonce: check it, then remove it so it can never be replayed."""
    ok, reason = validate_nonce(handle, nonce, state_path, now=now)
    if not ok:
        return False, reason
    state = _read_state(state_path)
    state.pop(handle, None)
    _write_state(state_path, state)
    return True, reason


# ----------------------------------------------------------------- report


def commit_author_email(repo, commit):
    """The author email recorded in the commit itself.

    This - not the `signature for X` label in git's output - is what binds the
    signature to an identity. That label is the principal *we* supplied through
    allowed_signers, so comparing it against the claim would be circular.
    """
    result = subprocess.run(
        ["git", "-C", repo, "log", "-1", "--format=%ae", commit], capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def commit_message(repo, commit):
    result = subprocess.run(
        ["git", "-C", repo, "log", "-1", "--format=%B", commit], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def nonce_from_message(message, trailer=NONCE_TRAILER):
    """Read the nonce trailer out of a commit message (last occurrence wins)."""
    found = None
    for line in message.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(trailer.lower() + ":"):
            found = stripped.split(":", 1)[1].strip()
    return found


def count_attestations(entry):
    attestations = entry.get("attestations") or []
    return len([a for a in attestations if isinstance(a, dict)])


def evaluate(entry, contributors, repo, commit, state_path, nonce_override=None, now=None, burn=True):
    """Run every check and return a report dict (no printing, so it is testable)."""
    claims = entry.get("claims") or {}
    signed_commit = (entry.get("signing") or {}).get("commit")

    claim_problems = check_claims(claims)
    valid_providers = sorted(p for p in claims if p not in claim_problems)
    min_claims = entry.get("min_claims") or MIN_CLAIMS
    enough = len(valid_providers) >= min_claims

    allowed = None
    if allowed_signers_from([entry]).strip():
        allowed = os.path.join(
            os.path.dirname(state_path) or ".", "allowed_signers_%s" % entry.get("handle", "x")
        )
        os.makedirs(os.path.dirname(allowed) or ".", exist_ok=True)
        with open(allowed, "w", encoding="utf-8") as handle:
            handle.write(allowed_signers_from([entry]))

    signature_ok, signer, signature_detail = verify_signature(repo, commit, allowed)

    bound_email = (claims.get("email") or "").lower()
    author_email = commit_author_email(repo, commit).lower()
    binding_ok = bool(signature_ok and bound_email and author_email and author_email == bound_email)

    message = commit_message(repo, commit)
    nonce_in_commit = nonce_override or nonce_from_message(message)
    nonce_ok, nonce_reason = (False, "no %s: trailer in the commit message" % NONCE_TRAILER)
    if nonce_in_commit:
        nonce_ok, nonce_reason = validate_nonce(entry.get("handle"), nonce_in_commit, state_path, now=now)

    tier = 0
    if signature_ok:
        tier = 1 if (enough and binding_ok and nonce_ok) else 0
    if tier == 1 and count_attestations(entry) > 0:
        tier = 2

    # Spend the nonce only once the round actually succeeded, so a crash or a
    # failed check never forces the contributor to ask for a fresh challenge.
    if burn and tier >= 1 and nonce_ok:
        consume_nonce(entry.get("handle"), nonce_in_commit, state_path, now=now)

    return {
        "handle": entry.get("handle"),
        "valid_claims": valid_providers,
        "claim_problems": claim_problems,
        "min_claims": min_claims,
        "enough_claims": enough,
        "distinct_providers": len(valid_providers),
        "cross_check": cross_check(contributors),
        "signature_ok": signature_ok,
        "signature_detail": signature_detail,
        "signer": signer,
        "author_email": author_email,
        "binding_ok": binding_ok,
        "bound_email": bound_email,
        "nonce_in_commit": nonce_in_commit,
        "nonce_ok": nonce_ok,
        "nonce_reason": nonce_reason,
        "commit_on_record": signed_commit,
        "attestations": count_attestations(entry),
        "tier": tier,
        "tier_name": TIER_NAMES[tier],
    }


OK = "OK"
NO = "!!"


def _line(label, ok, detail):
    return "  %-20s %-3s %s" % (label, OK if ok else NO, detail)


def print_report(report, require_tier):
    print("Proof of Humanity report - @%s" % report["handle"])
    print("=" * 72)
    print(_line("claims", report["enough_claims"], "%s valid (%s); minimum %s" % (
        report["distinct_providers"], ", ".join(report["valid_claims"]) or "none", report["min_claims"])))
    for provider, problems in sorted(report["claim_problems"].items()):
        print(_line("  " + provider, False, "; ".join(problems)))
    print(_line("cross-checks", not report["cross_check"],
                "no duplicate identity or shared key" if not report["cross_check"] else "; ".join(report["cross_check"])))
    print(_line("commit signature", report["signature_ok"], report["signature_detail"]))
    print(_line("signature binding", report["binding_ok"],
                "commit author %s matches the email claim" % report["author_email"] if report["binding_ok"]
                else "commit author %s vs email claim %s" % (report["author_email"] or "none", report["bound_email"] or "none")))
    print(_line("nonce", report["nonce_ok"], report["nonce_reason"]))
    if report["attestations"]:
        print(_line("attestations", True, "%s on record" % report["attestations"]))
    print("-" * 72)
    verdict = "PASS" if report["tier"] >= require_tier else "FAIL"
    print("TIER %s (%s) - required %s -> %s" % (report["tier"], report["tier_name"], require_tier, verdict))
    return report["tier"] >= require_tier


def print_card(entry):
    """The 'more engaging' part of #551: a small, printable contributor card."""
    claims = entry.get("claims") or {}
    handle = entry.get("handle", "?")
    filled = "".join("x" if claims.get(p) else "." for p in PROVIDERS)
    identity = entry.get("name") or handle
    tier = 2 if count_attestations(entry) else (1 if len(claims) >= MIN_CLAIMS else 0)
    print("+----------------------------------------------------------+")
    print("|  qtop proof-of-humanity contributor card                 |")
    print("+----------------------------------------------------------+")
    providers = "providers: " + ", ".join(sorted(claims))
    print("|  %-56s|" % ("@%s" % handle)[:56])
    print("|  %-56s|" % identity[:56])
    print("|  %-56s|" % ("tier %s   claims %s" % (tier, filled)))
    print("|  %-56s|" % providers[:56])
    print("+----------------------------------------------------------+")
    return 0


# ------------------------------------------------------------------- command


def cmd_init(args):
    entry = {
        "handle": args.handle,
        "name": args.name or "",
        "claims": {"github": args.handle, "email": args.email},
        "signing": {"email": args.email, "key": "<paste your public key, e.g. cat ~/.ssh/id_ed25519.pub>"},
        "attestations": [],
    }
    if args.orcid:
        entry["claims"]["orcid"] = args.orcid
    if args.linkedin:
        entry["claims"]["linkedin"] = args.linkedin
    if args.matrix:
        entry["claims"]["matrix"] = args.matrix
    print(yaml.safe_dump(entry, sort_keys=False, allow_unicode=True), end="")
    print("## merge this under `contributors:` in %s" % DEFAULT_CLAIMS, file=sys.stderr)
    return 0


def cmd_challenge(args):
    ttl = parse_duration(args.ttl)
    nonce = issue_nonce(args.handle, args.state, ttl)
    print(nonce)
    print(
        "Send this to @%s and ask for a *signed* commit carrying:\n  %s: %s" % (args.handle, NONCE_TRAILER, nonce),
        file=sys.stderr,
    )
    return 0


def cmd_verify(args):
    contributors, errors = load_claims(args.claims)
    if errors:
        for error in errors:
            print("claims file: %s" % error, file=sys.stderr)
        return 1

    entry = find_contributor(contributors, args.handle)
    if entry is None:
        print("@%s has no entry in %s" % (args.handle, args.claims), file=sys.stderr)
        return 1

    report = evaluate(entry, contributors, args.repo, args.commit, args.state, nonce_override=args.nonce)
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0 if report["tier"] >= args.require_tier else 1
    return 0 if print_report(report, args.require_tier) else 1


def cmd_card(args):
    contributors, errors = load_claims(args.claims)
    if errors:
        for error in errors:
            print("claims file: %s" % error, file=sys.stderr)
        return 1
    entry = find_contributor(contributors, args.handle)
    if entry is None:
        print("@%s has no entry in %s" % (args.handle, args.claims), file=sys.stderr)
        return 1
    return print_card(entry)


def build_parser():
    parser = argparse.ArgumentParser(prog="poh", description="Proof-of-Humanity helper for qtop (issue #551)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="print a claims entry to paste into the claims file")
    init.add_argument("--handle", required=True)
    init.add_argument("--email", required=True)
    init.add_argument("--name", default="")
    init.add_argument("--orcid", default="")
    init.add_argument("--linkedin", default="")
    init.add_argument("--matrix", default="")
    init.set_defaults(func=cmd_init)

    challenge = sub.add_parser("challenge", help="issue a single-use nonce for a handle")
    challenge.add_argument("--handle", required=True)
    challenge.add_argument("--ttl", default=DEFAULT_TTL)
    challenge.add_argument("--state", default=DEFAULT_STATE)
    challenge.set_defaults(func=cmd_challenge)

    verify = sub.add_parser("verify", help="verify claims + signed commit + nonce, then report a tier")
    verify.add_argument("--handle", required=True)
    verify.add_argument("--commit", default="HEAD")
    verify.add_argument("--claims", default=DEFAULT_CLAIMS)
    verify.add_argument("--state", default=DEFAULT_STATE)
    verify.add_argument("--repo", default=".")
    verify.add_argument("--nonce", default=None, help="override the nonce trailer (for tests)")
    verify.add_argument("--require-tier", type=int, default=1, choices=(0, 1, 2))
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)

    card = sub.add_parser("card", help="print the contributor card for a handle")
    card.add_argument("--handle", required=True)
    card.add_argument("--claims", default=DEFAULT_CLAIMS)
    card.set_defaults(func=cmd_card)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as error:
        print("poh: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
