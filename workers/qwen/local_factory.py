"""Factory for the shared stdlib local gateway; no Runpod SDK lifecycle."""
import os

if __package__:
    from .handler import QwenWorker, WorkerError
    from .runtime import ProcessRuntime
else:
    from handler import QwenWorker, WorkerError
    from runtime import ProcessRuntime


class LocalQwenWorker:
    def __init__(self, runtime, execution_seconds):
        self.runtime = runtime
        self.worker = QwenWorker(runtime, execution_seconds=execution_seconds)

    @property
    def ready(self):
        return self.runtime.ready

    def handle(self, job, cancelled=None):
        predicate = cancelled.is_set if hasattr(cancelled, "is_set") else cancelled or (lambda: False)
        return self.worker.handle(job, cancelled=predicate)

    def close(self):
        self.runtime.close()


def create_worker():
    runtime = ProcessRuntime.from_env()
    if runtime.config["profile"] != "dgx-spark":
        raise WorkerError("UNSUPPORTED_RUNTIME", "Local Qwen factory requires the DGX Spark profile.")
    try:
        execution_seconds = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "240"))
    except ValueError:
        raise WorkerError("INVALID_INPUT", "Execution timeout must be an integer.") from None
    if not 1 <= execution_seconds <= 600:
        raise WorkerError("INVALID_INPUT", "Execution limit must be between 1 and 600 seconds.")
    try:
        runtime.start()
        return LocalQwenWorker(runtime, execution_seconds)
    except BaseException:
        runtime.close()
        raise
