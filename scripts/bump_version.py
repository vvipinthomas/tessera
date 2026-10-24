"""Set Tessera's version everywhere it appears. Pushing the result to main publishes the release.

    python scripts/bump_version.py 1.7.2

Updates pyproject.toml, tessera/__init__.py, CITATION.cff, the Cloudflare Worker package, the container tag in
docker-compose.yml, the AWS template's default tag, and versioned install links in the README and docs. Add the
release's section to CHANGELOG.md yourself; the release notes are taken from it.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def sub(path: str, pattern: str, repl: str, count: int = 0, required: bool = True) -> None:
    p = ROOT / path
    text = p.read_text()
    new, n = re.subn(pattern, repl, text, count=count, flags=re.M)
    if required and n == 0:
        raise SystemExit(f"{path}: pattern not found: {pattern}")
    p.write_text(new)


def main() -> None:
    if len(sys.argv) != 2 or not SEMVER.match(sys.argv[1]):
        raise SystemExit("usage: python scripts/bump_version.py X.Y.Z")
    v = sys.argv[1]
    sub("pyproject.toml", r'^version = "[^"]+"', f'version = "{v}"', count=1)
    sub("tessera/__init__.py", r'^__version__ = "[^"]+"', f'__version__ = "{v}"', count=1)
    sub("CITATION.cff", r"^version: .+$", f"version: {v}", count=1)
    sub("CITATION.cff", r"^date-released: .+$", f"date-released: {datetime.date.today().isoformat()}", count=1)
    sub("docker-compose.yml", r"(image: ghcr\.io/vvipinthomas/tessera:)[\w.]+", rf"\g<1>{v}")
    sub("deploy/aws/tessera-ec2.yaml", r"(Default: )v\d+\.\d+\.\d+", rf"\g<1>v{v}", count=1)
    for doc in ("README.md", "docs/index.md", "docs/DEPLOY.md"):
        sub(doc, r"releases/download/v\d+\.\d+\.\d+/tessera-\d+\.\d+\.\d+-py3", f"releases/download/v{v}/tessera-{v}-py3", required=False)
        sub(doc, r"(ghcr\.io/vvipinthomas/tessera:)\d+\.\d+\.\d+", rf"\g<1>{v}", required=False)
    for name in ("package.json", "package-lock.json"):
        p = ROOT / "cloudflare" / name
        data = json.loads(p.read_text())
        data["version"] = v
        if name == "package-lock.json" and "" in data.get("packages", {}):
            data["packages"][""]["version"] = v
        p.write_text(json.dumps(data, indent=2) + "\n")
    if f"## [{v}]" not in (ROOT / "CHANGELOG.md").read_text():
        print(f"Now add a '## [{v}] - <date>' section to CHANGELOG.md; the release notes come from it.")
    print(f"Version set to {v}. Commit and push to main to publish v{v}.")


if __name__ == "__main__":
    main()
