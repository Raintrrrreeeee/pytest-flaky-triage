"""CLI contract: python -m report DIAGNOSIS_JSON OUTPUT_DIR [--project PROJECT_DIR]."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .core import ReportContractError, load_diagnosis, render_markdown


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a Markdown test-stability triage report from diagnosis.json."
    )
    parser.add_argument("diagnosis_json", type=Path, help="Path to diagnosis.json (schema_version 1)")
    parser.add_argument("output_dir", type=Path, help="Directory where report.md will be written")
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="Optional target pytest project directory for AST source heuristics",
    )
    args = parser.parse_args(argv)

    try:
        diagnosis = load_diagnosis(args.diagnosis_json)
    except OSError as exc:
        print(f"report: cannot read input: {exc}", file=sys.stderr)
        return 3
    except (ReportContractError, json.JSONDecodeError, UnicodeError) as exc:
        print(f"report: invalid diagnosis input: {exc}", file=sys.stderr)
        return 4

    markdown = render_markdown(diagnosis, project_dir=args.project)
    temporary = None
    diag_tmp = None
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=args.output_dir,
            prefix=".report-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(markdown)
        destination = args.output_dir / "report.md"
        os.replace(temporary, destination)

        # Persist AST-disambiguated resolution back to diagnosis.json when located in output_dir
        try:
            if args.diagnosis_json.resolve().parent == args.output_dir.resolve():
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=args.output_dir,
                    prefix=".diagnosis-enriched-",
                    suffix=".tmp",
                    delete=False,
                ) as dstream:
                    diag_tmp = Path(dstream.name)
                    json.dump(diagnosis, dstream, indent=2)
                    dstream.write("\n")
                os.replace(diag_tmp, args.diagnosis_json)
        except OSError:
            pass
    except OSError as exc:
        print(f"report: cannot write output: {exc}", file=sys.stderr)
        return 5
    finally:
        for tmp_file in (temporary, diag_tmp):
            if tmp_file is not None:
                try:
                    tmp_file.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"report: could not remove temporary output {tmp_file}: {exc}", file=sys.stderr)

    print(f"report: wrote {destination} ({len(diagnosis['tests'])} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
