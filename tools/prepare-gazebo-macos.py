#!/usr/bin/env python3
"""Give Gazebo's Ruby-hosted Qt GUI a normal macOS application identity."""

import os
from pathlib import Path
import plistlib
import shutil
import tempfile

prefix = Path(os.environ["CONDA_PREFIX"])
contents = Path(__file__).resolve().parents[1] / ".pixi/Orbo Gazebo.app/Contents"
binary = contents / "MacOS/ruby"
binary.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(dir=binary.parent, delete=False) as stream:
    replacement = Path(stream.name)
try:
    shutil.copy2(prefix / "bin/ruby", replacement)
    replacement.replace(binary)
finally:
    replacement.unlink(missing_ok=True)
for name in ("lib", "share", "plugins", "opt"):
    link = contents / name
    if not link.exists():
        link.symlink_to(prefix / name, target_is_directory=True)
with (contents / "Info.plist").open("wb") as stream:
    plistlib.dump(
        {
            "CFBundleIdentifier": "org.little-orbo.gazebo",
            "CFBundleName": "Orbo Gazebo",
            "CFBundleExecutable": "ruby",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "NSHighResolutionCapable": True,
        },
        stream,
    )
print(binary)
