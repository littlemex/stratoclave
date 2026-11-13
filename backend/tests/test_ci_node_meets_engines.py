"""Every CI job that installs a Node project fails at install time, with the package
named, when its Node is older than a dependency needs.

A dependency bump raised `vitest` and `jsdom` to releases whose `engines.node`
excludes Node 20 while the workflow still asked for Node 20. `npm ci` only warns on an
engines mismatch, so the install passed and the test runner crashed inside a
transitive dependency, which read as a broken test rather than a wrong runtime.

The check is npm's own: each Node project sets `engine-strict=true` in its `.npmrc`,
so `npm ci` refuses a Node that any package in the lockfile does not accept, and names
that package. These tests keep the pieces of that in place for every job that runs
npm: it sets up Node itself, from the one version file `.nvmrc` with `check-latest:
true` (so it runs that major's newest release, not whatever the runner cached), the
project it installs has the `.npmrc`, and nothing in the job turns the check off.
`engines.node` in each package.json is required to be present so a contributor can
see the floor; the enforcement is engine-strict on every lockfile entry, not that
field.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW_FILES = sorted((REPO / ".github" / "workflows").glob("*.y*ml"))
NVMRC = REPO / ".nvmrc"


def _jobs():
    for wf in WORKFLOW_FILES:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            yield wf.name, job_id, job


def _workdir(job: dict) -> str:
    return ((job.get("defaults") or {}).get("run") or {}).get("working-directory", ".")


def _runs_npm(job: dict) -> bool:
    return any(re.search(r"(^|\s)(npm|npx)\s", str(s.get("run", "")))
               for s in job.get("steps") or [])


def _setup_node(job: dict):
    return [s for s in job.get("steps") or []
            if str(s.get("uses", "")).startswith("actions/setup-node")]


def test_every_job_that_runs_npm_sets_up_node_itself():
    """A job that runs npm on the runner image's default Node is outside every check
    below, so it must not exist."""
    bad = [f"{wf}:{job_id}" for wf, job_id, job in _jobs()
           if _runs_npm(job) and not _setup_node(job)]
    assert not bad, f"jobs run npm without actions/setup-node: {bad}"


def test_the_workflows_were_found_and_some_job_installs_node_packages():
    """Guards the tests below against passing on an empty glob."""
    assert WORKFLOW_FILES, f"no workflows under {REPO}"
    assert any(_setup_node(job) and _runs_npm(job) for _, _, job in _jobs()), (
        "no job sets up Node and runs npm; the parser no longer matches the workflows")


def test_every_npm_job_takes_node_from_nvmrc_with_check_latest():
    assert NVMRC.is_file() and re.fullmatch(r"\d+\s*", NVMRC.read_text(encoding="utf-8")), (
        ".nvmrc must hold the one Node major CI uses")
    bad = []
    for wf, job_id, job in _jobs():
        if not _runs_npm(job):
            continue
        for step in _setup_node(job):
            w = step.get("with") or {}
            if w.get("node-version-file") != ".nvmrc" or "node-version" in w or w.get("check-latest") is not True:
                bad.append(f"{wf}:{job_id}")
    assert not bad, (
        "setup-node steps in npm jobs must use `node-version-file: .nvmrc` with "
        f"`check-latest: true`, so one file decides the version and the newest release of it runs: {bad}")


def test_no_job_turns_the_engines_check_off():
    bad = []
    for wf, job_id, job in _jobs():
        texts = [str(s.get("run", "")) for s in job.get("steps") or []]
        texts += [str(v) for v in (job.get("env") or {}).values()]
        texts += [f"{k}={v}" for s in job.get("steps") or [] for k, v in (s.get("env") or {}).items()]
        if any(re.search(r"engine-strict|ENGINE_STRICT|npm\s+(ci|install)\b[^\n]*--force", t, flags=re.I)
               for t in texts):
            bad.append(f"{wf}:{job_id}")
    assert not bad, f"jobs override engine-strict or force an install: {bad}"


def test_every_project_a_job_installs_refuses_an_unsupported_node():
    problems = []
    for wf, job_id, job in _jobs():
        if not _setup_node(job) or not _runs_npm(job):
            continue
        project = REPO / _workdir(job)
        pkg = project / "package.json"
        if not pkg.is_file():
            problems.append(f"{wf}:{job_id}: no package.json in {project.relative_to(REPO)}")
            continue
        npmrc = project / ".npmrc"
        strict = npmrc.is_file() and re.search(
            r"^\s*engine-strict\s*=\s*true\s*$", npmrc.read_text(encoding="utf-8"), flags=re.M)
        if not strict:
            problems.append(f"{wf}:{job_id}: {project.relative_to(REPO)}/.npmrc does not set "
                            "engine-strict=true, so npm ci only warns on an engines mismatch")
        engines = (json.loads(pkg.read_text(encoding="utf-8")).get("engines") or {}).get("node")
        if not engines:
            problems.append(f"{wf}:{job_id}: {pkg.relative_to(REPO)} declares no engines.node")
    assert not problems, "\n  ".join([""] + problems)
