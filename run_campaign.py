"""Drive maps end-to-end through the running webui (port 5001), auto-approving every gate.

Unlike run_batch.py, which calls the pipeline modules directly, this goes through
the same HTTP API the browser uses and writes every review artifact, so the UI
shows a clean, fully reviewed run afterwards.  The gate order mirrors
continuePipeline in webui/static/minimal/controls.js.

    python run_campaign.py --all              incomplete maps, continuing from their state
    python run_campaign.py --all --force      every map, from Step 1
    python run_campaign.py --from-step 4 spain china
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5001"
ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "campaign.log")
REPORT = os.path.join(ROOT, "campaign_report.json")


def call(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(p):
    return call("GET", p)


def post(p, payload=None):
    return call("POST", p, payload if payload is not None else {})


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def q(stem):
    return urllib.parse.quote(stem)


def wait(stem):
    while True:
        time.sleep(5)
        code, job = get(f"/api/job/{q(stem)}")
        if not isinstance(job, dict) or job.get("status") != "running":
            return job if isinstance(job, dict) else {"status": "error", "error": str(job)}


def run(stem, steps):
    code, out = post("/api/run", {"stem": stem, "steps": steps})
    if code != 200:
        return {"status": "refused", "error": f"HTTP {code}: {str(out)[:300]}"}
    job = wait(stem)
    for line in job.get("log", []):
        if line.startswith(("ERROR", "WARN")) or any(
                key in line for key in ("derived", "re-read", "merged", "legend=", "group(s)")):
            log(f"    | {line[:200]}")
    return job


def state(stem):
    code, maps = get("/api/maps")
    for m in maps["maps"]:
        if m["stem"] == stem:
            return m
    return None


def drive(stem, from_step=None):
    m = state(stem)
    if m is None:
        return {"stem": stem, "result": "unknown map"}
    done = {int(k) for k, v in (m.get("steps") or {}).items() if v}
    if from_step is not None:
        done = {s for s in done if s < from_step}
    log(f"=== {stem} === done={sorted(done)}")
    record = {"stem": stem, "result": None, "failed_at": None, "error": None}

    def need(*steps):
        return [s for s in steps if s not in done]

    def fail(where, job):
        record["failed_at"] = where
        record["error"] = str(job.get("error") or job.get("status"))[:400]
        record["result"] = "failed"
        log(f"    FAILED at {where}: {record['error'][:200]}")
        return record

    try:
        if need(1, 2):
            job = run(stem, need(1, 2))
            if job.get("status") != "done":
                return fail("1-2", job)
            done.update({1, 2})
            if not state(stem).get("in_scope", True):
                record["result"] = "out of scope"
                log("    out of scope; stopping")
                return record
        if 3 not in done:
            code, _ = post(f"/api/maskreview/{q(stem)}", {"approve": True})
            log(f"    mask approve -> {code}")
        if need(3, 4, 5):
            job = run(stem, need(3, 4, 5))
            if job.get("status") != "done":
                return fail("3-5", job)
            done.update({3, 4, 5})
        if not state(stem).get("step5_review_ready"):
            code, rev = get(f"/api/aggregation-review/{q(stem)}")
            if code == 200 and rev.get("effective_groups"):
                groups = [{"label": g["label"], "members": g["members"], "approved": True,
                           "rationale": g.get("rationale", "")} for g in rev["effective_groups"]]
                code, out = post(f"/api/aggregation-review/{q(stem)}", {"groups": groups})
                status = out.get("review", {}).get("status") if isinstance(out, dict) else out
                log(f"    step5 approve -> {code} {status}")
        if 6 not in done:
            job = run(stem, [6])
            if job.get("status") != "done":
                return fail("6", job)
            done.add(6)
        if not state(stem).get("step6_review_ready"):
            code, _ = post(f"/api/step6preset/{q(stem)}", {"level": 4})
            log(f"    step6 preset -> {code}")
            done -= {7, 8, 9}   # choosing a level retires the pattern/label/legend artifacts
        for step in (7, 8, 9):
            if step not in done:
                job = run(stem, [step])
                if job.get("status") != "done":
                    return fail(str(step), job)
                done.add(step)
            code, _ = post(f"/api/step{step}-review/{q(stem)}", {"approve": True})
            log(f"    step{step} approve -> {code}")
        code, body = call("GET", f"/api/download/{q(stem)}")
        record["download"] = code
        record["result"] = "complete" if code == 200 else f"download HTTP {code}"
        log(f"    download -> {code}")
    except Exception as exc:  # noqa: BLE001 - report, keep the campaign going
        record["result"] = "crashed"
        record["error"] = repr(exc)[:300]
        log(f"    CRASH {exc!r}")
    return record


def main(argv):
    force = "--force" in argv
    from_step = None
    if "--from-step" in argv:
        i = argv.index("--from-step")
        from_step = int(argv[i + 1])
        del argv[i:i + 2]
    stems = [a for a in argv if not a.startswith("--")]
    if "--all" in argv:
        code, maps = get("/api/maps")
        stems = [m["stem"] for m in maps["maps"]
                 if force or not all((m.get("steps") or {}).get(str(s)) for s in range(1, 10))]
        if force:
            from_step = 1
    report = json.load(open(REPORT, encoding="utf-8")) if os.path.exists(REPORT) else {}
    for stem in stems:
        started = time.monotonic()
        rec = drive(stem, from_step)
        rec["seconds"] = round(time.monotonic() - started)
        report[stem] = rec
        json.dump(report, open(REPORT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        log(f"    -> {rec['result']} in {rec['seconds']}s")
    log("CAMPAIGN DONE")


if __name__ == "__main__":
    main(sys.argv[1:])
