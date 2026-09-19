#!/usr/bin/env python3
"""Aggregate per-VM results into a single self-contained HTML report.

Inputs (from scripts/test/output/<run-id>/):
  <vm-name>/install.log         — captured cloud-init / install output
  <vm-name>/install-status      — "OK" or "FAIL"
  <vm-name>/pytest-report.json  — pytest-json-report output (may be missing if install failed)
  <vm-name>/journalctl.log      — backend logs (best-effort)

Output:
  report.html in the run directory.
"""
import json
import sys
import html
from pathlib import Path
from datetime import datetime


def esc(s):
    return html.escape(str(s) if s is not None else "")


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def read_text(path, limit=200_000):
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
        if len(data) > limit:
            data = "...[truncated]...\n" + data[-limit:]
        return data
    except Exception:
        return "(file missing)"


def vm_summary(vm_dir: Path):
    name = vm_dir.name
    status_file = vm_dir / "install-status"
    install_ok = status_file.exists() and status_file.read_text().strip() == "OK"
    pytest_json = load_json(vm_dir / "pytest-report.json")

    tests = []
    pass_count = fail_count = skip_count = 0
    if pytest_json and "tests" in pytest_json:
        for t in pytest_json["tests"]:
            outcome = t.get("outcome", "unknown")
            tests.append((t.get("nodeid", "?"), outcome, t.get("call", {}).get("longrepr", "")))
            if outcome == "passed":
                pass_count += 1
            elif outcome == "failed":
                fail_count += 1
            elif outcome == "skipped":
                skip_count += 1

    overall = "PASS" if (install_ok and fail_count == 0 and (pass_count > 0 or pytest_json is None)) else "FAIL"
    if not install_ok:
        overall = "FAIL"

    return {
        "name": name,
        "install_ok": install_ok,
        "overall": overall,
        "tests": tests,
        "pass": pass_count,
        "fail": fail_count,
        "skip": skip_count,
        "install_log": read_text(vm_dir / "install.log"),
        "journal": read_text(vm_dir / "journalctl.log", limit=50_000),
        "runtime": load_json(vm_dir / "runtime.json"),
    }


def runtime_table(measurement):
    if not measurement:
        return '<p>Runtime baseline: unavailable (no artifact).</p>'
    status = measurement.get('status', 'unknown')
    if status != 'measured':
        return f"<p>Runtime baseline: {esc(status)}. {esc(measurement.get('reason', ''))}</p>"

    def number(value, divisor=1):
        return 'unavailable' if value is None else f'{value / divisor:,.2f}'

    rows = []
    for item in measurement.get('summary', []):
        memory = item.get('memory_bytes', {})
        cpu = item.get('cpu_percent_one_core', {})
        rows.append(f"<tr><td>{esc(item['name'])}</td>"
                    f"<td>{number(memory.get('p50'), 1024 ** 2)}</td>"
                    f"<td>{number(memory.get('sampled_max'), 1024 ** 2)}</td>"
                    f"<td>{number(cpu.get('p50'))}</td><td>{number(cpu.get('p95'))}</td></tr>")
    return ('<p>Post-install observation: cgroup memory includes cache; CPU 100% = one logical CPU. '
            'Sampled peaks can miss short spikes. This is not a capacity benchmark.</p>'
            '<table><tr><th>Service</th><th>Median MiB</th><th>Sampled peak MiB</th>'
            '<th>Median CPU %</th><th>p95 CPU %</th></tr>' + ''.join(rows) + '</table>'
            f"<p>Source: <code>{esc(measurement.get('running_revision'))}</code></p>")


HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>ServerKit E2E Report — {ts}</title>
{refresh_meta}
<style>
 body{{font-family:-apple-system,Segoe UI,sans-serif;margin:24px;background:#0f1115;color:#e6e6e6}}
 h1{{margin-top:0}}
 .live-banner{{background:#1f3a5f;color:#cfe2ff;padding:8px 14px;border-radius:6px;margin:0 0 16px;display:inline-block}}
 .live-banner .dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;margin-right:8px;animation:pulse 1.2s ease-in-out infinite}}
 @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
 .vm{{background:#1a1d24;border-radius:8px;padding:16px;margin:16px 0;border-left:6px solid #555}}
 .vm.pass{{border-left-color:#3fb950}}
 .vm.fail{{border-left-color:#f85149}}
 .vm.running{{border-left-color:#3a72d6}}
 .badge{{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}}
 .badge.pass,.badge.passed{{background:#1f6f33;color:#d2f5d8}}
 .badge.fail,.badge.failed{{background:#7a2a26;color:#fdd}}
 .badge.skip,.badge.skipped{{background:#3a3a3a;color:#bbb}}
 .badge.running{{background:#1f4b8e;color:#cfe2ff}}
 details{{margin-top:8px;background:#0c0e13;border-radius:6px;padding:8px}}
 summary{{cursor:pointer;font-weight:600}}
 pre{{white-space:pre-wrap;word-break:break-word;font-size:12px;background:#000;padding:8px;border-radius:4px;max-height:400px;overflow:auto}}
 td.empty{{color:#666;text-align:center}}
 table{{border-collapse:collapse;width:100%;margin-top:8px;font-size:13px}}
 td,th{{padding:6px 10px;border-bottom:1px solid #2a2d34;text-align:left;vertical-align:top}}
 .counts span{{margin-right:14px}}
 .stage{{color:#9aa4b2;font-size:13px;margin-top:6px}}
</style></head><body>
<h1>ServerKit E2E Test Report</h1>
{live_banner}
<p>Run: <code>{run_id}</code> · {ts}</p>
<p>Overall: <span class="badge {overall_cls}">{overall}</span>
 &nbsp; VMs: {n_vms} &nbsp; Passed: {n_pass} &nbsp; Failed: {n_fail}</p>
{vm_sections}
</body></html>"""

VM_TEMPLATE = """<div class="vm {cls}">
 <h2>{name} <span class="badge {cls}">{overall}</span></h2>
 <div class="counts">
   <span>Stage: <b>{stage}</b></span>
   <span>Install: <b>{install}</b></span>
   <span>Passed: <b>{p}</b></span>
   <span>Failed: <b>{f}</b></span>
   <span>Skipped: <b>{s}</b></span>
 </div>
 {tests_table}
 {runtime_table}
 <details><summary>Install log</summary><pre>{install_log}</pre></details>
 <details><summary>journalctl -u serverkit</summary><pre>{journal}</pre></details>
</div>"""


def _read_state(run_dir: Path):
    """state.json (written by the orchestrator) tells us which phase
    each VM is in while the run is still going."""
    f = run_dir / "state.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def render(run_dir: Path) -> Path:
    state = _read_state(run_dir)
    state_file_present = (run_dir / "state.json").exists()
    # If state.json is missing we're likely rendering between the orchestrator
    # creating OutDir and the first Save-State call — treat as RUNNING so the
    # page doesn't flash "FAIL" on first open.
    running = state.get("running", not state_file_present)
    vm_states = state.get("vms", {})

    vm_dirs = sorted([p for p in run_dir.iterdir() if p.is_dir()])
    summaries = [vm_summary(d) for d in vm_dirs]

    # Add stage from state.json (if available) and merge in VMs that haven't
    # produced output yet (so the UI shows them as "running").
    seen = {s["name"] for s in summaries}
    for name, st in vm_states.items():
        if name not in seen:
            summaries.append({
                "name": name, "install_ok": False, "overall": "RUNNING",
                "tests": [], "pass": 0, "fail": 0, "skip": 0,
                "install_log": "", "journal": "",
            })
    for s in summaries:
        s["stage"] = vm_states.get(s["name"], {}).get("stage", "done")
        # While the run is in progress, a VM that hasn't produced a status
        # file yet should display as RUNNING (not FAIL).
        if running and s["stage"] not in ("done", "install failed") and not s["install_ok"]:
            s["overall"] = "RUNNING"

    n_pass = sum(1 for s in summaries if s["overall"] == "PASS")
    n_fail = sum(1 for s in summaries if s["overall"] == "FAIL")
    overall = "PASS" if (n_fail == 0 and n_pass > 0 and not running) else ("RUNNING" if running else ("PASS" if n_fail == 0 and n_pass > 0 else "FAIL"))

    refresh_meta = '<meta http-equiv="refresh" content="8">' if running else ''
    live_banner = '<div class="live-banner"><span class="dot"></span>Run in progress — page auto-refreshes every 8s</div>' if running else ''

    vm_sections = []
    for s in summaries:
        cls = {"PASS": "pass", "FAIL": "fail", "RUNNING": "running"}.get(s["overall"], "fail")
        if s["tests"]:
            rows = []
            for nid, oc, repr_ in s["tests"]:
                details_html = (
                    f"<pre style='max-height:120px'>{esc(repr_)}</pre>"
                    if (repr_ and str(repr_).strip()) else
                    "<span class='empty'>—</span>"
                )
                rows.append(
                    f"<tr><td>{esc(nid)}</td>"
                    f"<td><span class='badge {oc}'>{esc(oc)}</span></td>"
                    f"<td>{details_html}</td></tr>"
                )
            tests_table = ("<table><tr><th>Test</th><th>Result</th><th>Details</th></tr>"
                           + "".join(rows) + "</table>")
        elif s["overall"] == "RUNNING":
            tests_table = f"<p class='stage'><i>Currently: {esc(s['stage'])}…</i></p>"
        else:
            tests_table = "<p><i>No pytest results (install likely failed before harness ran).</i></p>"

        vm_sections.append(VM_TEMPLATE.format(
            cls=cls,
            name=esc(s["name"]),
            overall=esc(s["overall"]),
            stage=esc(s.get("stage", "?")),
            install="OK" if s["install_ok"] else ("-" if s["overall"] == "RUNNING" else "FAIL"),
            p=s["pass"], f=s["fail"], s=s["skip"],
            tests_table=tests_table,
            runtime_table=runtime_table(s.get('runtime')),
            install_log=esc(s["install_log"]),
            journal=esc(s["journal"]),
        ))

    out = run_dir / "report.html"
    out.write_text(HTML_TEMPLATE.format(
        ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        run_id=esc(run_dir.name),
        overall=overall,
        overall_cls={"PASS":"pass","FAIL":"fail","RUNNING":"running"}.get(overall,"fail"),
        n_vms=len(summaries),
        n_pass=n_pass,
        refresh_meta=refresh_meta,
        live_banner=live_banner,
        n_fail=n_fail,
        vm_sections="\n".join(vm_sections),
    ), encoding="utf-8")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: report.py <run-output-dir>", file=sys.stderr)
        sys.exit(2)
    run_dir = Path(sys.argv[1]).resolve()
    if not run_dir.is_dir():
        print(f"not a directory: {run_dir}", file=sys.stderr)
        sys.exit(2)
    out = render(run_dir)
    print(str(out))
