# Release and distribution

This page describes the release behavior defined in this repository. External
registry state can change independently, so verify it during a release.

## Published surfaces

A release is complete only after all four published surfaces are verified:

| Surface | Published result | Automation |
|---|---|---|
| PyPI | The `remove-ai-watermarks` wheel and source distribution | `publish.yml` |
| Homebrew | The `remove-ai-watermarks` formula in `wiltodelta/homebrew-tap` | `distribute.yml` |
| Hugging Face Space | A factory rebuild of `wiltodelta/remove-ai-watermarks` against the new PyPI release | `distribute.yml` |
| ComfyUI Registry | A compatible release of `wiltodelta/ComfyUI-remove-ai-watermarks` with its own node version | `distribute.yml` and the node repository's workflows |

The GitHub Release is the trigger and release record for this flow. Conda is
not a supported publishing surface: this repository has no conda recipe or
conda publication job.

## Release sources of truth

The package version appears in:

- `pyproject.toml`;
- `src/remove_ai_watermarks/__init__.py`;
- the root package entry generated in `uv.lock`.

Update the first two, then refresh the lock file with uv. Do not edit a
line-number-specific location in `uv.lock`; its package order changes.

## Publish flow

PyPI publishing is triggered by a published GitHub Release, not by a tag push
alone.

The expected sequence:

1. update the version sources and lock file;
2. run the complete project gate;
3. commit the release change;
4. create an annotated `vX.Y.Z` tag;
5. push the commit and tag;
6. publish the GitHub Release.

`.github/workflows/publish.yml` then:

1. checks that the release tag matches `pyproject.toml`;
2. builds the package with uv;
3. publishes with `uv publish` through PyPI trusted publishing.

The workflow uses GitHub OIDC through the `pypi` environment. It does not read a
PyPI API token from the repository.

## Post-release distribution

`.github/workflows/distribute.yml` runs on the same published-release event. It
waits for the matching source distribution to appear on PyPI, then:

- updates the Homebrew tap formula URL and SHA-256;
- triggers a factory rebuild of the Hugging Face Space;
- synchronizes, tests, versions, and publishes the ComfyUI nodes.

The workflow can also be started manually with an optional version input.

If a distribution job fails because a repository or Hugging Face credential is
invalid, rotate the corresponding GitHub secret and rerun the failed job. A
manual Homebrew formula update is the fallback when its automation is blocked.

## Source distribution boundary

The wheel includes the package under `src/`.

The source distribution uses an explicit allowlist for `/src`, `/LICENSE`,
`/README.md`, and `/pyproject.toml` through
`[tool.hatch.build.targets.sdist]` in `pyproject.toml`. It also defensively
excludes `/data`, `/tmp`, and `/.sc`. Keep both controls: calibration captures,
test corpora, generated research outputs, and local session state do not belong
in the published package archive. Hatchling always adds the root `.gitignore` to
the sdist, so keep its comments generic and free of local operational context.
Ignore rules are not the build boundary: `data/` is deliberately tracked, while
the sdist configuration keeps it and the other excluded paths out of the archive.

## Build backend

The package uses hatchling through the unpinned `hatchling` build requirement in
`pyproject.toml`. Uploading uses uv rather than the older twine-based action.

## Other channels

The ComfyUI nodes are maintained and versioned in their own repository. After
the matching source distribution appears on PyPI, `distribute.yml` dispatches
that repository's sync workflow with the exact library version and waits for it
to finish. The sync updates the dependency floor, runs compatibility tests,
bumps the node patch version, and publishes to the ComfyUI Registry only when
those tests pass. Its daily schedule remains as a recovery path if a release
dispatch is interrupted. The `COMFYUI_RELEASE_TOKEN` repository secret is a
fine-grained token limited to the ComfyUI node repository, with Actions read and
write access.

## Release verification

Forensic transports are versioned independently from the package. Before publishing
a change to provenance metadata, provenance reports, broad forensic metadata, or
pixel evidence, run their schema 1 contract tests. Additive fields are compatible;
renaming a field, changing its type or meaning, changing a signal name or watermark
label, or removing a field requires a new output schema. Add the new serializer
without removing schema 1 so long-lived consumers can update separately. A package
release must never silently substitute its latest schema when a caller explicitly
requests an older supported one.

After publication, verify:

- both wheel and source distribution exist on PyPI;
- the package version matches the tag;
- the Homebrew formula points to the new source distribution;
- the distribution workflow completed successfully;
- the ComfyUI Registry node requires the new library version;
- a clean install can run `remove-ai-watermarks --version`.
