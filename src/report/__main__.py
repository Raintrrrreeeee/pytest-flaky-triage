"""CLI contract: python -m report [DIAGNOSIS_JSON] [OUTPUT_DIR] [--diagnosis PATH] [--out PATH] [--project PROJECT_DIR] [--lang {en,zh}]."""

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
    parser.add_argument(
        "diagnosis_json",
        nargs="?",
        type=Path,
        default=None,
        help="Path to diagnosis.json (schema_version 1)",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        type=Path,
        default=None,
        help="Directory where report.md will be written, or direct .md file path",
    )
    parser.add_argument(
        "--diagnosis",
        dest="diagnosis_opt",
        type=Path,
        default=None,
        help="Path to diagnosis.json (schema_version 1)",
    )
    parser.add_argument(
        "--out",
        dest="out_opt",
        type=Path,
        default=None,
        help="Output directory or direct file path ending with .md",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="Optional target pytest project directory for AST source heuristics",
    )
    parser.add_argument(
        "--lang",
        choices=("en", "zh"),
        default="en",
        help="Report language: 'en' (English, default) or 'zh' (Simplified Chinese)",
    )
    args = parser.parse_args(argv)

    diagnosis_path = args.diagnosis_opt if args.diagnosis_opt is not None else args.diagnosis_json
    out_target = args.out_opt if args.out_opt is not None else args.output_dir
    if diagnosis_path is None or out_target is None:
        parser.error(
            "the following arguments are required: diagnosis_json/--diagnosis and output_dir/--out"
        )

    try:
        diagnosis = load_diagnosis(diagnosis_path)
    except OSError as exc:
        print(f"report: cannot read input: {exc}", file=sys.stderr)
        return 3
    except (ReportContractError, json.JSONDecodeError, UnicodeError) as exc:
        print(f"report: invalid diagnosis input: {exc}", file=sys.stderr)
        return 4

    markdown = render_markdown(diagnosis, project_dir=args.project, lang=args.lang)
    if out_target.suffix.lower() == ".md":
        destination = out_target
        out_dir = destination.parent if str(destination.parent) != "" else Path(".")
    else:
        out_dir = out_target
        destination = out_dir / "report.md"

    temporary = None
    diag_tmp = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=out_dir,
            prefix=".report-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(markdown)
        os.replace(temporary, destination)

        # Persist AST-disambiguated resolution back to diagnosis.json when located in output_dir
        try:
            if diagnosis_path.resolve().parent == out_dir.resolve():
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=out_dir,
                    prefix=".diagnosis-enriched-",
                    suffix=".tmp",
                    delete=False,
                ) as dstream:
                    diag_tmp = Path(dstream.name)
                    json.dump(diagnosis, dstream, indent=2)
                    dstream.write("\n")
                os.replace(diag_tmp, diagnosis_path)
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
