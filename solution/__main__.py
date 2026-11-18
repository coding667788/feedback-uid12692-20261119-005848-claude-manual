"""Command line entry point: python -m solution --input cases.json --output result.json"""
from __future__ import annotations

import argparse
import json
import sys

from .planner import PlanError, run_document


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="solution",
        description="Offline compliance deletion planner")
    parser.add_argument("--input", required=True, help="path to cases.json")
    parser.add_argument("--output", required=True, help="path to write result.json")
    args = parser.parse_args(argv)

    try:
        with open(args.input, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        print(f"error: cannot read input file: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        result = run_document(doc)
    except PlanError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    try:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    except OSError as exc:
        print(f"error: cannot write output file: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
