#!/usr/bin/env python3
"""Entry point of the GitHub Action: run the flows, gate on the verdicts, write outputs and the job summary.

Reads its inputs from VH_* environment variables (set by action.yml), never from the command line.
Exit 0 only when every flow passed (or cannot-tell is allowed and none failed)."""
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "truly-done" / "scripts"))
import verify  # noqa: E402


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def main() -> int:
    flows = []
    for pattern in os.environ.get("VH_FLOWS", "").split():
        flows += sorted(glob.glob(pattern, recursive=True)) or [pattern]
    if not flows:
        print("REFUSED: the flows input is empty", file=sys.stderr)
        return 3
    out = os.environ.get("VH_OUT") or "truly-done-report"
    run = ["run", *flows, "--out", out]
    if os.environ.get("VH_URL"):
        run += ["--url", os.environ["VH_URL"]]
    for host in os.environ.get("VH_ALLOW_ORIGINS", "").split():
        run += ["--allow-origin", host]
    run += ["--mine"] if truthy("VH_MINE") else []
    run += ["--offline"] if truthy("VH_OFFLINE") else []
    code = verify.main(run)
    if code == 3:
        return 3
    code = verify.main(["gate", out] + (["--allow-unknown"] if truthy("VH_ALLOW_UNKNOWN") else []))
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(out).rglob("report.json"))]
    verdict = verify.worst(r["verdict"] for r in reports) if reports else verify.UNKNOWN
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"verdict={verdict}\nflows={len(reports)}\nreport={out}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            for p in sorted(Path(out).rglob("report.md")):
                fh.write(p.read_text(encoding="utf-8").split("![final state]")[0] + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
