The official NVIDIA cuSPARSELt CUDA 13 ARM64 0.8.1 wheel names an ARM64 platform in its filename but records `manylinux2014_sbsa` inside `WHEEL`. Pip rejects that internal tag on ARM64.

`repair_vendor_wheel_tag.py` makes the transparent local version `0.8.1+pulsefeed.arm64tag1`, corrects only the packaging tag, and regenerates `RECORD`. It verifies that every CUDA/library/license payload byte remains unchanged. It accepts only the authenticated upstream wheel and writes a separate source/output hash proof. It does not modify CUDA code or dependency requirements.

The current default Dockerfiles do not select this candidate repair. Do not commit the 221 MB vendor archive. Fetch the exact public source in `provenance.json`, verify its SHA256, and run:

```sh
python3 repair_vendor_wheel_tag.py SOURCE.whl 4dca476c50bf4780d46cd0bfbd82e2bc10a08e4fef7950917ce8d7578d22a23f OUTPUT_DIRECTORY repair-proof.json
```

The upstream NVIDIA license remains inside the unchanged wheel payload. The source URL, original hash, and honest local version must remain visible in image provenance. Runtime imports and the real GB10 model smoke remain separate validation gates; this packaging correction does not establish general CUDA compatibility.
