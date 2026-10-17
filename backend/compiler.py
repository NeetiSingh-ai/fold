"""The Fold compiler: logged Gemini calls in, a verified zero-cost function out.

Pipeline
  1. ANALYSE   Gemini studies a sample of real (input, output) pairs and decides whether
               the task is deterministic enough to be expressed as code.
  2. GENERATE  Gemini writes a pure-Python run(text) -> dict | None. Returning None means
               "not sure", which routes that request back to Gemini at runtime.
  3. VERIFY    The function is run in the sandbox against the training pairs; failures are
               fed back to Gemini to repair (up to MAX_REPAIRS rounds).
  4. TEST      Final precision and coverage are measured on held-out calls it never saw.
  5. ACTIVATE  Only if precision >= PRECISION_GATE and coverage >= COVERAGE_GATE.
"""
import json
import os
import random
import time

import sandbox
from gemini_client import COMPILER_MODEL, generate_json

PRECISION_GATE = float(os.getenv("PRECISION_GATE", "0.98"))
COVERAGE_GATE = float(os.getenv("COVERAGE_GATE", "0.5"))
MIN_EXAMPLES = int(os.getenv("MIN_EXAMPLES", "30"))
MAX_REPAIRS = int(os.getenv("MAX_REPAIRS", "3"))


def _norm(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    if isinstance(v, str):
        s = " ".join(v.strip().split()).casefold()
        try:
            return round(float(s.replace(",", "")), 2)
        except ValueError:
            return s
    return json.dumps(v, sort_keys=True)


def same(expected: dict, got: dict) -> bool:
    if not isinstance(got, dict):
        return False
    for k, ev in expected.items():
        gv = got.get(k)
        ne, ng = _norm(ev), _norm(gv)
        if isinstance(ne, float) and isinstance(ng, float):
            if abs(ne - ng) > 0.011:
                return False
        elif ne != ng:
            return False
    return True


def evaluate(fn, examples):
    answered, correct, failures, skipped = 0, 0, [], []
    for ex in examples:
        out, err = sandbox.call(fn, ex["input"])
        if out is None:
            if len(skipped) < 6:
                skipped.append({"input": ex["input"][:1200], "expected": ex["output"],
                                "got": f"None (error: {err})" if err else "None (code declined)"})
            continue
        answered += 1
        if same(ex["output"], out):
            correct += 1
        elif len(failures) < 12:
            failures.append({"input": ex["input"][:1200], "expected": ex["output"], "got": out})
    n = len(examples) or 1
    cov = answered / n
    # If the code answers too little, show Gemini the cases it declined or crashed on.
    if cov < 0.85:
        failures = failures[:8] + skipped
    return {"n": len(examples), "answered": answered, "correct": correct,
            "coverage": cov, "precision": (correct / answered) if answered else 0.0,
            "failures": failures}


def _examples_block(examples, k):
    return "\n\n".join(f"### INPUT\n{e['input']}\n### OUTPUT\n{json.dumps(e['output'], ensure_ascii=False)}"
                       for e in examples[:k])


def analyse(task, examples):
    prompt = f"""You are Fold, a compiler that replaces repetitive LLM calls with ordinary code.
Below is a production LLM task and {min(len(examples), 20)} real input/output pairs logged from it.

TASK: {task['name']} - {task['description']}
OUTPUT SCHEMA: {json.dumps(task['schema'])}

{_examples_block(examples, 20)}

Decide whether a deterministic Python function using only regex/string/date/number logic and
keyword/lookup tables could reproduce these outputs.
Important: the function does NOT need to handle every possible input. It may return None for anything
unfamiliar or ambiguous, and those requests automatically go back to the LLM. It only needs to be
correct (>98%) on the inputs it chooses to answer, and to answer a meaningful share (>50%) of traffic
like the examples above. Judge compilability on that basis.
Only free-form generated text (replies, summaries, creative writing, open-ended reasoning) is NOT compilable.
Return ONLY JSON:
{{"compilable": true|false, "confidence": 0-1, "pattern": "one sentence on the regularity you found",
  "strategy": "how the code should work (or why it can't)", "risks": "inputs the code should hand back to the LLM"}}"""
    return generate_json(prompt, model=COMPILER_MODEL)


_RULES = """Rules for the code:
- Define exactly: def run(text: str) -> dict | None
- Use only these modules: re, datetime, json, math, string, unicodedata, decimal. No other imports.
- No file, network, eval/exec, getattr, or attribute names starting with underscore.
- Return a dict with exactly the schema keys, using the same formats/normalisation as the examples.
- If the input does not clearly match a pattern you handle, RETURN None (the request then goes to the LLM).
  Prefer returning None over guessing: a wrong answer is far worse than a fallback.
- Keep it under 150 lines, readable, with short comments."""


def generate(task, examples, analysis):
    prompt = f"""You are Fold. Write a Python function that reproduces this LLM task's outputs.

TASK: {task['name']} - {task['description']}
OUTPUT SCHEMA: {json.dumps(task['schema'])}
PATTERN FOUND: {analysis.get('pattern')}
STRATEGY: {analysis.get('strategy')}

{_RULES}

EXAMPLES (real logged LLM calls):
{_examples_block(examples, 60)}

Return ONLY JSON: {{"code": "<the full python source>", "notes": "one line"}}"""
    return generate_json(prompt, model=COMPILER_MODEL)


def repair(task, code, failures, error=None):
    fails = "\n\n".join(f"INPUT:\n{f['input']}\nEXPECTED: {json.dumps(f['expected'], ensure_ascii=False)}\n"
                        f"GOT: {json.dumps(f['got'], ensure_ascii=False)}" for f in failures)
    prompt = f"""You are Fold. Your function for '{task['name']}' has problems. Fix them.
OUTPUT SCHEMA: {json.dumps(task['schema'])}
{_RULES}

CURRENT CODE:
{code}

{('ERROR: ' + error) if error else 'PROBLEM CASES (fix wrong answers; make it handle declined/crashed cases it can answer reliably; return None only when truly ambiguous):'}
{fails}

Return ONLY JSON: {{"code": "<the full corrected python source>", "notes": "what you changed"}}"""
    return generate_json(prompt, model=COMPILER_MODEL)


def run_compile(store, task_id, job_id):
    t_start = time.time()
    spent = {"cost_usd": 0.0}
    steps = []

    def step(name, status, detail=None, **extra):
        if steps and steps[-1]["name"] == name and steps[-1]["status"] == "running":
            steps.pop()
        steps.append({"name": name, "status": status, "detail": detail, "t": round(time.time() - t_start, 1)})
        store.set_job(job_id, {"steps": steps, "state": "running", **extra})

    def llm(fn, *a):
        out, usage = fn(*a)
        spent["cost_usd"] += usage["cost_usd"]
        return out

    task = store.get_task(task_id)
    data = [d for d in store.training_set(task_id) if isinstance(d.get("output"), dict)]
    if len(data) < MIN_EXAMPLES:
        store.set_job(job_id, {"state": "error",
                               "error": f"Need at least {MIN_EXAMPLES} logged Gemini calls; have {len(data)}. "
                                        f"Send more traffic first."})
        return
    random.Random(42).shuffle(data)
    cut = max(int(len(data) * 0.6), MIN_EXAMPLES // 2)
    train, test = data[:cut], data[cut:]
    step("Collect", "done", f"{len(data)} logged Gemini calls: {len(train)} to learn from, {len(test)} held out")

    step("Analyse", "running", "Gemini is looking for a deterministic pattern")
    analysis = llm(analyse, task, train)
    step("Analyse", "done", analysis.get("pattern"), analysis=analysis)

    if not analysis.get("compilable"):
        store.update_task(task_id, {"status": "not_compilable", "analysis": analysis})
        store.save_compiled(task_id, {"task_id": task_id, "active": False, "analysis": analysis,
                                      "verdict": "kept_on_llm", "created": time.time(),
                                      "compile_cost_usd": spent["cost_usd"]})
        step("Verdict", "done", "Not compilable: this task needs real generation, so it stays on Gemini.")
        store.set_job(job_id, {"state": "done", "verdict": "kept_on_llm", "compile_cost_usd": spent["cost_usd"]})
        return

    step("Generate", "running", "Gemini is writing the function")
    code = llm(generate, task, train, analysis).get("code", "")
    step("Generate", "done", f"{len(code.splitlines())} lines of Python", code=code)

    # Keep the best version seen so far: a repair can never make the result worse.
    res, best = None, None

    def score(r):
        # precision matters most (wrong answers are costly); coverage breaks ties
        return (round(r["precision"], 3), r["coverage"])

    for attempt in range(MAX_REPAIRS + 1):
        try:
            fn = sandbox.load(code)
        except Exception as e:  # noqa: BLE001
            step(f"Verify #{attempt + 1}", "failed", f"Rejected by sandbox: {e}")
            if attempt == MAX_REPAIRS:
                break
            code = llm(repair, task, best[0] if best else code, [], str(e)).get("code", "")
            continue
        res = evaluate(fn, train)
        improved = best is None or score(res) > score(best[1])
        if improved:
            best = (code, res)
        step(f"Verify #{attempt + 1}", "done",
             f"precision {res['precision']:.1%}, coverage {res['coverage']:.1%} on {res['n']} training calls"
             + ("" if improved else " (worse, so Fold keeps the previous best)"))
        store.set_job(job_id, {"code": best[0]})
        b = best[1]
        if (b["precision"] >= 0.995 and b["coverage"] >= 0.85) or attempt == MAX_REPAIRS:
            break
        step(f"Repair #{attempt + 1}", "running", f"Sending {len(b['failures'])} mismatches back to Gemini")
        code = llm(repair, task, best[0], b["failures"]).get("code", "")
    if best:
        code, res = best

    try:
        fn = sandbox.load(code)
        test_res = evaluate(fn, test)
    except Exception as e:  # noqa: BLE001
        test_res = {"n": len(test), "answered": 0, "correct": 0, "coverage": 0, "precision": 0,
                    "failures": [], "error": str(e)}
    passed = test_res["precision"] >= PRECISION_GATE and test_res["coverage"] >= COVERAGE_GATE
    stats = task.get("stats", {})
    avg_cost = (stats.get("cost_usd", 0) / stats["llm_calls"]) if stats.get("llm_calls") else 0
    monthly = avg_cost * test_res["coverage"] * 1_000_000
    step("Test", "done" if passed else "failed",
         f"Held-out: precision {test_res['precision']:.1%}, coverage {test_res['coverage']:.1%} "
         f"on {test_res['n']} unseen calls (gate: {PRECISION_GATE:.0%} / {COVERAGE_GATE:.0%})")

    version = (task.get("compiled_version") or 0) + 1
    doc = {"task_id": task_id, "version": version, "code": code, "analysis": analysis, "test": test_res,
           "train": {k: v for k, v in (res or {}).items() if k != "failures"},
           "active": passed, "verdict": "compiled" if passed else "rejected", "created": time.time(),
           "compile_cost_usd": spent["cost_usd"], "avg_llm_cost_usd": avg_cost,
           "projected_monthly_saving_usd_per_1m_calls": monthly}
    store.save_compiled(task_id, doc)
    if passed:
        store.update_task(task_id, {"status": "compiled", "compiled_version": version,
                                    "coverage": test_res["coverage"], "precision": test_res["precision"],
                                    "shadow_base": stats.get("shadow_checks", 0),
                                    "shadow_agree_base": stats.get("shadow_agree", 0)})
        step("Activate", "done", f"Live. ~{test_res['coverage']:.0%} of calls now run as free code; "
                                 f"the rest still go to Gemini.")
    else:
        step("Activate", "failed", "Did not pass the quality gate, so the task stays on Gemini.")
    store.set_job(job_id, {"state": "done", "verdict": doc["verdict"], "compile_cost_usd": spent["cost_usd"],
                           "test": {k: v for k, v in test_res.items() if k != "failures"},
                           "projected": monthly})
