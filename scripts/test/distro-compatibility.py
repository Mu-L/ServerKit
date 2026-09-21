#!/usr/bin/env python3
"""Run evidence-producing ServerKit checks across distro containers.

This is a discovery runner, not a support-claim generator. A passing plain
container proves only the named mode:

* quick: shell syntax and source-level installer/update test suites
* provision: the real distro repositories can supply a supported Python,
  virtualenv, pip, ssl, sqlite3, ctypes, and native dependency builds through
  install.sh (Ubuntu 26.04 also verifies the full backend requirements)

It does not prove systemd, nginx, Docker-in-Docker, firewall behavior, a full
ServerKit install, a distro-specific kernel, or hardware. The generated report
keeps exact, proxy, untestable, failed, and infrastructure-error states apart.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
CATALOG_PATH = REPO_ROOT / "backend" / "app" / "data" / "distro-catalog.json"
DEFAULT_OUTPUT_ROOT = SCRIPT_DIR / "output" / "distro-compatibility"
MODES = ("quick", "provision")

# The suites run inside MINIMAL base images, so the harness must guarantee the
# tools the source-level suites legitimately assume a server has: bash, plus
# find (test_update's backup-retention assertions) and awk (test_install /
# test_cli function-body extraction). Amazon Linux 2023 ships no findutils,
# openSUSE Tumbleweed's minimal image has neither, and the SLES BCI has no awk —
# without this bootstrap those are harness gaps, not product failures.
BOOTSTRAP_BASH = r"""
need=""
command -v bash >/dev/null 2>&1 || need="$need bash"
command -v find >/dev/null 2>&1 || need="$need find"
command -v awk >/dev/null 2>&1 || need="$need awk"
if [ -n "$need" ]; then
  pkgs=""
  for tool in $need; do
    if command -v emerge >/dev/null 2>&1; then
      case "$tool" in
        bash) pkgs="$pkgs app-shells/bash" ;;
        find) pkgs="$pkgs sys-apps/findutils" ;;
        awk)  pkgs="$pkgs sys-apps/gawk" ;;
      esac
    else
      case "$tool" in
        bash) pkgs="$pkgs bash" ;;
        find) pkgs="$pkgs findutils" ;;
        awk)  pkgs="$pkgs gawk" ;;
      esac
    fi
  done
  { command -v apt-get >/dev/null 2>&1 && apt-get update -qq && apt-get install -y -qq $pkgs; } ||
  { command -v dnf >/dev/null 2>&1 && dnf install -y -q $pkgs; } ||
  { command -v yum >/dev/null 2>&1 && yum install -y -q $pkgs; } ||
  { command -v zypper >/dev/null 2>&1 && zypper --non-interactive install $pkgs; } ||
  { command -v pacman >/dev/null 2>&1 && pacman -Sy --noconfirm $pkgs; } ||
  { command -v apk >/dev/null 2>&1 && apk add --no-cache $pkgs; } ||
  { command -v emerge >/dev/null 2>&1 && emerge --oneshot $pkgs; } || true
fi
for tool in bash find awk; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "COMPAT_HARNESS_ERROR=no $tool available" >&2
    exit 127
  }
done
""".strip()

MODE_COMMANDS = {
    "quick": "exec bash /src/scripts/test/run-distro-quick.sh",
    "provision": "exec bash /src/scripts/test/test_provision_python.sh",
}
MODE_TIMEOUTS = {"quick": 900, "provision": 1500}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    targets = catalog.get("targets")
    if catalog.get("schema_version") != 1 or not isinstance(targets, list):
        raise ValueError("unsupported or malformed distro catalog")
    if len(targets) != 25:
        raise ValueError(f"catalog must describe exactly 25 targets, found {len(targets)}")

    target_ids: set[str] = set()
    probe_keys: set[str] = set()
    for target in targets:
        target_id = target.get("id")
        if not target_id or target_id in target_ids:
            raise ValueError(f"missing or duplicate target id: {target_id!r}")
        target_ids.add(target_id)
        for probe in target.get("probes", []):
            key = probe.get("key")
            if not key or key in probe_keys:
                raise ValueError(f"missing or duplicate probe key: {key!r}")
            if probe.get("fidelity") not in ("exact", "proxy"):
                raise ValueError(f"probe {key} has invalid fidelity")
            if not probe.get("image"):
                raise ValueError(f"probe {key} has no container image")
            probe_keys.add(key)

    for probe in catalog.get("legacy_probes", []):
        key = probe.get("key")
        if not key or key in probe_keys:
            raise ValueError(f"missing or duplicate legacy probe key: {key!r}")
        if probe.get("target_id") not in target_ids:
            raise ValueError(f"legacy probe {key} references an unknown target")
        probe_keys.add(key)

    for target in targets:
        missing = set(target.get("proxy_probe_keys", [])) - probe_keys
        if missing:
            raise ValueError(
                f"target {target['id']} references unknown proxy probes: {sorted(missing)}"
            )
    return catalog


def flatten_probes(catalog: dict[str, Any], include_legacy: bool) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for target in catalog["targets"]:
        for position, raw_probe in enumerate(target.get("probes", [])):
            probe = dict(raw_probe)
            probe.update(
                target_id=target["id"],
                target_label=target["label"],
                family=target["family"],
                published_status=target["published_status"],
                catalog_position=len(probes),
                target_probe_position=position,
            )
            probes.append(probe)
    if include_legacy:
        targets = {target["id"]: target for target in catalog["targets"]}
        for raw_probe in catalog.get("legacy_probes", []):
            probe = dict(raw_probe)
            target = targets[probe["target_id"]]
            probe.update(
                target_label=target["label"],
                family=probe.get("family", target["family"]),
                published_status="legacy",
                catalog_position=len(probes),
                target_probe_position=-1,
            )
            probes.append(probe)
    return probes


def normalize_only(values: list[str]) -> set[str]:
    return {
        value.strip()
        for item in values
        for value in item.split(",")
        if value.strip()
    }


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-.")


def run_logged(
    args: list[str], log_path: Path, timeout_s: int, *, append: bool = False
) -> tuple[int | None, float, str]:
    started = time.monotonic()
    mode = "a" if append else "w"
    with log_path.open(mode, encoding="utf-8", errors="replace") as log:
        if append:
            log.write("\n")
        log.write("$ " + " ".join(args) + "\n\n")
        log.flush()
        try:
            proc = subprocess.run(
                args,
                cwd=REPO_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            return proc.returncode, time.monotonic() - started, ""
        except subprocess.TimeoutExpired:
            log.write(f"\nCOMPAT_HARNESS_TIMEOUT={timeout_s}s\n")
            return None, time.monotonic() - started, f"timed out after {timeout_s}s"
        except OSError as exc:
            log.write(f"\nCOMPAT_HARNESS_OS_ERROR={exc}\n")
            return None, time.monotonic() - started, str(exc)


def docker_output(args: list[str], timeout_s: int = 30) -> str:
    try:
        proc = subprocess.run(
            ["docker", *args],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def inspect_image(image: str) -> dict[str, str]:
    image_id = docker_output(["image", "inspect", "--format", "{{.Id}}", image])
    identity_cmd = (
        ". /etc/os-release 2>/dev/null || true; "
        "printf '%s\\t%s\\t%s\\n' \"${ID:-unknown}\" "
        "\"${VERSION_ID:-unknown}\" \"${PRETTY_NAME:-unknown}\""
    )
    identity = docker_output(["run", "--rm", image, "sh", "-c", identity_cmd], 60)
    parts = identity.split("\t", 2)
    if len(parts) != 3:
        parts = ["unknown", "unknown", identity or "unknown"]
    return {
        "image_id": image_id,
        "os_id": parts[0],
        "version_id": parts[1],
        "pretty_name": parts[2],
    }


def failure_excerpt(log_path: Path, mode: str) -> str:
    """Pull the first useful cause from a failed check without parsing pass counts."""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    useful: list[str] = []
    needles = ("✘", "command not found", "No supported package manager")
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        harness_error = line.startswith("COMPAT_HARNESS_")
        if line and (harness_error or any(needle.lower() in line.lower() for needle in needles)):
            if line not in useful:
                useful.append(line)
        if len(useful) == 3:
            break
    if useful:
        excerpt = "; ".join(useful)
        return excerpt[:500]
    if mode == "provision" and "  distro:" not in text:
        return "installer exited during OS/package-manager detection before the provision assertions"
    return "see log for the failing assertion"


def result_for_pull_failure(
    probe: dict[str, Any], mode: str, exit_code: int | None, duration_s: float, detail: str
) -> dict[str, Any]:
    return {
        **{key: probe[key] for key in (
            "key", "label", "target_id", "target_label", "family", "image", "fidelity"
        )},
        "mode": mode,
        "status": "infra_error",
        "exit_code": exit_code,
        "duration_s": round(duration_s, 1),
        "detail": detail,
        "log": f"{probe['key']}/pull.log",
        "identity": {},
        "catalog_position": probe["catalog_position"],
    }


def run_probe(
    probe: dict[str, Any],
    modes: tuple[str, ...],
    run_dir: Path,
    no_pull: bool,
    remove_images: bool,
) -> list[dict[str, Any]]:
    probe_dir = run_dir / probe["key"]
    probe_dir.mkdir(parents=True, exist_ok=True)
    image = probe["image"]

    if not no_pull:
        pull_rc, pull_duration, pull_error = run_logged(
            ["docker", "pull", image], probe_dir / "pull.log", timeout_s=900
        )
        if pull_rc != 0:
            if pull_rc is None:
                detail = pull_error or "image pull did not return an exit code"
            else:
                detail = f"docker pull failed with exit {pull_rc}"
            return [
                result_for_pull_failure(probe, mode, pull_rc, pull_duration, detail)
                for mode in modes
            ]
    else:
        (probe_dir / "pull.log").write_text(
            "Pull skipped by --no-pull; Docker may use a cached image.\n",
            encoding="utf-8",
        )

    identity = inspect_image(image)
    results: list[dict[str, Any]] = []
    mount_source = REPO_ROOT.as_posix()
    for mode in modes:
        container_name = safe_slug(
            f"sk-compat-{probe['key']}-{mode}-{uuid.uuid4().hex[:8]}"
        )[:120]
        script = BOOTSTRAP_BASH + "\n" + MODE_COMMANDS[mode]
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--mount",
            f"type=bind,source={mount_source},target=/src,readonly",
            "--workdir",
            "/src",
            image,
            "sh",
            "-c",
            script,
        ]
        log_path = probe_dir / f"{mode}.log"
        rc, duration, error = run_logged(
            command, log_path, timeout_s=MODE_TIMEOUTS[mode]
        )
        if rc is None:
            status = "infra_error"
            detail = error or "docker run did not return an exit code"
            docker_output(["rm", "-f", container_name])
        elif rc == 0:
            status = "passed"
            detail = "exit 0"
        elif rc == 125:
            status = "infra_error"
            detail = "docker could not start the test container (exit 125)"
        else:
            status = "failed"
            detail = f"test exited {rc}: {failure_excerpt(log_path, mode)}"
        results.append(
            {
                **{key: probe[key] for key in (
                    "key", "label", "target_id", "target_label", "family", "image", "fidelity"
                )},
                "mode": mode,
                "status": status,
                "exit_code": rc,
                "duration_s": round(duration, 1),
                "detail": detail,
                "log": f"{probe['key']}/{mode}.log",
                "identity": identity,
                "catalog_position": probe["catalog_position"],
            }
        )

    if remove_images:
        docker_output(["image", "rm", image], 120)
    return results


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def result_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        status: sum(result["status"] == status for result in results)
        for status in ("passed", "failed", "infra_error")
    }


def target_evidence(
    target: dict[str, Any], results: list[dict[str, Any]]
) -> tuple[str, str]:
    own_keys = {probe["key"] for probe in target.get("probes", [])}
    proxy_keys = set(target.get("proxy_probe_keys", []))
    relevant = [result for result in results if result["key"] in own_keys]
    evidence_kind = "exact" if any(
        probe.get("fidelity") == "exact" for probe in target.get("probes", [])
    ) else "proxy"
    if not relevant and proxy_keys:
        relevant = [result for result in results if result["key"] in proxy_keys]
        evidence_kind = "base proxy"
    if not relevant:
        if own_keys or proxy_keys:
            return "not selected", "No matching probe was selected in this run."
        return "separate harness", target["windows_strategy"]

    counts = result_counts(relevant)
    total = len(relevant)
    if counts["failed"]:
        state = f"{evidence_kind} failing"
    elif counts["infra_error"]:
        state = f"{evidence_kind} infrastructure error"
    elif counts["passed"] == total:
        state = f"{evidence_kind} passed"
    else:
        state = f"{evidence_kind} incomplete"
    detail = (
        f"{counts['passed']}/{total} checks passed; "
        f"{counts['failed']} failed; {counts['infra_error']} infrastructure errors."
    )
    if evidence_kind != "exact":
        detail += " This is not exact distro/full-install proof."
    return state, detail


def write_markdown(
    path: Path,
    catalog: dict[str, Any],
    probes: list[dict[str, Any]],
    results: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    counts = result_counts(results)
    exact_probes = sum(probe["fidelity"] == "exact" for probe in probes)
    proxy_probes = sum(probe["fidelity"] == "proxy" for probe in probes)
    lines = [
        "# ServerKit distro compatibility discovery",
        "",
        f"Generated: `{metadata['finished_at']}`  ",
        f"Host: `{md_escape(metadata['host'])}`  ",
        f"Docker: `{md_escape(metadata['docker_version'] or 'unavailable')}`  ",
        f"Modes: `{', '.join(metadata['modes'])}`",
        "",
        "## Result summary",
        "",
        f"- Published targets cataloged: **{len(catalog['targets'])}/25**",
        f"- Concrete images selected: **{len(probes)}** "
        f"({exact_probes} exact, {proxy_probes} vendor userland proxies)",
        f"- Checks: **{len(results)}** — {counts['passed']} passed, "
        f"{counts['failed']} failed, {counts['infra_error']} infrastructure errors",
        "",
        "> A quick pass proves shell syntax and source-level suites only. A provision pass proves",
        "> that install.sh obtained a usable supported Python from real repositories. Neither is a",
        "> full ServerKit install, systemd, nginx, firewall, kernel, hardware, LXC, or hypervisor pass.",
        "",
        "## All 25 published targets",
        "",
        "| Target | Published claim | Container evidence | Detail | Windows test path |",
        "|---|---:|---|---|---|",
    ]
    for target in catalog["targets"]:
        state, detail = target_evidence(target, results)
        lines.append(
            "| "
            + " | ".join(
                md_escape(value)
                for value in (
                    f"{target['label']} {target['versions']}",
                    target["published_status"],
                    state,
                    detail,
                    target["windows_strategy"],
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Concrete probe results",
            "",
            "| Probe | Fidelity | Pulled identity | Mode | Result | Duration | Log |",
            "|---|---|---|---|---:|---:|---|",
        ]
    )
    for result in sorted(results, key=lambda item: (item["catalog_position"], MODES.index(item["mode"]))):
        identity = result.get("identity", {})
        pretty = identity.get("pretty_name") or "identity unavailable"
        lines.append(
            "| "
            + " | ".join(
                (
                    md_escape(f"{result['label']} (`{result['image']}`)"),
                    md_escape(result["fidelity"]),
                    md_escape(pretty),
                    md_escape(result["mode"]),
                    md_escape(result["status"]),
                    md_escape(f"{result['duration_s']:.1f}s"),
                    f"[log]({result['log']})",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `failed` means the image ran and the ServerKit check returned non-zero. It is product evidence, not hidden as an expected failure.",
            "- `infra_error` means the image could not be pulled/run or the harness timed out; it is not evidence that ServerKit failed.",
            "- `base proxy` means only a related base distro ran. It must never be promoted to an exact distro support result.",
            "- Full support numbers require the existing VM/systemd harnesses and special hosts listed in the target table.",
            "",
        ]
    )
    failed = [result for result in results if result["status"] == "failed"]
    if failed:
        lines.extend(
            [
                "## Failed-check details",
                "",
                "| Probe | Mode | First useful evidence |",
                "|---|---|---|",
            ]
        )
        for result in failed:
            lines.append(
                "| "
                + " | ".join(
                    md_escape(value)
                    for value in (result["label"], result["mode"], result["detail"])
                )
                + " |"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("quick", "provision", "both"), default="both")
    parser.add_argument(
        "--only",
        nargs="*",
        default=[],
        metavar="KEY",
        help="probe keys, separated by spaces or commas",
    )
    parser.add_argument("--jobs", type=int, default=4, help="parallel image workers")
    parser.add_argument("--include-legacy", action="store_true")
    parser.add_argument("--no-pull", action="store_true", help="use cached images when present")
    parser.add_argument("--remove-images", action="store_true", help="remove tested images after each probe")
    parser.add_argument("--strict", action="store_true", help="exit non-zero for failed/infra checks")
    parser.add_argument("--list", action="store_true", help="validate and list the catalog without Docker")
    parser.add_argument("--output", type=Path, help="report directory (default: timestamped output)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = load_catalog()
    all_probes = flatten_probes(catalog, args.include_legacy)
    selected_keys = normalize_only(args.only)
    known_keys = {probe["key"] for probe in all_probes}
    unknown = selected_keys - known_keys
    if unknown:
        print(f"Unknown probe key(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    probes = [
        probe for probe in all_probes if not selected_keys or probe["key"] in selected_keys
    ]

    if args.list:
        for target in catalog["targets"]:
            keys = ", ".join(probe["key"] for probe in target.get("probes", [])) or "separate harness"
            print(
                f"{target['id']:<22} {target['published_status']:<10} "
                f"{target['label']} {target['versions']}: {keys}"
            )
        print(f"\n25 targets; {len(all_probes)} runnable image probes")
        return 0

    if shutil.which("docker") is None:
        print("Docker CLI is not on PATH", file=sys.stderr)
        return 2
    docker_version = docker_output(["version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"])
    if not docker_version:
        print("Docker daemon is unavailable", file=sys.stderr)
        return 2

    modes = MODES if args.mode == "both" else (args.mode,)
    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = (args.output or (DEFAULT_OUTPUT_ROOT / run_stamp)).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "started_at": utc_now(),
        "finished_at": None,
        "host": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": sys.version.split()[0],
        "docker_version": docker_version,
        "modes": list(modes),
        "selected_probe_keys": [probe["key"] for probe in probes],
        "jobs": max(1, args.jobs),
    }
    print(
        f"ServerKit distro discovery: {len(probes)} images x {len(modes)} mode(s), "
        f"{max(1, args.jobs)} workers",
        flush=True,
    )
    print(f"Output: {run_dir}", flush=True)

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        future_to_probe = {
            pool.submit(
                run_probe,
                probe,
                modes,
                run_dir,
                args.no_pull,
                args.remove_images,
            ): probe
            for probe in probes
        }
        for future in concurrent.futures.as_completed(future_to_probe):
            probe = future_to_probe[future]
            try:
                probe_results = future.result()
            except Exception as exc:  # keep one harness bug from erasing the run
                probe_results = [
                    result_for_pull_failure(
                        probe, mode, None, 0.0, f"unhandled harness error: {exc}"
                    )
                    for mode in modes
                ]
            results.extend(probe_results)
            summary = ", ".join(
                f"{result['mode']}={result['status']}" for result in probe_results
            )
            print(
                f"[{len(results):>3}/{len(probes) * len(modes)}] "
                f"{probe['label']}: {summary}",
                flush=True,
            )

    results.sort(key=lambda item: (item["catalog_position"], MODES.index(item["mode"])))
    metadata["finished_at"] = utc_now()
    payload = {
        "schema_version": 1,
        "metadata": metadata,
        "summary": result_counts(results),
        "targets": catalog["targets"],
        "results": results,
    }
    (run_dir / "report.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    write_markdown(run_dir / "report.md", catalog, probes, results, metadata)

    counts = payload["summary"]
    print(
        f"Done: {counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['infra_error']} infrastructure errors",
        flush=True,
    )
    print(f"Report: {run_dir / 'report.md'}", flush=True)
    if args.strict and (counts["failed"] or counts["infra_error"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
