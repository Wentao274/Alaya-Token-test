#!/usr/bin/env python3
"""Convenience runner that isolates each run's allure results under a timestamp.

Why this exists:
  ``pytest.ini`` historically set ``--alluredir=reports/allure-results`` (and
  ``--clean-alluredir``), so every run wrote to the SAME directory and the HTML
  report at ``reports/allure-report`` was overwritten on each run. To keep
  per-run results, this wrapper computes a timestamped run dir and passes it
  dynamically as ``--alluredir`` (and the report output dir), so each run lands
  in its own folder: ``reports/<YYYYMMDD-HHMMSS>/allure-results`` and
  ``reports/<YYYYMMDD-HHMMSS>/allure-report``.

Usage:
  python run.py            # run all tests (passes any extra args to pytest)
  python run.py -k tpm     # run only tests matching "tpm"
  python run.py --help     # show pytest help (args forwarded)

Environment variables honored:
  ALAYA_RUN_DIR   overrides the timestamped dir name (e.g. "my-run-42"); when
                   set, NO timestamp is appended (useful for re-running into a
                   fixed dir or for CI naming).
  ALAYA_SKIP_ALLURE_GEN=1  skip the allure HTML generation (the report is
                   generated HERE, after pytest exits, not inside pytest —
                   this avoids a race where the last test's result.json was
                   written after the in-process generate, dropping the last
                   test from the report).
"""

import os
import subprocess
import sys
from datetime import datetime


def _resolve_run_dir():
    """Return the run directory name (no path). Honors ALAYA_RUN_DIR override."""
    override = os.environ.get("ALAYA_RUN_DIR", "").strip()
    if override:
        return override
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _generate_allure_report(results_dir, report_dir):
    """Run ``allure generate`` to build the HTML report after pytest exits.

    Called from the parent process (run.py) AFTER the pytest subprocess
    returns, so all result JSON files are guaranteed flushed to disk. The
    previous design (a session-scope fixture in conftest that ran
    ``allure generate`` at teardown) raced with allure-pytest writing the LAST
    test's ``result.json``: the fixture teardown ran the generate before the
    last test's result file hit disk, producing a report missing the last
    test. Subprocess exit eliminates that race.
    """
    import shutil
    import subprocess

    allure_bin = shutil.which("allure")
    if not allure_bin:
        print(f"[allure] allure CLI not found; raw results in {results_dir}")
        print(
            "[allure] install it (`npm i -g allure-commandline`) then run: "
            f"allure generate {results_dir} -o {report_dir} --clean"
        )
        return
    os.makedirs(os.path.dirname(report_dir) or "reports", exist_ok=True)
    cmd = [allure_bin, "generate", results_dir, "-o", report_dir, "--clean"]
    # On Windows the npm shim is a .cmd/.bat; subprocess needs shell=True.
    use_shell = bool(os.name == "nt" and allure_bin.lower().endswith((".cmd", ".bat")))
    print(f"[allure] generating HTML report: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, shell=use_shell
        )
        print(proc.stdout)
        if proc.returncode == 0:
            print(f"[allure] report ready: {os.path.abspath(report_dir)}/index.html")
        else:
            print(
                f"[allure] generation failed (code={proc.returncode}):\n{proc.stderr}"
            )
    except subprocess.TimeoutExpired:
        print("[allure] generation timed out")


def main():
    run_dir = _resolve_run_dir()
    results_dir = os.path.join("reports", run_dir, "allure-results")
    report_dir = os.path.join("reports", run_dir, "allure-report")
    os.makedirs(results_dir, exist_ok=True)

    # Forward ALAYA_RUN_DIR to the pytest subprocess so conftest.py resolves the
    # SAME run dir for the HTML generation step (results/report stay in sync).
    env = dict(os.environ)
    env["ALAYA_RUN_DIR"] = run_dir

    # Build the pytest command: keep pytest.ini addopts EXCEPT --alluredir /
    # --clean-alluredir (those are removed from pytest.ini; we inject our own
    # dynamic --alluredir here). Extra CLI args from the user are appended last
    # so they can override anything pytest.ini sets.
    pytest_args = [
        sys.executable,
        "-m",
        "pytest",
        f"--alluredir={results_dir}",
        "--clean-alluredir",
    ]
    # Pass through any user args (e.g. -k, -m, --collect-only, file paths).
    pytest_args += sys.argv[1:]

    print(f"[run] run_dir={run_dir}")
    print(f"[run] allure-results -> {results_dir}")
    print(f"[run] allure-report  -> {report_dir} (generated after session)")
    print(f"[run] cmd: {' '.join(pytest_args)}")

    proc = subprocess.run(pytest_args, env=env)
    # The allure HTML report is generated HERE (in the parent process) AFTER the
    # pytest subprocess exits. Generating inside pytest via a session fixture
    # races with allure-pytest writing the last test's result.json — the fixture
    # teardown ran `allure generate` before the last test's result file was
    # flushed to disk, producing a report missing the last test. Generating
    # after subprocess exit guarantees all result files are on disk.
    if os.environ.get("ALAYA_SKIP_ALLURE_GEN") != "1":
        _generate_allure_report(results_dir, report_dir)
    # Propagate the exit code so CI/Make can detect failure.
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
