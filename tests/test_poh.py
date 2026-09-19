"""Tests for the proof-of-humanity helper (tools/poh.py, issue #551)."""

import datetime
import json
import os
import shutil
import subprocess

import pytest

from tools import poh

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAS_TOOLS = bool(shutil.which("git")) and bool(shutil.which("ssh-keygen"))
needs_signing = pytest.mark.skipif(not HAS_TOOLS, reason="git and ssh-keygen are required")


## --------------------------------------------------------------- claim formats


@pytest.mark.parametrize(
    "value, expected",
    (
        ("0000-0002-1825-0097", True),
        ("0000-0001-5109-3700", True),
        ("0000-0002-1825-0098", False),  # wrong check digit
        ("0000-0002-1825-0097-", False),
        ("0000000218250097", False),
        ("", False),
    ),
)
def test_orcid_checksum(value, expected):
    assert poh.orcid_is_valid(value) is expected


@pytest.mark.parametrize(
    "value, expected",
    (
        ("alice@example.org", True),
        ("a.b+tag@sub.example.co.in", True),
        ("alice@mailinator.com", False),  # disposable inbox
        ("alice@YOPMAIL.com", False),
        ("not-an-email", False),
        ("a@b", False),
    ),
)
def test_email_validation(value, expected):
    assert poh.email_is_valid(value) is expected


@pytest.mark.parametrize(
    "value, expected",
    (
        ("https://www.linkedin.com/in/example-contributor", True),
        ("https://linkedin.com/in/ab", False),
        ("https://www.linkedin.com/company/foo", False),
        ("ftp://linkedin.com/in/foo", False),
    ),
)
def test_linkedin_validation(value, expected):
    assert poh.linkedin_is_valid(value) is expected


@pytest.mark.parametrize(
    "value, expected",
    (
        ("@alice:matrix.org", True),
        ("@alice:matrix.org:8448", True),
        ("alice@matrix.org", False),
        ("@alice", False),
    ),
)
def test_matrix_validation(value, expected):
    assert poh.matrix_is_valid(value) is expected


@pytest.mark.parametrize(
    "value, expected",
    (
        ("alice", True),
        ("alice-bob", True),
        ("-alice", False),
        ("alice-", False),
        ("a" * 40, False),
    ),
)
def test_github_validation(value, expected):
    assert poh.github_is_valid(value) is expected


def test_check_claims_reports_unknown_provider_and_bad_values():
    problems = poh.check_claims({"github": "alice", "mastodon": "@a@b.c", "orcid": "nope"})
    assert "mastodon" in problems
    assert "orcid" in problems
    assert "github" not in problems


## ------------------------------------------------------------ yaml subset
##
## tools/poh.py carries its own reader so the PoH round needs no third-party
## parser (CONTRIBUTING.md: avoid dependencies). These tests pin the subset.


def test_yaml_load_reads_the_committed_claims_file():
    contributors, errors = poh.load_claims(os.path.join(REPO_ROOT, "poh", "claims.yaml"))
    assert errors == []
    assert [entry["handle"] for entry in contributors] == [
        "qtop-maintainer-example",
        "qtop-contributor-example",
    ]
    assert contributors[0]["claims"]["orcid"] == "0000-0002-1825-0097"
    assert contributors[0]["attestations"][0]["type"] == "live-demo"
    assert contributors[1]["attestations"] == []


def test_yaml_load_handles_nesting_comments_and_scalars():
    text = "\n".join(
        (
            "# a comment",
            "version: 1",
            "enabled: true",
            "missing: null",
            'quoted: "@example:matrix.org"',
            "contributors:",
            "  - handle: alice",
            "    claims:",
            "      email: alice@example.org",
            "    attestations: []",
            "  - handle: bob",
            "",
        )
    )
    data = poh.yaml_load(text)
    assert data["version"] == 1
    assert data["enabled"] is True
    assert data["missing"] is None
    assert data["quoted"] == "@example:matrix.org"
    assert data["contributors"][0]["claims"]["email"] == "alice@example.org"
    assert data["contributors"][0]["attestations"] == []
    assert data["contributors"][1] == {"handle": "bob"}


@pytest.mark.parametrize(
    "text",
    (
        "contributors: [oops\n",
        "version: 1\ncontributors: {broken\n",
        'name: "unterminated\n',
        "version: 1\n  stray-indent: 2\n",
    ),
)
def test_yaml_load_rejects_unsupported_or_broken_input(text):
    with pytest.raises(poh.YamlError):
        poh.yaml_load(text)


def test_yaml_dump_round_trips_through_yaml_load():
    entry = {
        "version": 1,
        "contributors": [
            {
                "handle": "alice",
                "name": "Alice",
                "claims": {"github": "alice", "email": "alice@example.org"},
                "signing": {"email": "alice@example.org", "key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBm6Zm1n a@b"},
                "attestations": [],
            }
        ],
    }
    assert poh.yaml_load(poh.yaml_dump(entry)) == entry


def test_yaml_load_of_an_empty_document_is_none():
    assert poh.yaml_load("# only a comment\n\n") is None


## ---------------------------------------------------------------- claims file


def _claims_file(tmp_path, contributors):
    path = tmp_path / "claims.yaml"
    path.write_text(poh.yaml_dump({"version": 1, "contributors": contributors}), encoding="utf-8")
    return path


def example_entry(handle="alice", email="alice@example.org", **claims):
    base = {"github": handle, "email": email, "orcid": "0000-0002-1825-0097"}
    base.update(claims)
    return {
        "handle": handle,
        "name": "Alice",
        "claims": base,
        "signing": {"email": email, "key": "<paste here>"},
        "attestations": [],
    }


def test_load_claims_accepts_a_minimal_file(tmp_path):
    path = _claims_file(tmp_path, [example_entry()])
    contributors, errors = poh.load_claims(str(path))
    assert errors == []
    assert contributors[0]["handle"] == "alice"


def test_load_claims_rejects_missing_file(tmp_path):
    contributors, errors = poh.load_claims(str(tmp_path / "nope.yaml"))
    assert contributors == []
    assert "not found" in errors[0]


def test_load_claims_rejects_bad_yaml(tmp_path):
    path = tmp_path / "claims.yaml"
    path.write_text("version: 1\ncontributors: [oops\n", encoding="utf-8")
    _, errors = poh.load_claims(str(path))
    assert errors and "not valid yaml" in errors[0]


def test_load_claims_rejects_wrong_version(tmp_path):
    path = tmp_path / "claims.yaml"
    path.write_text(poh.yaml_dump({"version": 2, "contributors": [example_entry()]}), encoding="utf-8")
    _, errors = poh.load_claims(str(path))
    assert any("version" in error for error in errors)


def test_load_claims_rejects_invalid_handle(tmp_path):
    entry = example_entry()
    entry["handle"] = "-bad-"
    path = _claims_file(tmp_path, [entry])
    _, errors = poh.load_claims(str(path))
    assert any("handle" in error for error in errors)


def test_cross_check_flags_duplicate_identity():
    first = example_entry("alice", "alice@example.org")
    second = example_entry("bob", "alice@example.org")
    errors = poh.cross_check([first, second])
    assert any("email" in error and "alice@example.org" in error for error in errors)


def test_cross_check_flags_shared_signing_key():
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBm6Zm1n example@host"
    first = example_entry("alice")
    first["signing"]["key"] = key
    second = example_entry("bob", "bob@example.org")
    second["signing"]["key"] = key
    errors = poh.cross_check([first, second])
    assert any("signing key" in error for error in errors)


def test_cross_check_ignores_unmaterialised_placeholders():
    first = example_entry("alice")
    second = example_entry("bob", "bob@example.org")
    second["claims"]["orcid"] = "0000-0001-5109-3700"  # a distinct identity
    assert poh.cross_check([first, second]) == []


## ------------------------------------------------------------------- duration


@pytest.mark.parametrize("value, seconds", (("30s", 30), ("10m", 600), ("2h", 7200)))
def test_parse_duration(value, seconds):
    assert poh.parse_duration(value) == seconds


@pytest.mark.parametrize("value", ("", "m", "10", "10x", "1.5m"))
def test_parse_duration_rejects_junk(value):
    with pytest.raises(ValueError):
        poh.parse_duration(value)


## ---------------------------------------------------------------------- nonce


def test_nonce_lifecycle_is_single_use(tmp_path):
    state = str(tmp_path / "nonces.json")
    nonce = poh.issue_nonce("alice", state, 600)

    assert poh.validate_nonce("alice", nonce, state)[0] is True
    assert poh.consume_nonce("alice", nonce, state)[0] is True
    ok, reason = poh.validate_nonce("alice", nonce, state)
    assert ok is False and "no nonce was issued" in reason


def test_nonce_rejects_wrong_value(tmp_path):
    state = str(tmp_path / "nonces.json")
    poh.issue_nonce("alice", state, 600)
    ok, reason = poh.validate_nonce("alice", "qp-not-the-one", state)
    assert ok is False and "does not match" in reason


def test_nonce_expires(tmp_path):
    state = str(tmp_path / "nonces.json")
    issued = datetime.datetime(2026, 9, 19, 12, 0, tzinfo=datetime.timezone.utc)
    nonce = poh.issue_nonce("alice", state, 60, now=issued)
    ok, reason = poh.validate_nonce("alice", nonce, state, now=issued + datetime.timedelta(seconds=120))
    assert ok is False and "expired" in reason


def test_validate_nonce_does_not_spend_it(tmp_path):
    state = str(tmp_path / "nonces.json")
    nonce = poh.issue_nonce("alice", state, 600)
    for _ in range(3):
        assert poh.validate_nonce("alice", nonce, state)[0] is True


## ------------------------------------------------------------ commit messages


def test_nonce_from_message_reads_the_trailer():
    message = "fix: something\n\nProof-of-Humanity-Nonce: qp-abc123\n"
    assert poh.nonce_from_message(message) == "qp-abc123"


def test_nonce_from_message_last_one_wins_and_absent_is_none():
    message = "Proof-of-Humanity-Nonce: first\nProof-of-Humanity-Nonce: second\n"
    assert poh.nonce_from_message(message) == "second"
    assert poh.nonce_from_message("no trailer here") is None


## ------------------------------------------------------------ allowed_signers


def test_allowed_signers_from_uses_only_real_keys():
    entry = example_entry()
    entry["signing"]["key"] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBm6Zm1n user@host"
    text = poh.allowed_signers_from([entry])
    assert text.strip() == "alice@example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBm6Zm1n user@host"
    assert poh.allowed_signers_from([example_entry()]).strip() == ""


## ----------------------------------------------------------- end to end round


def signed_repo(tmp_path, nonce=None, sign=True, email="alice@example.org", signer_name="Alice"):
    key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "poh-test", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    public = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", email)
    git("config", "user.name", signer_name)
    git("config", "gpg.format", "ssh")
    git("config", "user.signingkey", str(key))
    (repo / "work.txt").write_text("hello\n", encoding="utf-8")
    git("add", "work.txt")
    message = "feat: a contribution"
    if nonce:
        message += "\n\n%s: %s" % (poh.NONCE_TRAILER, nonce)
    git("commit", "-q", "-S" if sign else "--no-gpg-sign", "-m", message)
    return repo, public


@needs_signing
def test_full_round_reaches_tier_1(tmp_path):
    state = str(tmp_path / "nonces.json")
    nonce = poh.issue_nonce("alice", state, 600)
    repo, public = signed_repo(tmp_path, nonce=nonce)

    entry = example_entry(linkedin="https://www.linkedin.com/in/alice", matrix="@alice:matrix.org")
    entry["signing"]["key"] = public
    contributors = [entry]

    report = poh.evaluate(entry, contributors, str(repo), "HEAD", state)

    assert report["signature_ok"] is True
    assert report["binding_ok"] is True
    assert report["nonce_ok"] is True
    assert report["distinct_providers"] == 5
    assert report["tier"] == 1
    # the nonce is spent by a successful round
    assert poh.validate_nonce("alice", nonce, state)[0] is False


@needs_signing
def test_unsigned_commit_stays_at_tier_0(tmp_path):
    state = str(tmp_path / "nonces.json")
    nonce = poh.issue_nonce("alice", state, 600)
    repo, public = signed_repo(tmp_path, nonce=nonce, sign=False)

    entry = example_entry()
    entry["signing"]["key"] = public

    report = poh.evaluate(entry, [entry], str(repo), "HEAD", state)

    assert report["signature_ok"] is False
    assert report["tier"] == 0


@needs_signing
def test_signature_must_match_the_email_claim(tmp_path):
    state = str(tmp_path / "nonces.json")
    nonce = poh.issue_nonce("alice", state, 600)
    repo, public = signed_repo(tmp_path, nonce=nonce, email="someone-else@example.org")

    entry = example_entry()  # claims alice@example.org
    entry["signing"]["key"] = public

    report = poh.evaluate(entry, [entry], str(repo), "HEAD", state)

    assert report["signature_ok"] is True
    assert report["binding_ok"] is False
    assert report["tier"] == 0


@needs_signing
def test_missing_nonce_trailer_blocks_tier_1(tmp_path):
    state = str(tmp_path / "nonces.json")
    poh.issue_nonce("alice", state, 600)
    repo, public = signed_repo(tmp_path, nonce=None)

    entry = example_entry()
    entry["signing"]["key"] = public

    report = poh.evaluate(entry, [entry], str(repo), "HEAD", state)

    assert report["signature_ok"] is True
    assert report["nonce_ok"] is False
    assert "no Proof-of-Humanity-Nonce" in report["nonce_reason"]
    assert report["tier"] == 0


@needs_signing
def test_attestation_promotes_to_tier_2(tmp_path):
    state = str(tmp_path / "nonces.json")
    nonce = poh.issue_nonce("alice", state, 600)
    repo, public = signed_repo(tmp_path, nonce=nonce)

    entry = example_entry()
    entry["signing"]["key"] = public
    entry["attestations"] = [{"type": "live-demo", "by": "maintainer", "date": "2026-09-19"}]

    report = poh.evaluate(entry, [entry], str(repo), "HEAD", state)

    assert report["tier"] == 2


def test_failed_checks_do_not_burn_the_nonce(tmp_path):
    """A crash or a failed check must not cost the contributor a new challenge."""
    state = str(tmp_path / "nonces.json")
    nonce = poh.issue_nonce("alice", state, 600)

    entry = example_entry()
    report = poh.evaluate(entry, [entry], str(tmp_path), "HEAD", state, nonce_override=nonce)

    assert report["signature_ok"] is False
    assert report["tier"] == 0
    assert poh.validate_nonce("alice", nonce, state)[0] is True


## ---------------------------------------------------------------------- CLI


def test_cli_card_prints_a_card(tmp_path, capsys):
    path = _claims_file(tmp_path, [example_entry()])
    assert poh.main(["card", "--handle", "alice", "--claims", str(path)]) == 0
    assert "@alice" in capsys.readouterr().out


def test_cli_verify_missing_handle_exits_1(tmp_path, capsys):
    path = _claims_file(tmp_path, [example_entry()])
    assert poh.main(["verify", "--handle", "nobody", "--claims", str(path), "--state", str(tmp_path / "n.json")]) == 1
    assert "no entry" in capsys.readouterr().err


def test_cli_challenge_prints_a_nonce(tmp_path, capsys):
    state = str(tmp_path / "nonces.json")
    assert poh.main(["challenge", "--handle", "alice", "--state", state]) == 0
    nonce = capsys.readouterr().out.strip()
    assert nonce.startswith("qp-")
    assert json.load(open(state))["alice"]["nonce"] == nonce
