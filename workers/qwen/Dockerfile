# syntax=docker/dockerfile:1
# Prepare the private offline context with prepare_build.py before building.
FROM --platform=linux/amd64 vllm/vllm-openai@sha256:f99aa709b3f873aa1e1aadf5c1db35970119e8af9b36fe15d2a667aae0ed005c AS corrected-base
COPY base-gpu.lock /opt/pulsefeed-base/base-gpu.lock
# Match PyTorch's NCCL pin; this dense single-GPU worker does not use EP/M2N.
# Remove only reviewed unused LMCache and Launchpad/apt-source configuration tools.
RUN --network=none --mount=type=bind,source=build-inputs/gpu,target=/gpu,readonly \
    python3 -m pip install --no-index --no-cache-dir --no-deps --require-hashes --force-reinstall --find-links=/gpu -r /opt/pulsefeed-base/base-gpu.lock \
    && python3 -m pip uninstall -y lmcache mooncake-transfer-engine \
    && DEBIAN_FRONTEND=noninteractive apt-get purge -y python3-httplib2 python3-oauthlib python3-launchpadlib python3-lazr.restfulclient python3-software-properties software-properties-common
COPY apply_os_updates.py base-os-artifacts.json /opt/pulsefeed-os-security/
RUN --network=none --mount=type=bind,source=build-inputs/os,target=/os-debs,readonly \
    python3 /opt/pulsefeed-os-security/apply_os_updates.py /opt/pulsefeed-os-security/base-os-artifacts.json /os-debs \
    && rm -f /var/cache/apt/archives/*.deb
COPY base-python-inventory.json base-os-inventory.tsv verify_base.py lock_dependencies.py /opt/pulsefeed-base/
RUN --network=none python3 /opt/pulsefeed-base/verify_base.py /opt/pulsefeed-base/base-python-inventory.json /opt/pulsefeed-base/base-os-inventory.tsv

FROM corrected-base AS runtime
WORKDIR /worker
COPY requirements-runpod.lock requirements-runpod.provenance.json check_dependencies.py lock_dependencies.py /worker/
# No resolution or network: every added wheel and the full inherited closure is locked.
RUN --network=none --mount=type=bind,source=build-inputs/overlay,target=/overlay,readonly \
    python3 -m pip install --no-index --no-cache-dir --no-deps --require-hashes --find-links=/overlay -r requirements-runpod.lock \
    && python3 /worker/check_dependencies.py \
    && python3 /worker/lock_dependencies.py verify requirements-runpod.provenance.json --lock-file requirements-runpod.lock
COPY handler.py runtime.py smoke_test.py healthcheck.py verify_runpod_runtime.py /worker/
RUN --network=none python3 /worker/verify_runpod_runtime.py \
    && python3 -m pip freeze > /worker/build-packages.txt
ENV PYTHONUNBUFFERED=1 VLLM_WORKER_MULTIPROC_METHOD=spawn RUNPOD_LOG_LEVEL=INFO \
    HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_ETAG_TIMEOUT=10 \
    MODEL_REVISION=ccd10a893cbca613259517c3efe08e151ddf2b8e \
    MODEL_CACHE_SOURCE=volume MODEL_CACHE_OFFLINE=false \
    MODEL_CACHE_DIR=/runpod-volume/huggingface INITIALIZATION_TIMEOUT_SECONDS=480 \
    EXECUTION_TIMEOUT_SECONDS=240
HEALTHCHECK --interval=15s --timeout=3s --start-period=480s --retries=2 CMD python3 /worker/healthcheck.py
ENTRYPOINT ["python3", "-u", "/worker/handler.py"]
