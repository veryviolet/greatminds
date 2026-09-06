"""Launch gate: do not execute a harness until its identity is durably saved.

This helper deliberately imports no project modules. EOF means the supervisor
died before authorizing exec, so no agent is launched in that crash window.
"""

import os
import sys


def main():
    gate = int(sys.argv[1])
    try:
        proceed = os.read(gate, 1) == b"1"
    finally:
        os.close(gate)
    if not proceed:
        return 125
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == "__main__":
    sys.exit(main())
