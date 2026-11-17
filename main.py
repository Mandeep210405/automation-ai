"""
main.py — start here.

    python main.py
    python main.py --url https://example.com --prompt "Verify the More information link opens IANA page" --login no
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config
from tcgen.agent import Agent
from tcgen.browser import ask_yes_no
from tcgen.llm import OllamaClient

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return s[:40] or "test"


def load_test_data(path: Path) -> dict:
    if not path.exists():
        path.write_text("{}\n", encoding="utf-8")
        print(f"Created empty {path}. Put values like email/password there; the AI only sees the key names.")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        sys.exit(f"{path} must contain a JSON object like {{\"email\": \"...\"}}")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a working UI test case with Firefox + a local Ollama model.")
    ap.add_argument("--url", help="start URL of the test")
    ap.add_argument("--prompt", help="what test case you want, in plain English")
    ap.add_argument("--login", choices=["yes", "no"], help="does the site need login before the test?")
    ap.add_argument("--data", default=config.TEST_DATA_FILE, help="test data JSON file (default: test_data.json)")
    args = ap.parse_args()

    url = (args.url or input("URL to test: ")).strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    prompt = (args.prompt or input("Describe the test case you want: ")).strip()
    if not prompt:
        sys.exit("A test case description is required.")
    login = (args.login == "yes") if args.login else ask_yes_no("Does this site require login before the test?", False)

    data_file = Path(args.data)
    test_data = load_test_data(data_file)

    llm = OllamaClient(config)
    problems = llm.check()
    if problems:
        print("\nSetup problem:")
        for p in problems:
            print("  -", p)
        return 1

    run_dir = Path(config.RUNS_DIR) / f"{datetime.now():%Y%m%d_%H%M%S}_{slug(prompt)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nRun folder: {run_dir}")
    print(f"Models: {config.MODELS}")

    out = Agent(config, llm, url, prompt, login, test_data, data_file, run_dir).run()

    print("\n" + "=" * 64)
    print(f" STATUS : {out['status']}")
    print(f" Excel  : {out['excel']}")
    print(f" Script : {out['script']}")
    print(f" JSON   : {out['json']}")
    for role, u in llm.usage.items():
        print(f" LLM {role:<8}: {u['calls']} calls, {u['prompt_tokens']:,} prompt + {u['output_tokens']:,} output tokens, "
              f"{u['seconds']:.0f}s")
        tot_in = sum(u["prompt_tokens"] for u in llm.usage.values())
        tot_out = sum(u["output_tokens"] for u in llm.usage.values())
        print(f" TOTAL TOKENS : input {tot_in:,} | output {tot_out:,} | total {tot_in + tot_out:,}")
    print("=" * 64)

    if out["status"] != "Aborted" and ask_yes_no("Replay the generated script now to verify it?", False):
        r = subprocess.run([sys.executable, str(Path(out["script"]).resolve())])
        print("Replay:", "PASSED" if r.returncode == 0 else f"FAILED (exit code {r.returncode})")

    if out["status"] != "Aborted" and ask_yes_no("Generate the Automation Master Controller xlsx (formatted, XPath locators)?", True):
        from tcgen.master_export import export_from_json
        master, warns = export_from_json(out["json"], data_file, Path(out["json"]).parent / "Automation_Master_Controller.xlsx", config)
        print(f" Master xlsx: {master}")
        for w in warns:
            print("   !", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
