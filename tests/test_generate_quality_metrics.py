import sys
from pathlib import Path

# Add golden-dataset scripts to python path
_scripts_dir = Path(__file__).resolve().parent.parent / ".deepagents" / "skills" / "golden-dataset" / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from generate_quality_metrics import build_parser

reports_output_folder = "./output"


def test_generate_quality_metrics_parser_accepts_report_flag() -> None:
    parser = build_parser()

    args = parser.parse_args([f"{reports_output_folder}/golden_dataset.csv", "--report"])

    assert args.input_csv == f"{reports_output_folder}/golden_dataset.csv"
    assert args.report is True


def test_generate_quality_metrics_parser_mentions_content_report_file() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [f"{reports_output_folder}/golden_dataset.csv", "--report", "--report-file",
         f"{reports_output_folder}/content-report.txt"]
    )

    assert args.report_file == f"{reports_output_folder}/content-report.txt"
