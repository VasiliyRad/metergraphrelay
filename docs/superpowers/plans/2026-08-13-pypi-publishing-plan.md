# PyPI Publishing Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish `metergraphrelay` to PyPI through Trusted Publishing whenever a GitHub Release is published.

**Architecture:** One GitHub Actions workflow builds the source distribution and wheel, then publishes them with GitHub OIDC through the configured `pypi` environment. No PyPI token is stored in GitHub.

**Tech Stack:** GitHub Actions, Python 3.12, PyPA build, `pypa/gh-action-pypi-publish`.

---

### Task 1: Add and verify the publishing workflow

**Files:**
- Create: `.github/workflows/publish.yml`

- [ ] **Step 1: Create the workflow**

```yaml
name: Publish to PyPI

on:
  release:
    types: [published]

permissions:
  contents: read

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install build tooling
        run: python -m pip install --upgrade build
      - name: Build distributions
        run: python -m build
      - name: Publish distributions to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

- [ ] **Step 2: Validate the workflow structure**

Run a Python YAML parse and assert that the event is `release.published`, the job uses environment `pypi`, `id-token` is `write`, and the publish action is `pypa/gh-action-pypi-publish@release/v1`.

Expected: command exits 0 and prints `publish workflow valid`.

- [ ] **Step 3: Verify the project**

Run: `.venv/bin/pytest -q`

Expected: all existing tests pass.

Run: `.venv/bin/python -m build`

Expected: `dist/metergraphrelay-0.3.0.tar.gz` and `dist/metergraphrelay-0.3.0-py3-none-any.whl` are created.

- [ ] **Step 4: Commit and push**

```bash
git add .github/workflows/publish.yml docs/superpowers/plans/2026-08-13-pypi-publishing-plan.md
git commit -m "ci: publish releases to PyPI with trusted publishing"
git push origin main
```

