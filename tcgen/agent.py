"""
agent.py — the whole flowchart as one loop.

  START -> DOM + PROMPT -> FILTER -> AI(planner): ANALYSE + DEFINE FLOW OF STEPS
  for each plan step:
      AI(step) -> PATH OF TEST STEP (one action) -> EXECUTOR ENGINE -> PASS/FAIL
      PASS -> WRITE STEP INTO TESTCASE -> re-capture DOM (UPDATED FDOM)
              -> if the page changed: AI(planner) updates the remaining flow
      FAIL -> FAIL ERROR + PATH + NEW DOM -> AI(repair): ANALYSE -> new path -> executor
              -> after RETRY_LIMIT failures: HUMAN INTERVENTION (manual / retry / skip / abort)
  -> Excel + Playwright script
"""
from __future__ import annotations

import json
import time

from playwright.sync_api import sync_playwright

from .browser import open_session
from .dom_filter import capture, page_signature, render_for_llm
from .executor import Executor
from .llm import LLMError
from .prompts import (ACTION_SCHEMA, PLAN_SCHEMA, PLANNER_SYSTEM, REPAIR_EXTRA, REPLAN_SCHEMA, REPLAN_SYSTEM,
                      STEP_SYSTEM, plan_user_prompt, replan_user_prompt, step_user_prompt, validate_action,
                      validate_plan)
from .recorder import Recorder, describe_element


class Abort(Exception):
    pass


class Agent:
    def __init__(self, cfg, llm, url, request, login_required, test_data, test_data_file, run_dir):
        self.cfg = cfg
        self.llm = llm
        self.url = url
        self.request = request
        self.login_required = login_required
        self.test_data = test_data
        self.data_keys = sorted(test_data.keys())
        self.test_data_file = test_data_file
        self.run_dir = run_dir
        (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (run_dir / "snapshots").mkdir(exist_ok=True)
        self.log_file = run_dir / "run_log.jsonl"
        self.llm.logger = self._log
        self.capture_id = 0
        self.recorder = None
        self.executor = None

    # ---------------------------------------------------------------- utils
    def _log(self, entry: dict):
        entry = {"t": time.strftime("%H:%M:%S"), **entry}
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def snapshot(self, purpose: str, query: str, budget: int):
        """Capture + filter the CURRENT page. Saves JSON + the exact LLM text for inspection."""
        self.capture_id += 1
        t0 = time.time()
        snap = capture(self.executor.page, self.capture_id, self.cfg)
        ms = int((time.time() - t0) * 1000)
        text, st = render_for_llm(snap, query, budget)
        base = self.run_dir / "snapshots" / f"capture_{self.capture_id:03d}"
        base.with_suffix(".json").write_text(json.dumps(snap.to_json(), indent=1, ensure_ascii=False), encoding="utf-8")
        base.with_suffix(".llm.txt").write_text(text, encoding="utf-8")
        if self.cfg.SAVE_RAW_DOM:
            base.with_suffix(".raw.html").write_text(self.executor.page.content(), encoding="utf-8")
        raw = snap.stats.get("raw_html_chars") or 0
        rec = {
            "capture_id": self.capture_id, "purpose": purpose, "url": snap.url,
            "dom_elements": snap.stats.get("dom_elements", 0), "raw_html_chars": raw, "approx_raw_tokens": raw // 4,
            "interactive_kept": len(snap.elements), "elements_shown": st["elements_shown"],
            "llm_chars": st["chars"], "approx_llm_tokens": st["approx_tokens"],
            "reduction": f"{(1 - st['chars'] / raw) * 100:.1f}%" if raw else "", "capture_ms": ms,
        }
        self.recorder.captures.append(rec)
        print(f"   [capture {self.capture_id}] {rec['dom_elements']:,} DOM elements -> {len(snap.elements)} interactive "
              f"-> {st['elements_shown']} sent, ~{st['approx_tokens']:,} tokens (raw ~{raw // 4:,}) in {ms} ms")
        return snap, text

    # ---------------------------------------------------------------- AI calls
    def make_plan(self, page_text: str) -> dict:
        system = PLANNER_SYSTEM.replace("{max_steps}", str(self.cfg.MAX_PLAN_STEPS))
        user = plan_user_prompt(self.request, self.url, self.login_required, self.data_keys, page_text)
        for _ in range(2):
            plan = self.llm.chat_json("planner", system, user, PLAN_SCHEMA)
            err = validate_plan(plan, self.cfg.MAX_PLAN_STEPS)
            if not err:
                return plan
            user += f"\n\nYour previous answer was rejected: {err}. Fix it."
        raise Abort(f"Planner could not produce a valid plan: {err}")

    def ask_action(self, steps, i, done_actions, snap, failures):
        step = steps[i]
        query = f"{step['description']} {step['expected_result']}"
        text, _ = render_for_llm(snap, query, self.cfg.STEP_CONTEXT_TOKENS)
        role = "repair" if failures else "step"
        system = STEP_SYSTEM + (REPAIR_EXTRA if failures else "")
        user = step_user_prompt(self.recorder.title, self.request, steps, i, done_actions, self.data_keys, text, failures)
        try:
            return self.llm.chat_json(role, system, user, ACTION_SCHEMA), role
        except LLMError as exc:
            return {"_llm_error": str(exc), "action": "", "target": "", "value": "", "reason": ""}, role

    def replan(self, done_steps, remaining, snap) -> list:
        query = self.request + " " + " ".join(s["description"] for s in remaining)
        text, _ = render_for_llm(snap, query, self.cfg.PLANNER_CONTEXT_TOKENS)
        system = REPLAN_SYSTEM.replace("{max_steps}", str(max(1, self.cfg.MAX_PLAN_STEPS - len(done_steps))))
        try:
            res = self.llm.chat_json("planner", system,
                                     replan_user_prompt(self.recorder.title, self.request, done_steps, remaining, text),
                                     REPLAN_SCHEMA)
        except LLMError as exc:
            print(f"   (re-plan failed, keeping old plan: {exc})")
            return remaining
        new = [s for s in res.get("remaining_steps") or []
               if isinstance(s, dict) and str(s.get("description", "")).strip()]
        if not new and remaining:
            return remaining
        for s in new:
            s.setdefault("expected_result", "")
        return new[: max(1, self.cfg.MAX_PLAN_STEPS - len(done_steps))]

    # ---------------------------------------------------------------- human
    def human_intervention(self, step: dict, error: str) -> str:
        print("\n" + "!" * 64)
        print(f" HUMAN INTERVENTION — step failed {self.cfg.RETRY_LIMIT} times:")
        print(f"   {step['description']}")
        print(f"   last error: {error[:300]}")
        print("   m = I will do this step myself in the browser (recorded as manual)")
        print("   r = let the AI try again")
        print("   s = skip this step")
        print("   a = abort and save what we have")
        print("!" * 64)
        while True:
            c = input(" Choose [m/r/s/a]: ").strip().lower()
            if c in ("m", "r", "s", "a"):
                return c

    # ---------------------------------------------------------------- main
    def run(self) -> dict:
        with sync_playwright() as pw:
            browser, context, page, sess = open_session(pw, self.cfg, self.url, self.login_required)
            self.recorder = Recorder(self.run_dir, self.url, self.request, self.login_required, sess,
                                     self.test_data_file, self.cfg)
            self.executor = Executor(context, page, self.cfg, self.test_data)
            try:
                self._loop()
            except Abort as exc:
                self.recorder.status = "Aborted"
                print(f"\nABORTED: {exc}")
            except KeyboardInterrupt:
                self.recorder.status = "Aborted"
                print("\nStopped by user (Ctrl+C).")
            finally:
                outputs = self.recorder.save_all({k: dict(v) for k, v in self.llm.usage.items()}, self.test_data)
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass
        return {"status": self.recorder.status, "excel": outputs[0], "script": outputs[1], "json": outputs[2]}

    def _loop(self):
        cfg = self.cfg
        print("\n[1] Capturing and filtering the start page ...")
        snap, page_text = self.snapshot("plan", self.request, cfg.PLANNER_CONTEXT_TOKENS)

        print(f"[2] Planner ({cfg.MODELS['planner']}) is analysing the page and defining the flow of steps ...")
        plan = self.make_plan(page_text)
        self.recorder.set_plan(plan)
        steps = plan["steps"]
        print(f"\n   TEST CASE: {plan.get('title')}")
        for n, s in enumerate(steps, 1):
            print(f"   {n:>2}. {s['description']}")

        last_sig = page_signature(snap)
        total_actions = 0
        i = 0
        while i < len(steps):
            step = steps[i]
            print(f"\n== Step {i + 1}/{len(steps)}: {step['description']}")
            done_actions, sub_actions, step_done = [], 0, False

            while not step_done:
                if total_actions >= cfg.MAX_TOTAL_ACTIONS:
                    raise Abort(f"reached MAX_TOTAL_ACTIONS={cfg.MAX_TOTAL_ACTIONS}")
                if sub_actions >= cfg.MAX_ACTIONS_PER_STEP:
                    print(f"   step used {sub_actions} actions; moving on.")
                    break

                failures, attempt, human_done = [], 0, False
                action, source = self.ask_action(steps, i, done_actions, snap, None)
                while True:
                    attempt += 1
                    if action.get("_llm_error"):
                        verr = "LLM error: " + action["_llm_error"]
                    else:
                        verr = validate_action(action, snap, self.data_keys)
                    if verr:
                        result, ok, error = None, False, f"Invalid AI action: {verr}"
                    else:
                        result = self.executor.execute(action, snap)
                        ok, error = result["ok"], result.get("error", "")
                    self.recorder.add_log(i, attempt, source, action, ok, error)
                    el_desc = describe_element(snap.get(action.get("target", "")))
                    print(f"   [{source} #{attempt}] {action.get('action')} {action.get('target', '')} {el_desc} "
                          f"{('= ' + repr(action.get('value'))) if action.get('value') else ''} -> {'PASS' if ok else 'FAIL'}")
                    if ok:
                        break
                    print(f"      error: {error[:200]}")
                    failures.append(f"{action.get('action')} target={action.get('target')} {el_desc} "
                                    f"value={action.get('value')!r} -> ERROR: {error}")

                    if attempt >= cfg.RETRY_LIMIT:
                        choice = self.human_intervention(step, error)
                        if choice == "a":
                            raise Abort("aborted by user")
                        if choice in ("m", "s"):
                            if choice == "m":
                                input("   Do the step in the Firefox window now, then press ENTER... ")
                                self.executor.settle()
                            shot = self.executor.screenshot(self.run_dir / "screenshots" / f"step_{i + 1:02d}_{choice}.png")
                            self.recorder.add_step(i, step["description"], step["expected_result"],
                                                   {"action": "manual" if choice == "m" else "skip"}, attempt, shot,
                                                   status="Manual" if choice == "m" else "Skipped",
                                                   note="Performed by human" if choice == "m" else "Skipped by user")
                            human_done = True
                            break
                        attempt = cfg.RETRY_LIMIT - 1      # 'r': one more AI attempt

                    # FAIL ERROR + PATH + NEW DOM -> AI ANALYSE
                    snap, _ = self.snapshot("repair", step["description"], cfg.STEP_CONTEXT_TOKENS)
                    action, source = self.ask_action(steps, i, done_actions, snap, failures)

                if human_done:
                    snap, _ = self.snapshot("after-human", self.request, cfg.STEP_CONTEXT_TOKENS)
                    last_sig = page_signature(snap)
                    break

                # PASS -> WRITE STEP INTO TESTCASE
                total_actions += 1
                sub_actions += 1
                name = action["action"]
                shot = self.executor.screenshot(self.run_dir / "screenshots" / f"step_{i + 1:02d}_{sub_actions}.png")
                self.recorder.add_step(i, step["description"], step["expected_result"], result, attempt, shot,
                                       status="Skipped" if name == "skip" else "Passed",
                                       note=action.get("reason", "") if name == "skip" else "")
                done_actions.append(f"{name} {describe_element(result.get('element'))} "
                                    f"{('value=' + repr(action.get('value'))) if action.get('value') else ''}".strip())
                step_done = bool(action.get("completes_step")) or name.startswith("assert") or name in ("skip", "navigate")

                # UPDATED FDOM (and updated flow of steps if the page changed)
                snap, _ = self.snapshot("after-step", step["description"], cfg.STEP_CONTEXT_TOKENS)
                sig = page_signature(snap)
                if cfg.REPLAN_ON_URL_CHANGE and step_done and sig != last_sig and i + 1 < len(steps):
                    print(f"   page changed -> {snap.url}; planner is updating the remaining steps ...")
                    new_rest = self.replan(steps[: i + 1], steps[i + 1:], snap)
                    if [s["description"] for s in new_rest] != [s["description"] for s in steps[i + 1:]]:
                        steps = steps[: i + 1] + new_rest
                        for n, s in enumerate(new_rest, i + 2):
                            print(f"   {n:>2}. {s['description']}")
                    else:
                        print("   remaining steps unchanged.")
                last_sig = sig
            i += 1

        statuses = {s["status"] for s in self.recorder.steps}
        self.recorder.status = "Passed" if statuses <= {"Passed"} else "Passed with manual/skipped steps"
