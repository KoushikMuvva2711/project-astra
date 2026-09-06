# CI/CD

Three workflows. Two run themselves; the third you trigger.

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | push to `develop`, any PR | Lint, migrate, `alembic check`, full test suite, secret scan |
| `release.yml` | merge to `master` | Re-verifies, tags `vX.Y.Z`, builds a multi-arch image to GHCR, cuts a Release |
| `deploy.yml` | manual | Ships a chosen version to the server and runs migrations |

## Branching

```
feature work ──▶ develop ──(PR)──▶ master ──▶ tagged release + image
```

`develop` is the working branch. Merging to `master` cuts a version — so **bump `VERSION` in the same PR**. The release job refuses to overwrite an existing tag: forget to bump and it fails loudly rather than silently shipping the previous version's tag under a new commit.

## Why CI needs almost no secrets

The test suite never calls a cloud model. Every model-touching path is required to degrade rather than raise — narration falls back to deterministic rendering, summarisation falls back to truncation — so CI points `OLLAMA_BASE_URL` at nothing and the tests pass anyway. **If a test ever needs a live model, that test is the bug.**

The only credential-shaped value in CI is a dummy device token long enough to satisfy the config validator, which rejects placeholders by design.

## Secrets to configure

**Settings → Secrets and variables → Actions**

Only needed once you deploy. CI works with none of them.

| Secret | Used by | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | deploy | Scope it to a spend-capped workspace, not Default |
| `ASTRA_DEVICE_TOKEN` | deploy | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `POSTGRES_PASSWORD` | deploy | Generate; never reuse a local one |
| `DEPLOY_HOST` | deploy | Server public IP |
| `DEPLOY_USER` | deploy | `ubuntu` on Oracle's Ubuntu images |
| `DEPLOY_SSH_KEY` | deploy | **Private** key, whole file including header/footer |

Optional **variable** (not a secret): `ASTRA_MONTHLY_COST_CEILING_MINOR`, in paise. Defaults to `200000` (₹2,000).

`GITHUB_TOKEN` is provided automatically — no setup.

## Cutting a release

1. Work on `develop`; CI runs on every push
2. Bump `VERSION` (e.g. `0.1.0` → `0.2.0`)
3. PR `develop` → `master`, merge when green
4. Release runs: tag, image, GitHub Release
5. Run **Deploy** manually with that version

## Deploying

Actions → **Deploy** → Run workflow → enter the version (or `latest`).

It writes a production compose file and `.env` from your secrets, ships both over SSH, pulls the image, restarts, runs migrations, and polls `/health/live` for 60 seconds. If the API doesn't answer it prints the last 50 log lines and fails rather than reporting success.

The production compose differs from the dev one in three ways: no source bind-mount, no `--reload`, and the image comes from GHCR rather than a local build. Postgres and Redis stay bound to loopback — the tunnel fronts the API, and the database is never reachable from outside the host.

## Multi-arch, and why

Images build for `linux/amd64` **and** `linux/arm64`. The Oracle Always Free instance is Ampere ARM; an amd64-only image fails there with a confusing `exec format error`. QEMU makes the arm64 build slower — that's the cost of the free tier.

## Branch protection worth setting

On `master`: require the CI check to pass, require a PR. That prevents a direct push from cutting a release that never ran tests.
