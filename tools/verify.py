# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging>=24",
#     "pyyaml>=6",
# ]
# ///
"""Verify snippets: structure, lint, types, run, and optional tests.

Each snippet is a directory under snippets/ containing README.md (YAML frontmatter plus
required sections) and a PEP 723 script. Directories starting with "_" or "." are skipped.

Usage:
    uv run tools/verify.py                      # verify everything
    uv run tools/verify.py retry-with-tenacity  # verify named snippets
    uv run tools/verify.py --list-json          # emit snippet names as JSON (for CI matrix)
    uv run tools/verify.py --json-out out.json  # also write a machine-readable report
    uv run tools/verify.py --merge status/      # merge per-run reports into status.json

Set UV_RESOLUTION=lowest-direct to check that declared lower bounds actually work.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import InvalidRequirement, Requirement

log = logging.getLogger("verify")

REPO_ROOT = Path(__file__).resolve().parent.parent
SNIPPETS_DIR = REPO_ROOT / "snippets"

REQUIRED_FRONTMATTER = {"title": str, "summary": str, "tags": list}
REQUIRED_SECTIONS = ("Problem", "Why this approach", "Gotchas", "When not to use it")
DEFAULT_TIMEOUT = 60

# Verification tooling. These are deliberately resolved at *highest* whatever UV_RESOLUTION
# says: lowest-direct is for the snippet's dependencies, not for ruff (0.0.13, anyone?).
RUFF = "ruff>=0.16"
MYPY = "mypy>=2.3"
PYTEST = "pytest>=9.1"
TOOL_TIMEOUT = 300  # ceiling for dependency resolution, lint, type-check, tests

# Reference regex from the PEP 723 specification.
PEP723_RE = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
FRONTMATTER_RE = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
PINNED_RE = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;]+)")


class SnippetError(Exception):
    """A snippet failed a check. The message is shown to the author."""


@dataclasses.dataclass
class Step:
    name: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclasses.dataclass
class Result:
    snippet: str
    ok: bool = True
    steps: list[Step] = dataclasses.field(default_factory=list)
    resolved: dict[str, str] = dataclasses.field(default_factory=dict)
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)


def discover(names: list[str] | None = None) -> list[Path]:
    """Return snippet directories, optionally filtered by name."""
    if not SNIPPETS_DIR.is_dir():
        raise SystemExit(f"no snippets directory at {SNIPPETS_DIR}")
    found = sorted(
        p
        for p in SNIPPETS_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "README.md").is_file()
    )
    if not names:
        return found
    by_name = {p.name: p for p in found}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise SystemExit(f"unknown snippet(s): {', '.join(missing)}")
    return [by_name[n] for n in names]


def parse_frontmatter(readme: Path) -> tuple[dict[str, Any], str]:
    """Split README into (frontmatter dict, markdown body) and validate the frontmatter."""
    text = readme.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise SnippetError("README.md must start with a '---' YAML frontmatter block")
    try:
        meta = yaml.safe_load(match.group("body")) or {}
    except yaml.YAMLError as exc:
        raise SnippetError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SnippetError("frontmatter must be a mapping")
    for key, kind in REQUIRED_FRONTMATTER.items():
        if not isinstance(meta.get(key), kind) or not meta[key]:
            raise SnippetError(f"frontmatter '{key}' must be a non-empty {kind.__name__}")
    timeout = meta.setdefault("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise SnippetError("frontmatter 'timeout' must be an integer between 1 and 600")
    meta.setdefault("network", False)
    meta.setdefault("entrypoint", "main.py")
    entry = str(meta["entrypoint"])
    # Keep the entrypoint inside the snippet directory.
    if Path(entry).is_absolute() or ".." in Path(entry).parts:
        raise SnippetError("frontmatter 'entrypoint' must be a relative path inside the snippet")
    return meta, text[match.end() :]


def check_sections(body: str) -> None:
    """Ensure every required '## Heading' is present."""
    headings = {m.group(1).strip() for m in re.finditer(r"(?m)^##\s+(.+)$", body)}
    missing = [s for s in REQUIRED_SECTIONS if s not in headings]
    if missing:
        raise SnippetError(f"README.md is missing section(s): {', '.join(missing)}")


def parse_script_metadata(script: Path) -> dict[str, Any]:
    """Extract and validate the PEP 723 'script' block."""
    blocks = [m for m in PEP723_RE.finditer(script.read_text(encoding="utf-8")) if m.group("type") == "script"]
    if len(blocks) != 1:
        raise SnippetError(f"{script.name} must contain exactly one '# /// script' block (found {len(blocks)})")
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:] for line in blocks[0].group("content").splitlines(keepends=True)
    )
    try:
        meta = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise SnippetError(f"script metadata is not valid TOML: {exc}") from exc
    if "requires-python" not in meta:
        raise SnippetError("script metadata must declare requires-python")
    deps = meta.get("dependencies")
    if not isinstance(deps, list):
        raise SnippetError("script metadata must declare a dependencies list (empty is fine)")
    for dep in deps:
        try:
            req = Requirement(dep)
        except InvalidRequirement as exc:
            raise SnippetError(f"invalid dependency {dep!r}: {exc}") from exc
        # A lower bound is what makes lowest-direct CI meaningful and tells readers what's tested.
        if not any(spec.operator in (">=", "~=", "==") for spec in req.specifier):
            raise SnippetError(f"dependency {dep!r} needs a lower bound (>=, ~=, or ==)")
    return meta


def run(cmd: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command and capture output, converting a timeout into a failed result."""
    log.debug("running: %s (cwd=%s)", " ".join(cmd), cwd)
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return subprocess.CompletedProcess(cmd, returncode=124, stdout=out, stderr=f"timed out after {timeout}s")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=str(exc))


def step(
    result: Result,
    name: str,
    proc: subprocess.CompletedProcess[str] | None = None,
    error: str | None = None,
    started: float = 0.0,
) -> bool:
    """Record a step outcome on the result."""
    elapsed = round(time.monotonic() - started, 2)
    if proc is not None:
        ok = proc.returncode == 0
        detail = "" if ok else (proc.stdout + proc.stderr).strip()[-4000:]
    else:
        ok, detail = error is None, error or ""
    result.steps.append(Step(name, ok, elapsed, detail))
    result.ok &= ok
    log.log(
        logging.INFO if ok else logging.ERROR,
        "  %-10s %s (%.2fs)%s",
        name,
        "ok" if ok else "FAIL",
        elapsed,
        f"\n{detail}" if detail else "",
    )
    return ok


def verify(snippet: Path, uv: str) -> Result:
    """Run every check against one snippet. Later steps are skipped once structure fails."""
    result = Result(snippet.name)
    log.info("%s", snippet.name)

    # Structure.
    t = time.monotonic()
    try:
        meta, body = parse_frontmatter(snippet / "README.md")
        check_sections(body)
        script = snippet / meta["entrypoint"]
        if not script.is_file():
            raise SnippetError(f"entrypoint {meta['entrypoint']} not found")
        script_meta = parse_script_metadata(script)
        result.meta = {k: meta[k] for k in ("title", "summary", "tags", "libraries", "network") if k in meta}
    except (SnippetError, OSError, UnicodeDecodeError) as exc:
        step(result, "structure", error=str(exc), started=t)
        return result
    step(result, "structure", started=t)

    # Environment for tooling: identical except the resolution strategy is left at default.
    tool_env = {k: v for k, v in os.environ.items() if k != "UV_RESOLUTION"}
    ruff = [uv, "tool", "run", "--from", RUFF, "ruff"]

    # Lint and format across the whole snippet directory (tests included).
    t = time.monotonic()
    step(result, "ruff", run([*ruff, "check", "."], snippet, TOOL_TIMEOUT, tool_env), started=t)
    t = time.monotonic()
    step(result, "format", run([*ruff, "format", "--check", "."], snippet, TOOL_TIMEOUT, tool_env), started=t)

    with tempfile.TemporaryDirectory(prefix=f"verify-{snippet.name}-") as tmp:
        # Resolve the script's dependencies once. This honours UV_RESOLUTION, so the same
        # set is used for type checks and tests, and it records exactly what was verified.
        reqs = Path(tmp) / "requirements.txt"
        t = time.monotonic()
        export = [uv, "export", "--script", script.name, "--no-hashes", "--no-header", "-o", str(reqs)]
        proc = run(export, snippet, TOOL_TIMEOUT)
        if not step(result, "resolve", proc, started=t):
            return result
        for line in reqs.read_text(encoding="utf-8").splitlines():
            if m := PINNED_RE.match(line.strip()):
                result.resolved[m.group(1).lower()] = m.group(2)

        # Same interpreter constraint as the script itself, or mypy/pytest may run on an older Python.
        python = str(script_meta["requires-python"])
        with_env = [uv, "run", "--isolated", "--no-project", "--python", python, "--with-requirements", str(reqs)]

        # requirements.txt is fully pinned, so dropping UV_RESOLUTION here only affects the tools.
        has_tests = any(snippet.glob("test_*.py"))
        type_cmd = [*with_env, "--with", MYPY, *(["--with", PYTEST] if has_tests else [])]
        type_cmd += ["--", "mypy", "--config-file", str(REPO_ROOT / "pyproject.toml"), "--strict", "."]
        t = time.monotonic()
        step(result, "mypy", run(type_cmd, snippet, TOOL_TIMEOUT, tool_env), started=t)

        # The actual demo. SNIPPET_CI lets a script shorten sleeps or skip interactive bits.
        env = {**os.environ, "SNIPPET_CI": "1", "PYTHONUNBUFFERED": "1"}
        t = time.monotonic()
        step(result, "run", run([uv, "run", "--script", script.name], snippet, int(meta["timeout"]), env), started=t)

        if has_tests:
            t = time.monotonic()
            test_cmd = [*with_env, "--with", PYTEST, "--", "pytest", "-q", "-p", "no:cacheprovider"]
            step(result, "tests", run(test_cmd, snippet, TOOL_TIMEOUT, tool_env), started=t)

    return result


def merge_reports(report_dir: Path) -> dict[str, Any]:
    """Merge per-run --json-out reports into the site-facing status, keyed by snippet.

    Each snippet gets its frontmatter subset plus one entry per resolution, so the site
    can say "verified <date> with tenacity 9.1.4" and show the proven lower bounds.
    """
    merged: dict[str, Any] = {}
    for f in sorted(report_dir.glob("*.json")):
        report = json.loads(f.read_text(encoding="utf-8"))
        for r in report["results"]:
            entry = merged.setdefault(r["snippet"], {"meta": r["meta"], "runs": {}})
            entry["runs"][report["resolution"]] = {
                "ok": r["ok"],
                "verified": report["generated"],
                "python": report["python"],
                "uv": report["uv"],
                "resolved": r["resolved"],
                "failed_steps": [s["name"] for s in r["steps"] if not s["ok"]],
            }
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("snippets", nargs="*", help="snippet names (default: all)")
    parser.add_argument("--list-json", action="store_true", help="print snippet names as a JSON array and exit")
    parser.add_argument("--json-out", type=Path, help="write a JSON report here")
    parser.add_argument("--merge", type=Path, metavar="DIR", help="merge DIR/*.json reports into status.json and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    if args.merge:
        out = args.json_out or REPO_ROOT / "status.json"
        out.write_text(json.dumps(merge_reports(args.merge), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log.info("wrote %s", out)
        return 0

    snippets = discover(args.snippets)
    if args.list_json:
        print(json.dumps([p.name for p in snippets]))
        return 0

    uv = shutil.which("uv")
    if uv is None:
        log.error("uv not found on PATH")
        return 2

    uv_version = run([uv, "--version"], REPO_ROOT, 30).stdout.strip()
    resolution = os.environ.get("UV_RESOLUTION", "highest")
    log.info("%s, resolution=%s, %d snippet(s)\n", uv_version, resolution, len(snippets))

    results: list[Result] = []
    for snippet in snippets:
        try:
            results.append(verify(snippet, uv))
        except Exception:  # A verifier bug shouldn't hide results for other snippets.
            log.exception("verifier crashed on %s", snippet.name)
            results.append(
                Result(snippet.name, ok=False, steps=[Step("verifier", False, 0.0, "internal error, see log")])
            )

    if args.json_out:
        report = {
            "generated": datetime.now(UTC).isoformat(timespec="seconds"),
            "uv": uv_version,
            "resolution": resolution,
            "python": sys.version.split()[0],
            "results": [dataclasses.asdict(r) for r in results],
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    failed = [r.snippet for r in results if not r.ok]
    log.info(
        "\n%d/%d passed%s", len(results) - len(failed), len(results), f"; failed: {', '.join(failed)}" if failed else ""
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
