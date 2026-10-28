#!/usr/bin/env python3
"""Record and verify immutable datasets before/after AskData E integration."""
from pathlib import Path
import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / "data/homenova_phase_e/private"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected():
    files = set()
    for stage in "abcd":
        for folder in (ROOT / f"data/homenova_phase_{stage}", ROOT / f"scripts/homenova_phase_{stage}"):
            files.update(path for path in folder.rglob("*") if path.is_file()
                         and "__pycache__" not in path.parts and path.name != ".DS_Store")
        files.add(ROOT / f"docs/homenova_phase_{stage}_report.md")
    files.add(ROOT / "docs/homenova_storyline_manual.md")
    for app in ("askdata_studio", "askdata_studio_ops_release"):
        data = ROOT / f"references/{app}/backend/data"
        for folder in (data / "databases",):
            files.update(path for path in folder.rglob("*") if path.is_file())
        index = data / "schema_store.json"
        if index.exists():
            files.add(index)
    return sorted(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("before", "after"))
    args = parser.parse_args()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    manifest = PRIVATE / "protected_before.json"
    if args.mode == "before" and not manifest.exists():
        files = {str(path.relative_to(ROOT)): digest(path) for path in protected()}
        manifest.write_text(json.dumps({"sha256": files}, indent=2) + "\n")
        print(json.dumps({"recorded_files": len(files)}))
        return
    before = json.loads(manifest.read_text())["sha256"]
    changed, missing = [], []
    for name, expected in before.items():
        path = ROOT / name
        if not path.exists():
            missing.append(name)
        elif digest(path) != expected:
            changed.append(name)
    result = {"passed": not changed and not missing, "files_checked": len(before),
              "changed": changed, "missing": missing}
    (PRIVATE / "protected_files_check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
