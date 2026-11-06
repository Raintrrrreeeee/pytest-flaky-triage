"""CLI contract: python -m analysis RESULTS_CSV OUTPUT_DIR."""

import argparse
import csv
import json
import os
from pathlib import Path
import sys
import tempfile

from . import InputError, analyse, load_records


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyse repeated pytest results; reads sibling run_meta.csv.")
    parser.add_argument("results_csv", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        rows, runs = load_records(args.results_csv)
    except OSError as exc:
        print(f"analysis: cannot read input: {exc}", file=sys.stderr)
        return 3
    except (InputError, csv.Error, UnicodeError) as exc:
        print(f"analysis: invalid input: {exc}", file=sys.stderr)
        return 4

    diagnosis = analyse(rows, runs)
    temporary = None
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output_dir, prefix=".diagnosis-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(diagnosis, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        destination = args.output_dir / "diagnosis.json"
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
