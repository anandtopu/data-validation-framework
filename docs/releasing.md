# Releasing

This page is for maintainers. A release is fully automated once it is approved: merging the release pull request publishes the package to PyPI, the container images to GHCR, the GitHub Release with its notes, distributions, and SBOMs, and the documentation site. Nothing is published with a stored token.

## How it fits together

| Piece | Where | What it does |
|---|---|---|
| Conventional commits | every commit on `main` | Decide the next version: `fix:` is a patch, `feat:` a minor version, `feat!:` or a `BREAKING CHANGE:` footer a major version |
| release-please | `release-please-config.json`, `.release-please-manifest.json` | Keeps a release pull request open with the next version; merging it tags `vX.Y.Z` and creates the GitHub Release |
| hatch-vcs | `pyproject.toml` | Takes the package version from the git tag, so no file holds the version |
| Release workflow | `.github/workflows/release.yml` | Builds, checks, attests, and publishes the tag |
| Docs workflow | `.github/workflows/docs.yml` | Builds the MkDocs site in strict mode and deploys it to GitHub Pages |
| `CHANGELOG.md` | curated by hand | The notes of each GitHub Release |

release-please was chosen over python-semantic-release because it works through a pull request: the release is reviewed and merged like any change, branch protection stays on, and CI never needs to push to `main`. Its `simple` strategy only bumps the manifest, which suits a project whose version comes from git tags.

The release workflow runs these jobs for a release tag:

1. **Build**: `uv build` makes the sdist and wheel from the tag. `twine check --strict` validates their metadata and README, the built version must equal the tag, the wheel is installed in a clean environment and runs the seeded example, and `nox -s sbom` writes CycloneDX SBOMs from `uv.lock`.
2. **PyPI**: a GitHub build provenance attestation is created for the sdist and wheel, then they are uploaded with Trusted Publishing, which also uploads PEP 740 attestations.
3. **Images**, in parallel with PyPI: the slim and full images are built for `linux/amd64` and `linux/arm64`, pushed to `ghcr.io/OWNER/dvf`, given BuildKit SBOM and provenance attestations and a GitHub build provenance attestation, then pulled back and smoke-tested.
4. **GitHub Release**: the notes are set from the version's `CHANGELOG.md` section, and the distributions and SBOMs are attached.
5. **Docs**: the site is built from the tag and deployed (final releases only).

Each job has only the permissions it needs; the workflow default is none. Release builds never restore caches. Every third-party action is pinned to a commit SHA, with its version in a comment for Dependabot.

## Cut a release

1. Merge work to `main` with conventional commit messages (squash-merge titles count). release-please opens or updates a pull request titled `chore: release X.Y.Z`.
2. Prepare the notes in a normal pull request: rename the `## [Unreleased]` heading of `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD`, add a new empty `## [Unreleased]` above it, and merge.
3. Check that CI is green on `main`, then merge the release pull request. release-please tags `vX.Y.Z` and creates the GitHub Release, and the release workflow starts in the same run.
4. Watch the run under **Actions → Release**. When it finishes, check [PyPI](https://pypi.org/project/dvf-framework/), the package page on GHCR, the GitHub Release, and the docs site.

Run the local checks first if the release contains packaging or container changes:

```bash
uv run nox -s dist         # build and twine check
uv run nox -s quickstart   # fresh environment to a report, timed
uv run nox -s docker       # both images, with the seeded example
uv run nox -s audit        # pip-audit of the lockfile
uv run nox -s docs         # strict docs build
```

### The first release

The manifest starts at `0.0.0`, and before 1.0 a `feat:` would only raise the minor version. Make the first release 1.0.0 with a `Release-As` footer in any commit on `main`:

```bash
git commit --allow-empty -m "chore: release 1.0.0" -m "Release-As: 1.0.0"
```

The same footer forces any other version later.

### Pre-releases

Push a pre-release tag by hand from `main`, for example `git tag v1.1.0-rc.1` and `git push origin v1.1.0-rc.1`. PyPI receives the normalized version `1.1.0rc1`, and the images are tagged `1.1.0-rc.1` and `1.1.0-rc.1-full` only: no `latest`, `X.Y`, or `X` tags. The GitHub Release is marked as a pre-release, and the docs site is not updated.

### Tag by hand, or publish again

Pushing a `vX.Y.Z` tag runs the same jobs without release-please. To publish an existing tag again, for example after fixing a failed job, use **Actions → Release → Run workflow** and enter the tag. Every step can be repeated: PyPI skips files it already has, images are pushed again under the same tags, and release assets are replaced.

## One-time setup

### PyPI Trusted Publishing

Trusted Publishing lets the workflow upload without an API token: PyPI accepts a short-lived OIDC token issued to this repository's release workflow in the `pypi` environment.

1. Sign in to PyPI with the account that will own the project, and enable two-factor authentication.
2. Before the first release, add a pending publisher under **Your account → Publishing** (after the first release, under the project's **Settings → Publishing**):
   - PyPI project name: `dvf-framework`
   - Owner: the GitHub account or organization; Repository: the repository name
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. The first successful upload creates the project. Add a second owner so the project is never left without one.

The names must match exactly; a mismatch fails the upload with `invalid-publisher`. To rehearse, add the same publisher on TestPyPI and temporarily set `repository-url: https://test.pypi.org/legacy/` on the publish step in a branch.

### GitHub environment `pypi`

Under **Settings → Environments**, create `pypi`:

- **Deployment branches and tags**: selected branches and tags only. Add the branch `main` (release-please releases run on `main`) and the tag pattern `v*` (tags pushed by hand).
- **Required reviewers** (optional): one or more maintainers. The PyPI job then waits for an approval, which is a useful second check before anything is published.

Never add a PyPI token as a secret.

### Repository settings

- **Actions → General → Workflow permissions**: read repository contents by default, and allow GitHub Actions to create and approve pull requests (release-please opens the release pull request).
- **Branch protection on `main`**: require the CI checks. Pull requests opened with the default `GITHUB_TOKEN` do not start workflows, so CI does not run on the release pull request by itself. Close and reopen it to start CI, or give release-please a GitHub App token through the action's `token` input.

### GHCR

The images are pushed with the workflow's `GITHUB_TOKEN` (`packages: write`); no other credential is needed.

- After the first release, open the package `dvf` under the owner's **Packages**, and under **Package settings** change the visibility to public. New packages are private.
- The `org.opencontainers.image.source` label links the package to the repository, so it appears on the repository page and inherits its access. If the package was created another way, add the repository under **Manage Actions access** with the write role.
- In an organization, members must be allowed to publish public packages (**Organization settings → Packages**).

### GitHub Pages

- **Settings → Pages → Build and deployment → Source**: GitHub Actions.
- The `github-pages` environment accepts deployments from the default branch only. For releases from tags pushed by hand, add the tag pattern `v*` under **Settings → Environments → github-pages → Deployment branches and tags**.
- The site is published at `https://OWNER.github.io/REPOSITORY/`. Set it as the repository's website, and as `site_url` in `mkdocs.yml`.

### Scorecard

`.github/workflows/scorecard.yml` runs the OpenSSF Scorecard weekly and on every push to `main`, uploads the findings to code scanning, and publishes them for the README badge. Publishing works for public repositories only, and the workflow must keep Scorecard's restrictions: no top-level `env` or write permissions, and only the approved actions in its job.

## Verify a release

Anyone can check that a distribution or image was built by this repository's release workflow, from the tagged source.

```bash
# The sdist and wheel, downloaded from PyPI or the GitHub Release
gh attestation verify dvf_framework-1.0.0-py3-none-any.whl --repo OWNER/REPOSITORY

# A container image
gh attestation verify oci://ghcr.io/OWNER/dvf:1.0.0 --repo OWNER/REPOSITORY
```

PyPI shows each file's PEP 740 attestation under **Provenance** on the file's page, and the `pypi-attestations` tool verifies it:

```bash
uvx pypi-attestations verify pypi --repository https://github.com/OWNER/REPOSITORY pypi:dvf_framework-1.0.0-py3-none-any.whl
```

The BuildKit SBOM (SPDX) and SLSA provenance stored with each image:

```bash
docker buildx imagetools inspect ghcr.io/OWNER/dvf:1.0.0 --format "{{ json .SBOM }}"
docker buildx imagetools inspect ghcr.io/OWNER/dvf:1.0.0 --format "{{ json .Provenance }}"
```

The CycloneDX SBOMs attached to the GitHub Release describe the locked Python dependencies: `dvf-framework-X.Y.Z.cdx.json` the runtime dependencies, and `dvf-framework-X.Y.Z-all-extras.cdx.json` with every extra. `uv run nox -s sbom` writes the same files locally.

## Yank a release

When a release is broken, publish a fixed patch release, then withdraw the broken one without deleting anything that others may have pinned.

1. **PyPI**: on the project's **Manage → Releases** page, choose **Options → Yank** for the version and give the reason. pip and uv no longer pick a yanked version unless it is pinned exactly. Do not delete it: PyPI never allows a file name or version to be uploaded again.
2. **Images**: point the moving tags back at a good version, for example:

   ```bash
   docker buildx imagetools create -t ghcr.io/OWNER/dvf:latest ghcr.io/OWNER/dvf:1.0.1
   docker buildx imagetools create -t ghcr.io/OWNER/dvf:latest-full ghcr.io/OWNER/dvf:1.0.1-full
   ```

   Keep the broken version's tags, so deployments that pinned it can still be inspected, unless it is a security problem.
3. **GitHub Release**: edit it, add a warning at the top of the notes that names the fixed version, and keep the tag.
4. **Docs**: if the site was published from the broken release, run **Actions → Docs → Run workflow** with the fixed tag.
5. **Security problems**: also follow `SECURITY.md`, publish a GitHub security advisory, and request a CVE through it.

## Dependency audits

The CI `audit` job runs `nox -s audit`, which exports `uv.lock` to hashed requirements files (the runtime dependencies with every extra, then every dependency group) and checks them with `pip-audit` against the PyPI and OSV advisory databases. Nothing is installed. When a finding cannot be fixed yet, ignore it explicitly and temporarily with `--ignore-vuln ID` in the session, with a comment that says why and when to remove it. Dependabot also raises alerts and update pull requests for the lockfile and the workflow actions.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The build job fails with "built ... but the tag says ..." | The tag does not point at the commit that was built, or the tag is not a valid version. Delete and recreate the tag on the right commit before anything was published, or release the next patch version. |
| `invalid-publisher` from PyPI | The pending or trusted publisher's owner, repository, workflow file name (`release.yml`), or environment (`pypi`) does not match. |
| The PyPI or docs job waits or is rejected by the environment | The `pypi` or `github-pages` environment's deployment rules do not allow the branch or tag. Add `main` and `v*`. |
| CI did not run on the release pull request | Pull requests opened with `GITHUB_TOKEN` start no workflows. Close and reopen it, or use a GitHub App token. |
| No release pull request appears | Only `feat`, `fix`, `perf`, `deps`, and `revert` commits, or a `Release-As` footer, start a release. Check that the workflow may create pull requests. |
| Image push fails with `denied` | The package exists but the repository has no write access to it. Add it under the package's **Manage Actions access**. |
