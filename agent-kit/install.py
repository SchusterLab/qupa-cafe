#!/usr/bin/env python3
"""Run the agent-kit installer from a checkout.

The installer itself lives in ``qupa_cafe/install.py`` so that it ships with the
package and a ``pip install`` can run it as ``qupa-install``. This file stays
here because the documentation, the provisioning message, and every researcher's
notes say ``python install.py``, and breaking that to tidy a layout would be a
poor trade.

Both routes reach the same code:

    python install.py            # from this checkout
    qupa-install                 # after pip install qupa-cafe
"""

from __future__ import annotations

import sys
from pathlib import Path

# A checkout is not on sys.path when this is run as a script from another
# directory, so make the package importable before importing from it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qupa_cafe.install import main  # noqa: E402 - needs the path set first

if __name__ == "__main__":
    sys.exit(main())
