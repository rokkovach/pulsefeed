# syntax=docker/dockerfile:1
ARG FLUX_RUNTIME_PLATFORM=linux/amd64
# CPU-only contract/SDK tests; no torch, CUDA or model downloads.
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS cpu-tests
WORKDIR /worker
COPY requirements-test.txt ./
RUN --mount=type=cache,id=flux-cpu-pip,target=/root/.cache/pip,sharing=locked \
    python -m pip install --cache-dir=/root/.cache/pip -r requirements-test.txt \
    && python -m pip install --cache-dir=/root/.cache/pip --no-deps runpod==1.12.0
COPY flux_worker.py handler.py local_factory.py preflight.py repair_vendor_wheel_tag.py ./
COPY tests ./tests
RUN python -m pytest -q tests

FROM --platform=${FLUX_RUNTIME_PLATFORM} python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
WORKDIR /worker
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends ca-certificates libgomp1 libstdc++6 && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 \
    HF_HOME=/runpod-volume/huggingface HF_HUB_CACHE=/runpod-volume/huggingface/hub \
    HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_XET=1 HF_HUB_ETAG_TIMEOUT=10 HF_HUB_DOWNLOAD_TIMEOUT=30 \
    TOKENIZERS_PARALLELISM=false FLUX_RUNTIME_PROFILE=runpod MODEL_CACHE_SOURCE=volume RUNPOD_LOG_LEVEL=INFO \
    MODEL_REVISION=e7b7dc27f91deacad38e78976d1f2b499d76a294 \
    FLUX_STARTUP_TIMEOUT_SECONDS=600 FLUX_EXECUTION_TIMEOUT_SECONDS=540 \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility
COPY requirements-bootstrap.lock requirements-runpod.lock /worker/
RUN --mount=type=cache,id=flux-clean-cu129-pip,target=/root/.cache/pip,sharing=locked \
    python -m pip install --cache-dir=/root/.cache/pip --timeout 120 --retries 5 --no-deps --require-hashes --only-binary=:all: -r requirements-bootstrap.lock
RUN --mount=type=cache,id=flux-clean-cu129-pip,target=/root/.cache/pip,sharing=locked \
    python -m pip install --cache-dir=/root/.cache/pip --timeout 120 --retries 5 --resume-retries 5 --no-compile --no-deps --require-hashes --only-binary=:all: -r requirements-runpod.lock \
    && python -m pip check \
    && python -c "import ssl,torch,runpod; from diffusers import Flux2KleinPipeline; from transformers import Qwen3ForCausalLM; assert torch.__version__ == '2.13.0+cu129'; assert torch.version.cuda == '12.9'; print(ssl.OPENSSL_VERSION)"
COPY cleanup_bootstrap.py /tmp/cleanup_bootstrap.py
RUN mkdir -p /opt/flux-build-evidence && python /tmp/cleanup_bootstrap.py && rm /tmp/cleanup_bootstrap.py \
    && python -m pip check && python -m pip freeze --all > /opt/flux-build-evidence/requirements.freeze
COPY flux_worker.py handler.py smoke_test.py preflight.py /worker/
CMD ["python", "-u", "handler.py", "--rp_log_level", "INFO"]
