# Field notes

Small, runnable demonstrations of a library or practice. Each one is a directory holding
a README, a script, and any assets it needs. Every script runs with a single command:

```sh
cd snippets/retry-with-tenacity
uv run main.py
```

There's no virtualenv or install step. Dependencies are declared inline
([PEP 723](https://peps.python.org/pep-0723/)) and uv resolves them on the fly.

## Layout

```
snippets/
  _template/             # copy this to start a new one (skipped by the verifier)
  retry-with-tenacity/
    README.md            # YAML frontmatter + fixed sections; becomes the site page
    main.py              # PEP 723 script, the entrypoint
    test_main.py         # optional
    assets/              # optional: images, sample data
tools/verify.py          # the checker, itself a PEP 723 script
pyproject.toml           # ruff and mypy config only; no [project], no lockfile
.github/workflows/verify.yml
```

## Adding a snippet

```sh
cp -r snippets/_template snippets/my-thing
uv add --script snippets/my-thing/main.py 'somelib>=1.2'
uv run tools/verify.py my-thing
```

## The contract

The verifier enforces all of this, so it can't drift.

**README.md** starts with frontmatter (`title`, `summary`, `tags`, and `published` are required;
`libraries`, `entrypoint`, `timeout`, and `network` are optional) and contains these sections:
Problem, Why this approach, Gotchas, When not to use it. It can have other sections too.

**The script** has exactly one PEP 723 block declaring `requires-python`, and every
dependency has a lower bound. It exits 0 on success, needs no input, and finishes within
`timeout`. Prefer offline demos (mock transports, fixtures in `assets/`), and set
`network: true` when that isn't possible. `SNIPPET_CI=1` is set under verification if a
script wants to shorten sleeps.

**Checks, in order:** structure → `ruff check` → `ruff format --check` → resolve →
`mypy --strict` → run → `pytest` (if `test_*.py` exists).

## Why two resolutions

CI runs every snippet twice:

- `highest` catches rot, meaning a new release broke it.
- `lowest-direct` checks that the lower bounds you've declared are true. Without it,
  `httpx>=0.27` is just a claim.

Locally: `uv run tools/verify.py --resolution lowest-direct my-thing`. The strategy is a flag
rather than `UV_RESOLUTION` in the environment on purpose: set in the environment it would
also apply to the verifier's own dependencies and install versions that don't build any more.

Scripts are deliberately not locked. A lock would make the rot check meaningless. Use
`uv lock --script` locally if you want reproducibility while writing one.

## Status for the site

Every full run (the weekly schedule, or a manual one) merges its results into `status.json`,
keyed by snippet. For each snippet it records pass/fail per resolution, a timestamp, the
Python and uv versions, and the exact versions resolved, which is what lets the site render
"verified 2026-09-28 with tenacity 9.1.4".

The run publishes it to the `status` branch, next to `commit.txt` naming the commit that was
verified. The site's content bundle reads both from there, so it needs no credentials:

```sh
curl -fsSLO https://raw.githubusercontent.com/Darkflib/field-notes/status/status.json
```

The same file is also uploaded as the run's `status` artifact. Failures open or update a
single `snippet-rot` issue.
