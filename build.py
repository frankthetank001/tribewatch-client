#!/usr/bin/env python3
"""Build script for TribeWatch — PyInstaller bundle + Inno Setup installer.

Usage:
    python build.py              # Build exe only (dist/TribeWatch/)
    python build.py --installer  # Build exe + Inno Setup installer
    python build.py --version    # Print current version
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def get_version() -> str:
    init_path = os.path.join(ROOT, "tribewatch", "__init__.py")
    with open(init_path) as f:
        for line in f:
            if line.startswith("__version__"):
                return line.split("=")[1].strip().strip('"').strip("'")
    return "0.0.0"


def update_inno_version(version: str) -> None:
    """Patch the version in installer.iss."""
    iss_path = os.path.join(ROOT, "installer.iss")
    with open(iss_path, "r") as f:
        content = f.read()

    import re
    content = re.sub(
        r'#define MyAppVersion ".*?"',
        f'#define MyAppVersion "{version}"',
        content,
    )
    with open(iss_path, "w") as f:
        f.write(content)


def build_exe() -> bool:
    """Run PyInstaller to produce dist/TribeWatch/."""
    print(f"Building TribeWatch v{get_version()} with PyInstaller...")

    dist_dir = os.path.join(ROOT, "dist", "TribeWatch")
    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)

    result = subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            os.path.join(ROOT, "tribewatch.spec"),
        ],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("PyInstaller failed!")
        return False

    print(f"Build complete: {dist_dir}")
    return True


def build_installer(iss_file: str = "installer.iss") -> bool:
    """Run Inno Setup to produce the installer exe."""
    iscc = shutil.which("iscc")
    if not iscc:
        # Try common Inno Setup install paths
        for path in [
            r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            r"C:\Program Files\Inno Setup 6\ISCC.exe",
        ]:
            if os.path.exists(path):
                iscc = path
                break

    if not iscc:
        print("Inno Setup (ISCC.exe) not found. Install from https://jrsoftware.org/isdl.php")
        print("The PyInstaller build is available at dist/TribeWatch/")
        return False

    print(f"Building installer with Inno Setup ({iss_file})...")
    result = subprocess.run(
        [iscc, os.path.join(ROOT, iss_file)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("Inno Setup failed!")
        return False

    print("Installer ready in dist/")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Build TribeWatch")
    parser.add_argument("--installer", action="store_true", help="Also build Inno Setup installer")
    parser.add_argument("--iss", default="installer.iss", help="Inno Setup script file (default: installer.iss)")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    args = parser.parse_args()

    if args.version:
        print(get_version())
        return

    if not build_exe():
        sys.exit(1)

    if args.installer:
        if not build_installer(args.iss):
            sys.exit(1)

    print("\nDone!")


if __name__ == "__main__":
    main()
