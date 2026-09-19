# PR #1 ka CI fix apne naam se push karne ke steps

Bot `.github/workflows/*` push nahi kar sakta, aur aap chahte ho ki commit
`shloksharmawork` ke naam se jaaye. Isliye change ek patch file me hai:
**`poh-ci-fix.patch`** (repo root me).

Patch `feat/poh-551` ke head (`7e4d4b8`) pe **clean apply** hota hai — verify kiya gaya hai.

---

## Option A — patch apply karo (recommended)

Apne local qtop clone me:

```bash
# 1. apni PR branch pe jao
git checkout feat/poh-551
git pull origin feat/poh-551

# 2. patch lo (is repo ke arena branch se) aur apply karo
git fetch origin arena/01a0b96a-qtop
git show origin/arena/01a0b96a-qtop:poh-ci-fix.patch > /tmp/poh-ci-fix.patch
git am /tmp/poh-ci-fix.patch
```

`git am` commit message + authorship dono set kar dega (author already
`shloksharmawork` hai).

Agar `git am` ki jagah sirf changes chahiye (apna message likhna ho):

```bash
git apply /tmp/poh-ci-fix.patch
git add -A
git commit -m "fix(poh): drop the PyYAML dependency that broke CI"
```

## Option B — cherry-pick

```bash
git checkout feat/poh-551
git fetch origin arena/01a0b96a-qtop
git cherry-pick 1c2c9b95ab0bc2b12262ef4bb6d588e707f6e8fd
```

Ye bhi clean chalta hai (test kiya gaya). Commit author already aap ho,
bas `Co-authored-by: arena-agent` trailer chahiye to `--edit` se hata dena.

---

## 3. Workflow file ka manual edit (ye patch me NAHI hai)

`.github/workflows/poh.yml` se ye 3 lines hata do — ab pyyaml ki zaroorat nahi:

```yaml
      - name: Install helper dependency
        run: python -m pip install --quiet pyyaml
```

Optional, uske jagah ek comment:

```yaml
      # tools/poh.py is stdlib-only on purpose (CONTRIBUTING.md: avoid
      # dependencies), so this job installs nothing.
```

Bot is file ko chhu nahi sakta (GitHub App ke paas `workflows` permission nahi),
isliye ye aapko karna hai. Note: ye edit optional hai CI green karne ke liye —
`poh` job pyyaml install karke bhi pass hota hai. Par ab wo step bekaar hai.

---

## 4. Verify aur push

```bash
make ci-deps PYTHON=python3     # pinned CI deps, pyyaml ke bina
make github-ci PYTHON=python3   # -> 272 passed
make coverage-xml PYTHON=python3
make compat-py36 PYTHON=python3

git push origin feat/poh-551
```

## 5. Cleanup

- PR #2 (bot wala) close kar dena — uska poora content ab PR #1 me aa jayega.
- `poh-ci-fix.patch` aur `APPLY-POH-CI-FIX.md` ko `feat/poh-551` pe commit
  **mat** karna, ye sirf transfer ke liye hain.

---

## Patch me kya hai

| file | change |
| --- | --- |
| `tools/poh.py` | PyYAML import hataya; stdlib-only `yaml_load` / `yaml_dump` + `YamlError` add kiye; `ruff format` ke hisaab se reformat |
| `tests/test_poh.py` | `import yaml` hataya, `poh.yaml_dump` use kiya; yaml subset ke 8 naye tests (round-trip, rejections, real `poh/claims.yaml` ka parse) |
| `docs/proof-of-humanity.md` | "No dependencies" section |
| `poh/README.md` | note ki claims.yaml stdlib se parse hoti hai, block style hi likho |

Verified: `make github-ci` -> 272 passed, sample gate 10/10, ruff + fortifications +
format-check ok. Real GitHub Actions pe saare 6 checks green (PR #2 dekh lo).
