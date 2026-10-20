# Cloud deployment checkpoint — 20 October 2026

The production web app is available at [PulseFeed](https://pulsefeed-web-production.up.railway.app). **Real generation is paused** (`INFERENCE_ENABLED=false`, fake mode false) until the two Runpod endpoints are configured and verified. This is a production app deployment, not evidence of Runpod GPU inference.

## Railway

- Project: `pulsefeed`; service: `pulsefeed-web`; environment: `production`.
- HTTPS origin: `https://pulsefeed-web-production.up.railway.app`.
- Persistent volume mounted at `/data`; database `/data/pulsefeed.db`.
- One replica and one Uvicorn process. Healthcheck `/health`, 120 seconds; failure restart cap three.
- Initial policy: 100 FLUX attempts/day, 30 starts/rolling hour, nine stories/new page.
- Independent random `SESSION_SECRET` and `ADMIN_ACCESS_TOKEN` configured privately. Values are not in this document or source bundle.
- Admin sign-in: `/admin`, using `ADMIN_ACCESS_TOKEN` from the service's private Variables. Use the password form; no credential belongs in the URL.

### Verification

The exact source passed 545 app tests and all nine browser-behavior suites. The live HTTPS smoke passed homepage rendering, secure/HttpOnly/SameSite visitor cookies, admin sign-in, dashboard access, CSRF rejection and session revocation on logout. Paused admission/retry returns `503 INFERENCE_PAUSED` and admits no jobs. A clean-source redeployment retained the visitor session through persistent SQLite and stable secrets. The volume attachment reports Ready.

Verified deployment: `ead7cf1a-a1d6-4f56-9033-d16df30cb5f9`. Local private evidence is under the ignored `integration/artifacts/railway-2026-10-20/` directory.

### Repeat a deployment

Use a clean allowlisted source bundle containing only the app's build inputs and runtime assets. The uploaded bundle for this checkpoint contained 42 files and no environment files, databases, model caches or test artifacts. Use `railway up <bundle> --path-as-root` with explicit project, service and environment selectors. The upload root is already the app root; do not set `/app` as the service root for this flattened bundle.

Set `RAILWAY_DOCKERFILE_PATH=Dockerfile` and the service Dockerfile path explicitly. The generic `railway redeploy` command selected Railpack at this checkpoint and failed during build; the previous healthy deployment continued serving. The verified route is a clean `railway up` upload, which uses Dockerfile builds and retains the volume. Do not describe the failed generic redeploy as a persistence failure.

The current API rejects updating `railwayConfigFile` because JSON/TOML Config as Code is deprecated. The legacy `app/railway.json` remains a reference; healthcheck, replica count and restart policy were applied and verified explicitly. Future IaC migration is separate from this successful release.

Railway's builder rejects generic BuildKit cache-mount IDs. The app Dockerfile instead preserves a separately cached dependency layer and disables pip's retained download cache. GPU worker Dockerfiles remain separate and unchanged by this app adjustment.

The [current worker release](image-release-2026-10-20.md) records the public images and exact digests.

## Enable real inference

1. Create two Runpod Queue endpoints with the released AMD64 worker images and intended RTX PRO 4500 Blackwell hardware.
2. Verify each real GPU/native-queue result, including the structured worker envelope and image decoding.
3. Set `RUNPOD_API_KEY`, `RUNPOD_QWEN_ENDPOINT_ID` and `RUNPOD_FLUX_ENDPOINT_ID` privately on the Railway service. The provider key must never be pasted into chat or an image.
4. Set `INFERENCE_ENABLED=true`, keep `PULSEFEED_FAKE_MODE=false`, and deploy the same clean-source route. Normal credential and SQLite provider-identity checks apply. The unbound paused database can bind to its first real endpoint pair; an existing bound database still refuses endpoint replacement.
5. Verify the connected real feed flow, aggregate usage and quota accounting. The `/health` mode changing to `live` alone does not prove model readiness.

[Deployment guide](deployment.md) · [Models-first guide](runpod-first-deployment.md)
