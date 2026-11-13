"""CLI contract: python -m analysis [RESULTS_CSV] [OUTPUT_DIR] [--results PATH] [--run-meta PATH] [--out PATH]."""

import argparse
import csv
import json
import os
from pathlib import Path
import sys
import tempfile

from . import InputError, analyse, load_records


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyse repeated pytest results; reads sibling run_meta.csv unless --run-meta is specified."
    )
    parser.add_argument("results_csv", nargs="?", type=Path, default=None, help="Path to results.csv")
    parser.add_argument(
        "output_dir",
        nargs="?",
        type=Path,
        default=None,
        help="Output directory or path to diagnosis.json",
    )
    parser.add_argument(
        "--results",
        dest="results_opt",
        type=Path,
        default=None,
        help="Path to results.csv",
    )
    parser.add_argument(
        "--run-meta",
        dest="run_meta",
        type=Path,
        default=None,
        help="Optional path to run_meta.csv (defaults to run_meta.csv alongside results.csv)",
    )
    parser.add_argument(
        "--out",
        dest="out_opt",
        type=Path,
        default=None,
        help="Output directory or direct file path ending with .json",
    )
    args = parser.parse_args(argv)

    results_path = args.results_opt if args.results_opt is not None else args.results_csv
    out_target = args.out_opt if args.out_opt is not None else args.output_dir
    if results_path is None or out_target is None:
        parser.error(
            "the following arguments are required: results_csv/--results and output_dir/--out"
        )

    try:
        if args.run_meta is not None:
            rows, runs = load_records(results_path, args.run_meta)
        else:
            rows, runs = load_records(results_path)
    except OSError as exc:
        print(f"analysis: cannot read input: {exc}", file=sys.stderr)
        return 3
    except (InputError, csv.Error, UnicodeError) as exc:
        print(f"analysis: invalid input: {exc}", file=sys.stderr)
        return 4

    diagnosis = analyse(rows, runs)
    if out_target.suffix.lower() == ".json":
        destination = out_target
        out_dir = destination.parent if str(destination.parent) != "" else Path(".")
    else:
        out_dir = out_target
        destination = out_dir / "diagnosis.json"

    temporary = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=out_dir,
            prefix=".diagnosis-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(diagnosis, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, destination)
    except OSError as exc:
        print(f"analysis: cannot write output: {exc}", file=sys.stderr)
        return 5
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                print(f"analysis: could not remove temporary output {temporary}: {exc}", file=sys.stderr)
    print(f"analysis: wrote {destination} ({len(diagnosis['tests'])} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
