"""Fail on every dependency inconsistency in the corrected runtime image."""
import subprocess
import sys


def main(run=subprocess.run):
    result = run([sys.executable, '-m', 'pip', 'check'], capture_output=True, text=True, check=False)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode == 0 and not result.stderr:
        return 0
    return result.returncode if result.returncode > 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
