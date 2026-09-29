"""In-memory job manager: runs investigations in background threads; the UI polls for progress.
Single-instance only (jobs live in memory). For multiple instances store jobs in Postgres/Valkey."""
import re, time, uuid, threading, collections
from agent import Investigation
from tools import Api

CASE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
MAX_JOBS = 100

class Job:
    def __init__(self, case_id):
        self.id, self.case_id = uuid.uuid4().hex[:10], case_id
        self.status, self.steps, self.result, self.metrics = "running", [], None, {}
        self.error, self.answers, self.inv, self.t0 = None, [], None, time.time()
        self.lock = threading.Lock()

    def view(self, since=0):
        return {"id": self.id, "case_id": self.case_id, "status": self.status, "steps": self.steps[since:],
                "next": len(self.steps), "result": self.result, "metrics": self.metrics, "error": self.error,
                "answers": self.answers, "elapsed_s": round(time.time() - self.t0, 1)}

class JobManager:
    def __init__(self, investigation_cls=Investigation):
        self.jobs, self.cls = collections.OrderedDict(), investigation_cls

    def _trim(self):
        while len(self.jobs) > MAX_JOBS: self.jobs.popitem(last=False)

    def start(self, case_id):
        case_id = (case_id or "").strip().upper()
        if not CASE_RE.match(case_id): raise ValueError("Case ID may only contain letters, digits, - and _ (max 40)")
        c = Api(case_id).run("get_case", {})            # fail fast on unknown case
        if isinstance(c, dict) and "error" in c:
            code = c.get("error")
            raise LookupError(f"Case {case_id} not found in the dataset" if code == 404 else f"Dataset API problem: {c}")
        job = Job(case_id); self.jobs[job.id] = job; self._trim()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job):
        try:
            job.inv = self.cls(job.case_id, emit=job.steps.append)
            job.result = job.inv.run()
            job.metrics = job.inv.metrics(); job.status = "done"
        except Exception as e:
            job.error = f"{type(e).__name__}: {e}"[:500]; job.status = "error"

    def ask(self, job_id, question):
        job = self.jobs.get(job_id)
        if not job: raise KeyError(job_id)
        if job.status != "done" or job.inv is None: raise RuntimeError("Wait for the investigation to finish first")
        if not job.lock.acquire(blocking=False): raise RuntimeError("A question is already being answered")
        job.status = "running"
        def work():
            try:
                job.answers.append({"q": question, "a": job.inv.ask(question)})
                job.metrics = job.inv.metrics()
            except Exception as e:
                job.answers.append({"q": question, "a": f"Error: {type(e).__name__}: {e}"[:300]})
            finally:
                job.status = "done"; job.lock.release()
        threading.Thread(target=work, daemon=True).start()
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)
