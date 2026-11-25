# Copyright (c) 2026 Ximmer's Creations <ximmer@ximmer.dev>.
# Licensed under the source-available proprietary terms in LICENSE.
# Personal noncommercial use and private modifications only; see LICENSE.

import os
from pathlib import Path
import re
import sys


def main():
    ref_type = os.environ["GITHUB_REF_TYPE"]
    if ref_type == "tag":
        version = os.environ["GITHUB_REF_NAME"].removeprefix("v")
        if not version:
            raise ValueError("The tag must contain a version")
    elif ref_type == "branch":
        sha = os.environ["GITHUB_SHA"]
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ValueError("GITHUB_SHA must be a full commit SHA")
        version = f"development-{sha[:7]}"
    else:
        raise ValueError(f"Unsupported GITHUB_REF_TYPE: {ref_type!r}")

    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    source, count = re.subn(
        r"^APP_VERSION = .*$",
        lambda match: f"APP_VERSION = {version!r}",
        source,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError(f"Expected one APP_VERSION assignment, found {count}")
    path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
