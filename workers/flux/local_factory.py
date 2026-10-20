"""Factory for the root-owned stdlib local worker gateway; no Runpod import."""
try:
    from .flux_worker import DiffusersEngine, RuntimeConfig, Worker
except ImportError:  # /worker on PYTHONPATH in the DGX container.
    from flux_worker import DiffusersEngine, RuntimeConfig, Worker


class LocalFluxWorker(Worker):
    """Local factory seam; core readiness policy is shared with Runpod.

    Unknown native work still requires the gateway to exit while holding its
    cross-container GPU lock. Readiness alone cannot prove CUDA work stopped.
    """


def create_worker():
    """Return a fully initialized worker; gateway owns startup/exit watchdogs.

    handle(job, cancelled=threading.Event()) returns the raw shared envelope.
    The gateway owns authentication, result wrapping, volatile job/result state, and the
    cross-container LOCAL_GPU_LOCK. Do not take that flock recursively here.
    """
    config = RuntimeConfig.from_env()
    engine = DiffusersEngine(config)
    return LocalFluxWorker(engine, execution_seconds=config.execution_seconds)
