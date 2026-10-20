# Public worker release — 20 October 2026

The maintained **linux/amd64** Qwen and FLUX images are published in the public [rokkovach/pulsefeed repository](https://hub.docker.com/r/rokkovach/pulsefeed/tags). Runpod can pull them without private registry credentials. The repository visibility change was explicitly authorized by the owner.

| Worker | Versioned image |
| --- | --- |
| QWEN | `docker.io/rokkovach/pulsefeed:qwen-v1.0.1-20261020` |
| FLUX | `docker.io/rokkovach/pulsefeed:flux-v1.0.1-20261020` |

## Immutable deployment references

Use these registry manifest digests for reproducible deployment. They differ from local Docker configuration/image IDs.

```text
QWEN: docker.io/rokkovach/pulsefeed@sha256:cf9971170889687168c0c38c44b83cb1d0bf2df32abe472f7a1f0483e0ba11d7
FLUX: docker.io/rokkovach/pulsefeed@sha256:e674b604f7b64e1e30a3636ef74b9c8bf9c7f4e01033a464ae6b028dd9883f0c
```

## Verification

Both images were tagged directly from the maintained, tested AMD64 images; no model checkpoint, precision, GPU target or worker source changed during publication.

| Worker | Tested configuration ID | Registry layers |
| --- | --- | --- |
| QWEN | `sha256:9c1173ac7ef0ee093a3ca0709bf4fcf42888428b7e4d4cb2ef394e5c052208be` | 51 |
| FLUX | `sha256:4ac720be06ce786143441074b62d25c714cb009ee960e252b07483edff9d27d2` | 12 |

- Manifest and configuration payload SHA-256 checks passed; configurations match the tested local image IDs exactly.
- Anonymous registry manifest/config reads and HEAD checks for every filesystem layer passed. A full image redownload was unnecessary.
- Both configurations report Linux/AMD64. Populated provider/admin credential environment variables were absent.
- Qwen initially encountered Docker Hub HTTP 502 responses while uploading two large base layers. Reusing the exact pinned upstream blobs and refreshing Docker registry metadata completed publication without rebuilding or changing the tested configuration.
- Temporary publication credentials were removed from Spark after each push. Existing native Spark Qwen and FLUX containers remained healthy on their original image IDs.

The [dependency remediation report](dependency-remediation-2026-10-20.md) records CPU/package checks, real Spark evidence and retained upstream findings. Its no-publication statement describes the earlier remediation checkpoint; this document records the later publication. Public availability and CPU/Spark checks do **not** establish AMD64 RTX PRO 4500 Blackwell/native Runpod queue execution. That remains the next gate.

The [Railway production app](cloud-release-2026-10-20.md) is deployed with real generation paused until both endpoint configurations and GPU results are verified. The deck and handouts generated earlier on this date describe the preceding prepublication checkpoint.

Continue at [create two custom queue endpoints](runpod-first-deployment.md#2-create-two-custom-queue-endpoints). Never deploy the historical October 16 tags as the current release.
