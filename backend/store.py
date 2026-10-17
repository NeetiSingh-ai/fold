"""Persistence: Firestore in production, in-memory for local tests (STORE=memory).

Firestore layout
  meta/global                    running totals for the whole gateway (dashboard KPIs)
  tasks/{taskId}                 task definition, status (llm | compiled | not_compilable), stats
  tasks/{taskId}/training/{id}   every Gemini answer, the dataset Fold compiles from
  logs/{id}                      live request log (route, cost, latency) for the dashboard feed
  compiled/{taskId}              current compiled function, test results, Gemini's analysis
  jobs/{jobId}                   progress of long-running jobs (simulate / compile), streamed to UI
"""
import os
import threading
import time
import uuid

ZERO_STATS = {"calls": 0, "llm_calls": 0, "compiled_calls": 0, "fallback_calls": 0, "cost_usd": 0.0,
              "saved_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "shadow_checks": 0, "shadow_agree": 0,
              "energy_wh_saved": 0.0, "co2_g_saved": 0.0}


class MemoryStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset_all()

    def reset_all(self):
        self.tasks, self.training, self.logs, self.compiled, self.jobs = {}, {}, [], {}, {}
        self.glob = dict(ZERO_STATS)

    def ensure_tasks(self, defs):
        for tid, d in defs.items():
            if tid not in self.tasks:
                self.tasks[tid] = {"id": tid, "name": d["name"], "description": d["description"],
                                   "schema": d["schema"], "status": "llm", "stats": dict(ZERO_STATS),
                                   "compiled_version": 0, "updated": time.time()}

    def get_task(self, tid):
        return self.tasks.get(tid)

    def list_tasks(self):
        return list(self.tasks.values())

    def update_task(self, tid, fields):
        with self.lock:
            self.tasks[tid].update(fields)
            self.tasks[tid]["updated"] = time.time()

    def incr(self, tid, counters):
        with self.lock:
            for k, v in counters.items():
                self.tasks[tid]["stats"][k] = self.tasks[tid]["stats"].get(k, 0) + v
                self.glob[k] = self.glob.get(k, 0) + v

    def add_training(self, tid, rec):
        with self.lock:
            self.training.setdefault(tid, []).append(rec)

    def training_set(self, tid, limit=400):
        return list(self.training.get(tid, []))[-limit:]

    def add_log(self, rec):
        with self.lock:
            self.logs.append(rec)
            self.logs = self.logs[-200:]

    def recent_logs(self, limit=15):
        return list(reversed(self.logs[-limit:]))

    def save_compiled(self, tid, doc):
        self.compiled[tid] = doc

    def get_compiled(self, tid):
        return self.compiled.get(tid)

    def set_job(self, jid, fields):
        with self.lock:
            self.jobs.setdefault(jid, {"id": jid}).update(fields)

    def get_job(self, jid):
        return self.jobs.get(jid)

    def get_global(self):
        return dict(self.glob)

    def reset_task(self, tid):
        self.tasks[tid].update({"status": "llm", "stats": dict(ZERO_STATS), "compiled_version": 0})
        self.training.pop(tid, None)
        self.compiled.pop(tid, None)


class FirestoreStore:
    def __init__(self):
        from google.cloud import firestore
        self.fs = firestore
        self.db = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
                                   database=os.getenv("FIRESTORE_DB", "(default)"))

    def _t(self, tid):
        return self.db.collection("tasks").document(tid)

    def ensure_tasks(self, defs):
        g = self.db.collection("meta").document("global")
        if not g.get().exists:
            g.set(dict(ZERO_STATS))
        for tid, d in defs.items():
            ref = self._t(tid)
            snap = ref.get()
            base = {"id": tid, "name": d["name"], "description": d["description"], "schema": d["schema"]}
            if not snap.exists:
                ref.set({**base, "status": "llm", "stats": dict(ZERO_STATS), "compiled_version": 0,
                         "updated": time.time()})
            else:
                ref.set(base, merge=True)

    def get_task(self, tid):
        s = self._t(tid).get()
        return s.to_dict() if s.exists else None

    def list_tasks(self):
        return [s.to_dict() for s in self.db.collection("tasks").stream()]

    def update_task(self, tid, fields):
        self._t(tid).set({**fields, "updated": time.time()}, merge=True)

    def incr(self, tid, counters):
        inc = self.fs.Increment
        b = self.db.batch()
        b.update(self._t(tid), {f"stats.{k}": inc(v) for k, v in counters.items()})
        b.update(self.db.collection("meta").document("global"), {k: inc(v) for k, v in counters.items()})
        b.commit()

    def add_training(self, tid, rec):
        self._t(tid).collection("training").document(uuid.uuid4().hex).set(rec)

    def training_set(self, tid, limit=400):
        return [s.to_dict() for s in self._t(tid).collection("training").limit(limit).stream()]

    def add_log(self, rec):
        self.db.collection("logs").document(uuid.uuid4().hex).set(rec)

    def recent_logs(self, limit=15):
        q = self.db.collection("logs").order_by("ts", direction=self.fs.Query.DESCENDING).limit(limit)
        return [s.to_dict() for s in q.stream()]

    def save_compiled(self, tid, doc):
        self.db.collection("compiled").document(tid).set(doc)

    def get_compiled(self, tid):
        s = self.db.collection("compiled").document(tid).get()
        return s.to_dict() if s.exists else None

    def set_job(self, jid, fields):
        self.db.collection("jobs").document(jid).set({"id": jid, **fields}, merge=True)

    def get_job(self, jid):
        s = self.db.collection("jobs").document(jid).get()
        return s.to_dict() if s.exists else None

    def get_global(self):
        s = self.db.collection("meta").document("global").get()
        return s.to_dict() if s.exists else dict(ZERO_STATS)

    def _wipe(self, coll):
        while True:
            docs = list(coll.limit(300).stream())
            if not docs:
                return
            b = self.db.batch()
            for d in docs:
                b.delete(d.reference)
            b.commit()

    def reset_task(self, tid):
        self._wipe(self._t(tid).collection("training"))
        self.db.collection("compiled").document(tid).delete()
        self._t(tid).set({"status": "llm", "stats": dict(ZERO_STATS), "compiled_version": 0,
                          "updated": time.time()}, merge=True)

    def reset_all(self):
        for t in self.list_tasks():
            self.reset_task(t["id"])
        self._wipe(self.db.collection("logs"))
        self._wipe(self.db.collection("jobs"))
        self.db.collection("meta").document("global").set(dict(ZERO_STATS))


def make_store():
    return MemoryStore() if os.getenv("STORE") == "memory" else FirestoreStore()
