#!/usr/bin/env python3
"""Synchronize inline Lambda code or render an immutable template release directory."""

import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent


def render(source, marker, code):
    pattern = re.compile(
        rf"(?ms)^(?P<indent> +)# BEGIN GENERATED {marker}\n.*?^(?P=indent)# END GENERATED {marker}$"
    )

    def replace(match):
        indent = match.group("indent")
        lines = [f"# BEGIN GENERATED {marker}", *code.rstrip().splitlines(), f"# END GENERATED {marker}"]
        return "\n".join(indent + line if line else "" for line in lines)

    rendered, count = pattern.subn(replace, source)
    if count != 1:
        raise ValueError(f"Expected exactly one {marker} insertion marker")
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bucket")
    parser.add_argument("--version")
    args = parser.parse_args()
    if args.output:
        if args.check or not args.bucket or not args.version or args.output.resolve() == ROOT:
            parser.error("--output requires --bucket and --version, and must not overwrite the source directory")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", args.bucket):
            parser.error("Invalid S3 bucket name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.version):
            parser.error("Invalid release version")
    elif args.bucket or args.version:
        parser.error("--bucket and --version require --output")
    auth = (ROOT / "wif_auth.py").read_text()
    bootstrap = (ROOT / "wif_bootstrap.py").read_text()
    bootstrap = "\n".join(line for line in bootstrap.splitlines() if not line.startswith("from wif_auth import "))
    changed = []
    rendered = {}
    for filename, marker, code in [
        ("main_organizations.yaml", "WIF AUTH", auth),
        ("main_organizations_wif.yaml", "WIF BOOTSTRAP", auth + "\n\n" + bootstrap),
    ]:
        path = ROOT / filename
        current = path.read_text()
        result = render(current, marker, code)
        rendered[filename] = result
        if result != current:
            changed.append(filename)
            if not args.check and not args.output:
                path.write_text(result)
    if args.check and changed:
        print("Inline WIF code is stale: " + ", ".join(changed) + ". Run python render_wif.py.", file=sys.stderr)
        return 1
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        for source in ROOT.glob("*.yaml"):
            content = rendered.get(source.name, source.read_text())
            content = content.replace("<BUCKET_PLACEHOLDER>", args.bucket).replace("<VERSION_PLACEHOLDER>", args.version)
            (args.output / source.name).write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
