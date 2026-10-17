"""Fold API (Cloud Run).

POST /v1/run                      drop-in gateway: {"task": "...", "input": "..."} -> output + route
POST /api/tasks/{id}/simulate     send N synthetic production calls through the gateway (background job)
POST /api/tasks/{id}/compile      run the Fold compiler on that task's logged calls (background job)
POST /api/tasks/{id}/revert       send the task back to Gemini
GET  /api/tasks/{id}/sample       a realistic sample input for the playground
GET  /api/state                   dashboard state (fallback when the Firestore listener isn't available)
GET  /api/compiled/{id}           compiled function + test report
POST /api/reset                   wipe demo data (needs X-Admin-Token)
"""
import concurrent.futures as cf
import os
import random
import threading
import time
import uuid

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

if os.getenv("MOCK_GEMINI") == "1":
    import mock_gemini  # noqa: F401  (local tests only)
import compiler
import sandbox
from gemini_client import MODEL, generate_json
from store import make_store
from tasks import TASKS, sample

# Google's published median for one Gemini Apps text prompt (Aug 2025): 0.24 Wh, 0.03 gCO2e.
WH_PER_CALL = float(os.getenv("WH_PER_CALL", "0.24"))
CO2_G_PER_CALL = float(os.getenv("CO2_G_PER_CALL", "0.03"))
SHADOW_RATE = float(os.getenv("SHADOW_RATE", "0.1"))       # share of compiled calls re-checked by Gemini
SHADOW_MIN_AGREE = float(os.getenv("SHADOW_MIN_AGREE", "0.95"))
DAILY_LLM_CAP = int(os.getenv("DAILY_LLM_CAP", "4000"))    # protects the demo budget
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

app = FastAPI(title="Fold", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
store = make_store()
store.ensure_tasks(TASKS)

_fn_cache = {}          # task_id -> (version, fn)
_cap = {"day": time.strftime("%Y-%m-%d"), "n": 0}
_cap_lock = threading.Lock()
_bg = cf.ThreadPoolExecutor(max_workers=8)


def _allow_llm():
    with _cap_lock:
        today = time.strftime("%Y-%m-%d")
        if _cap["day"] != today:
            _cap.update(day=today, n=0)
        if _cap["n"] >= DAILY_LLM_CAP:
            return False
        _cap["n"] += 1
        return True


def _compiled_fn(task):
    v = task.get("compiled_version")
    hit = _fn_cache.get(task["id"])
    if hit and hit[0] == v:
        return hit[1]
    doc = store.get_compiled(task["id"])
    if not doc or not doc.get("active"):
        return None
    fn = sandbox.load(doc["code"])
    _fn_cache[task["id"]] = (v, fn)
    return fn


def _call_llm(task_id, text):
    if not _allow_llm():
        raise HTTPException(429, "Daily demo budget reached, try again tomorrow.")
    prompt = TASKS[task_id]["prompt"].format(input=text)
    out, usage = generate_json(prompt)
    return out, usage


def _avg_cost(task):
    s = task.get("stats", {})
    return (s.get("cost_usd", 0) / s["llm_calls"]) if s.get("llm_calls") else 0.0


def _shadow(task, text, compiled_out):
    """Re-check a sample of compiled answers against Gemini; auto-revert if they drift."""
    try:
        out, usage = _call_llm(task["id"], text)
    except Exception:  # noqa: BLE001
        return
    agree = compiler.same(out, compiled_out)
    store.add_training(task["id"], {"input": text, "output": out, "ts": time.time()})
    store.incr(task["id"], {"shadow_checks": 1, "shadow_agree": 1 if agree else 0,
                            "cost_usd": usage["cost_usd"], "tokens_in": usage["tokens_in"],
                            "tokens_out": usage["tokens_out"]})
    t = store.get_task(task["id"])
    s = t["stats"]
    checks = s.get("shadow_checks", 0) - t.get("shadow_base", 0)
    agreed = s.get("shadow_agree", 0) - t.get("shadow_agree_base", 0)
    if checks >= 20 and agreed / checks < SHADOW_MIN_AGREE and t.get("status") == "compiled":
        store.update_task(task["id"], {"status": "llm", "reverted_reason":
                                       f"Shadow checks agreed only {agreed}/{checks}; reverted to Gemini."})


def run_task(task_id: str, text: str, source: str = "api"):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, f"unknown task {task_id}")
    t0 = time.time()
    route, out, usage, err = "llm", None, None, None
    if task.get("status") == "compiled":
        try:
            fn = _compiled_fn(task)
        except Exception as e:  # noqa: BLE001
            fn, err = None, str(e)
        if fn:
            out, err = sandbox.call(fn, text)
            route = "compiled" if out is not None else "fallback"
    if out is None:
        out, usage = _call_llm(task_id, text)
        store.add_training(task_id, {"input": text, "output": out, "ts": time.time()})
    latency = int((time.time() - t0) * 1000)
    if route == "compiled":
        saved = _avg_cost(task)
        counters = {"calls": 1, "compiled_calls": 1, "saved_usd": saved,
                    "energy_wh_saved": WH_PER_CALL, "co2_g_saved": CO2_G_PER_CALL}
        cost = 0.0
        if random.random() < SHADOW_RATE:
            _bg.submit(_shadow, task, text, out)
    else:
        cost = usage["cost_usd"]
        counters = {"calls": 1, "llm_calls": 1, "cost_usd": cost, "tokens_in": usage["tokens_in"],
                    "tokens_out": usage["tokens_out"]}
        if route == "fallback":
            counters["fallback_calls"] = 1
    store.incr(task_id, counters)
    store.add_log({"ts": time.time(), "task_id": task_id, "route": route, "cost_usd": cost,
                   "latency_ms": latency, "source": source, "input": text[:160], "error": err})
    return {"task": task_id, "route": route, "output": out, "cost_usd": cost, "latency_ms": latency,
            "model": None if route == "compiled" else MODEL}


# ------------------------------------------------------------------ API
class RunReq(BaseModel):
    task: str
    input: str


class SimReq(BaseModel):
    n: int = 40


@app.get("/api/health")
def health():
    return {"ok": True, "model": MODEL}


@app.post("/v1/run")
@app.post("/api/run")
def api_run(req: RunReq):
    if len(req.input) > 6000:
        raise HTTPException(413, "input too long")
    return run_task(req.task, req.input, source="playground")


def _simulate(task_id, n, job_id):
    done = {"n": 0, "routes": {"llm": 0, "compiled": 0, "fallback": 0}}
    store.set_job(job_id, {"type": "simulate", "task_id": task_id, "state": "running", "total": n, "done": 0})
    lock = threading.Lock()

    def one(i):
        text, _ = sample(task_id, seed=None)
        try:
            r = run_task(task_id, text, source="traffic")
            route = r["route"]
        except Exception as e:  # noqa: BLE001
            print(f"simulate call failed: {type(e).__name__}: {e}", flush=True)
            route = "error"
        with lock:
            done["n"] += 1
            done["routes"][route] = done["routes"].get(route, 0) + 1
            if done["n"] % 5 == 0 or done["n"] == n:
                store.set_job(job_id, {"done": done["n"], "routes": done["routes"]})

    with cf.ThreadPoolExecutor(max_workers=int(os.getenv("SIM_CONCURRENCY", "5"))) as ex:
        list(ex.map(one, range(n)))
    store.set_job(job_id, {"state": "done", "done": n, "routes": done["routes"]})


@app.post("/api/tasks/{task_id}/simulate")
def api_simulate(task_id: str, req: SimReq, bg: BackgroundTasks):
    if task_id not in TASKS:
        raise HTTPException(404)
    n = max(1, min(req.n, 80))
    job_id = uuid.uuid4().hex[:12]
    store.set_job(job_id, {"type": "simulate", "task_id": task_id, "state": "queued", "total": n, "done": 0,
                           "created": time.time()})
    bg.add_task(_simulate, task_id, n, job_id)
    return {"job_id": job_id}


@app.post("/api/tasks/{task_id}/compile")
def api_compile(task_id: str, bg: BackgroundTasks):
    if task_id not in TASKS:
        raise HTTPException(404)
    job_id = uuid.uuid4().hex[:12]
    store.set_job(job_id, {"type": "compile", "task_id": task_id, "state": "queued", "steps": [],
                           "created": time.time()})

    def go():
        try:
            compiler.run_compile(store, task_id, job_id)
        except Exception as e:  # noqa: BLE001
            store.set_job(job_id, {"state": "error", "error": f"{type(e).__name__}: {e}"})

    bg.add_task(go)
    return {"job_id": job_id}


@app.post("/api/tasks/{task_id}/revert")
def api_revert(task_id: str):
    store.update_task(task_id, {"status": "llm"})
    return {"ok": True}


@app.get("/api/tasks/{task_id}/sample")
def api_sample(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(404)
    text, _ = sample(task_id)
    return {"input": text}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    return store.get_job(job_id) or {}


@app.get("/api/compiled/{task_id}")
def api_compiled(task_id: str):
    return store.get_compiled(task_id) or {}


@app.get("/api/state")
def api_state():
    return {"global": store.get_global(), "tasks": store.list_tasks(), "logs": store.recent_logs(15)}


@app.post("/api/reset")
def api_reset(x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "admin token required")
    store.reset_all()
    store.ensure_tasks(TASKS)
    _fn_cache.clear()
    return {"ok": True}


# Local dev convenience: serve the frontend from the same process.
_FRONT = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.getenv("SERVE_FRONTEND") == "1":
    @app.get("/")
    def index():
        return FileResponse(os.path.join(_FRONT, "index.html"))

    @app.get("/{name}")
    def static(name: str):
        p = os.path.join(_FRONT, name)
        if os.path.isfile(p):
            return FileResponse(p)
        raise HTTPException(404)
