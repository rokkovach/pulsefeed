"""Verify the complete corrected Python and OS baseline before adding the overlay."""
import json
from pathlib import Path
import subprocess
import sys
from lock_dependencies import inventory


def verify(python_path, os_path):
    expected = json.loads(Path(python_path).read_text())
    if inventory() != expected:
        raise ValueError('Corrected Python baseline differs from reviewed inventory.')
    query = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Architecture}\n'],
                           capture_output=True, text=True, check=True)
    def rows(text):
        return {line.split('\t')[0]: line.split('\t')[1:] for line in text.splitlines()}
    if rows(query.stdout) != rows(Path(os_path).read_text()):
        raise ValueError('Corrected OS baseline differs from reviewed inventory.')
    print('Complete corrected Python/OS baseline matches reviewed inventory.')


if __name__ == '__main__':
    verify(sys.argv[1], sys.argv[2])
