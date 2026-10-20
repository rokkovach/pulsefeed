"""CPU build gate for the fixed text-only AMD64 worker; never loads a model."""
import importlib.abc
import importlib.metadata
import json
from pathlib import Path
import sys
import site


class DenyMooncake(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.attempts = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mooncake' or fullname.startswith('mooncake.'):
            self.attempts.append(fullname)
            raise AssertionError('The unused Mooncake backend must not be imported.')
        return None


def main():
    try:
        importlib.metadata.distribution('mooncake-transfer-engine')
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError('The unused Mooncake distribution is still installed.')
    # pip uninstall must remove both Python and auditwheel native namespaces.
    leftovers = [str(p) for entry in site.getsitepackages() if Path(entry).is_dir()
                 for p in Path(entry).glob('mooncake*')]
    assert not leftovers, 'Mooncake-owned files remain installed.'
    guard = DenyMooncake()
    sys.meta_path.insert(0, guard)
    import torch
    import flashinfer
    import torchcodec.decoders
    from vllm import LLM, SamplingParams
    from vllm.engine.arg_utils import EngineArgs
    from runtime import engine_settings
    args = EngineArgs(model='/nonexistent-reviewed-offline-snapshot',
                      tokenizer='/nonexistent-reviewed-offline-snapshot',
                      quantization='modelopt_fp4',
                      **engine_settings({'profile': 'runpod', 'context_tokens': 4096}))
    assert args.kv_transfer_config is None and args.ec_transfer_config is None
    assert args.load_format == 'auto'
    assert args.tensor_parallel_size == args.pipeline_parallel_size == 1
    assert not args.enable_expert_parallel
    # Catch attempted imports even if an upstream optional-import catch swallowed denial.
    assert not guard.attempts
    assert not any(n == 'mooncake' or n.startswith('mooncake.') for n in sys.modules)
    maps = Path('/proc/self/maps').read_text()
    assert 'libetcd_wrapper' not in maps and 'libmooncake' not in maps
    print(json.dumps({'imports': 'torch/flashinfer/torchcodec.decoders/LLM/SamplingParams/EngineArgs',
                      'torch': torch.__version__, 'torchcodec': importlib.metadata.version('torchcodec'),
                      'kv_transfer_config': None, 'ec_transfer_config': None,
                      'expert_parallel': args.enable_expert_parallel,
                      'mooncake_import_attempts': guard.attempts,
                      'mooncake_files': leftovers, 'model_initialization': False}))


if __name__ == '__main__':
    main()
