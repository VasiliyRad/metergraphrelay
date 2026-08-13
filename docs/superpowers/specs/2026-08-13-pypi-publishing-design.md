# PyPI publishing workflow

Date: 2026-08-13

## Goal

Publish `metergraphrelay` to PyPI automatically when a GitHub Release is
published.

## Design

Add `.github/workflows/publish.yml` with one release-publishing job:

- Trigger only on the GitHub `release.published` event.
- Check out the released repository revision.
- Use Python 3.12 and build the source distribution and wheel with
  `python -m build`.
- Publish `dist/` through `pypa/gh-action-pypi-publish` using PyPI Trusted
  Publishing.
- Declare `id-token: write` and the GitHub environment `pypi`, matching the
  trusted publisher registered for `VasiliyRad/metergraphrelay` and workflow
  filename `publish.yml`.
- Do not store or read a PyPI API token.

Publishing remains an explicit release action: pushes to `main` and ordinary
tags do not upload packages. PyPI rejects an already-published version, so the
release version must match the package version in `pyproject.toml`.

## Verification

- Parse the workflow as YAML.
- Confirm the trigger, environment, OIDC permission, build step, and trusted
  publishing action are present.
- Run the existing test suite before committing and pushing.

