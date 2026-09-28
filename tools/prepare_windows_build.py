"""Prepare an independent, fresh Windows build tree; does not build or install."""
from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .source_release import ROOT, collect_sources, write_release
except ImportError:  # Direct invocation from tools/.
    from source_release import ROOT, collect_sources, write_release


def prepare(root: Path, destination: Path) -> Path:
    sources = collect_sources(root)
    destination = Path(destination).absolute()
    # Refuse any existing destination, including a link. Never delete a build
    # tree or copy from the machine's separate legacy installer workspace.
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in sources.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # The existing spec/NSIS scripts expect this staging layout.
    for name, data in sources.items():
        prefix = "packaging/windows/"
        if name.startswith(prefix):
            target = destination / "packaging" / name[len(prefix):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    mappings = {
        "packaging/windows/desktop.py": "adaptive_crypto/desktop.py",
        "packaging/windows/windows_launcher.py": "windows_launcher.py",
        "packaging/windows/self_test_btc.json": "adaptive_crypto/models/self_test_btc.json",
        "adaptive_crypto_settings.example.json": "adaptive_crypto_settings.json",
    }
    for source, target in mappings.items():
        (destination / target).write_bytes(sources[source])
    for flavor in ("Open", "Obfuscated"):
        (destination / "packaging" / f"README-{flavor}.txt").write_text(
            f"pyPTA ({flavor})\n\nPer-user Windows installation. Settings and records live "
            "outside the installation directory. New profiles start with empty records.\n"
            "See docs/RELEASE_BASELINE.md in the separate source archive for build and verification steps.\n",
            encoding="utf-8",
        )
    write_release(sources, destination / "release", "1.0.1")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.root, args.destination))


if __name__ == "__main__":
    main()
