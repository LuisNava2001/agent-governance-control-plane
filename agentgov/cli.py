"""Command-line interface: `python -m agentgov <command>` (or `agentgov` when installed).

Exit codes: 0 success, 2 the control plane refused the command (the reason is printed), 1 usage or I/O error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .canonical import short
from .engine import ControlPlane
from .errors import GovernanceError
from .policy import Policy

DEFAULT_HOME = os.environ.get("AGENTGOV_HOME", ".agentgov")


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"{result.get('task_id', '')} {result.get('event', '')} -> {result.get('state', '')}".strip())
    if result.get("reason"):
        print(f"  {result['reason']}")
    for key in ("evidence_id", "review_fingerprint"):
        if result.get(key):
            print(f"  {key}: {result[key]}")
    if result.get("grant"):
        print(f"  grant: {result['grant']['grant_id']}")


def _show(status: dict[str, Any]) -> None:
    print(f"{status['task_id']}  {status['state']}  level={status['level'] or '-'}  revision={status['revision']}")
    print(f"  title: {status['title']}  (requester {status['requester']})")
    print(f"  facts fingerprint:  {status['facts_fingerprint']}")
    print(f"  review fingerprint: {status['review_fingerprint'] or '-'}")
    if status["requirements"]:
        reqs = status["requirements"]
        print(f"  requires: evidence {', '.join(reqs['evidence'])}; {reqs['approvals']} approval(s) {reqs['approver_roles'] or ''}")
    if status["evidence_gate"]:
        for item in status["evidence_gate"]["items"]:
            mark = "ok " if item["valid"] else "-- "
            print(f"    {mark}{item['type']:<16} {item['evidence_id'] or '(missing)':<8} {'; '.join(item['problems'])}")
    for approval in status["approvals"]:
        print(f"  approval {approval['approval_id']} by {approval['approver']} [{approval['status']}] fp {short(approval['review_fingerprint'])}")
    for grant in status["grants"]:
        print(f"  grant {grant['grant_id']} [{grant['status']}] fp {short(grant['review_fingerprint'])} ({grant['basis']})")
    if status["release"]:
        release = status["release"]
        print(f"  release {release['release_id']} {release['version']}: stage {release['stage']}, deployment {release['deployment']}")
    if status["execution"]:
        execution = status["execution"]
        print(f"  execution {execution['execution_id']} [{execution['status']}] {'; '.join(execution.get('violations') or [])}")
    if status["integrity_problems"]:
        print(f"  INTEGRITY: {'; '.join(status['integrity_problems'])}")
    print(f"  next events: {', '.join(status['allowed_events']) or 'none (terminal)'}")


def cmd_demo(args: argparse.Namespace) -> int:
    from .scenarios import render_demo_data, run_all, scenario_specs

    if args.list:
        for spec in scenario_specs():
            print(f"{spec['id']:<26} {spec['title']}")
        return 0
    traces = run_all(args.scenario)
    if not traces:
        print(f"unknown scenario {args.scenario}", file=sys.stderr)
        return 1
    for trace in traces:
        print(f"{'PASS' if trace['passed'] else 'FAIL'}  {trace['id']:<26} final {trace['final']['state']:<10} {trace['title']}")
        if args.verbose or not trace["passed"]:
            for step in trace["steps"]:
                flag = " " if step["passed"] else "!"
                result = step["outcome"] if step["outcome"] == "ok" else step["error_code"]
                print(f"   {flag}{step['n']:>2} {step['do']:<15} {step['actor']:<13} {result:<20} -> {step['state_after']}")
    failed = [t["id"] for t in traces if not t["passed"]]
    if args.out:
        content = render_demo_data(traces)
        out = Path(args.out)
        if args.check:
            if not out.exists() or out.read_text(encoding="utf-8") != content:
                print(f"{out} is out of date; run `python -m agentgov demo --out {out}`", file=sys.stderr)
                return 1
            print(f"{out} is up to date")
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content.encode("utf-8"))
            print(f"wrote {out}")
    print(f"{len(traces) - len(failed)}/{len(traces)} scenarios behaved as specified")
    return 1 if failed else 0


def run(args: argparse.Namespace) -> int:
    if args.command == "init":
        home = ControlPlane.init_home(args.home)
        print(f"initialized control-plane home at {home}")
        return 0
    if args.command == "explain":
        classification = Policy.default().classify(_load_json(args.file)).to_dict()
        print(json.dumps(classification, indent=2) if args.json else
              f"level: {classification['level'] or 'FORBIDDEN'} (computed {classification['computed_level']})")
        if not args.json:
            for finding in classification["findings"]:
                print(f"  {'FORBIDDEN ' if finding['forbidden'] else ''}{finding['rule']}: {finding['detail']}")
        return 0
    if args.command == "demo":
        return cmd_demo(args)

    plane = ControlPlane(args.home)
    as_json = getattr(args, "json", False)
    if args.command == "submit":
        _print(plane.submit(_load_json(args.file), args.actor), as_json)
    elif args.command == "classify":
        _print(plane.classify(args.task, args.actor), as_json)
    elif args.command == "evidence":
        _print(plane.add_evidence(args.task, args.type, _load_json(args.file), args.source, args.actor), as_json)
    elif args.command == "plan":
        _print(plane.plan(args.task, args.actor), as_json)
    elif args.command == "review":
        _print(plane.review(args.task, args.actor), as_json)
    elif args.command == "release-plan":
        _print(plane.generate_release_plan(args.task, args.actor), as_json)
    elif args.command == "authorize":
        _print(plane.request_authorization(args.task, args.actor), as_json)
    elif args.command == "approve":
        _print(plane.approve(args.task, args.actor, args.fingerprint), as_json)
    elif args.command == "reject":
        _print(plane.reject(args.task, args.actor, args.reason), as_json)
    elif args.command == "amend":
        _print(plane.amend(args.task, _load_json(args.file), args.actor), as_json)
    elif args.command == "execute":
        _print(plane.execute(args.task, args.actor, args.grant), as_json)
    elif args.command == "complete":
        _print(plane.complete(args.task, args.actor), as_json)
    elif args.command == "block":
        _print(plane.block(args.task, args.actor, args.reason), as_json)
    elif args.command == "show":
        status = plane.status(args.task)
        print(json.dumps(status, indent=2, sort_keys=True)) if as_json else _show(status)
    elif args.command == "list":
        for task_id in plane.store.task_ids():
            status = plane.status(task_id)
            print(f"{task_id}  {status['state']:<18} {status['level'] or '-':<9} {status['title']}")
    elif args.command == "audit":
        entries = [e for e in plane.store.read_audit() if args.task in (None, e["task_id"])]
        for e in entries:
            print(f"{e['seq']:>4} {e['timestamp']} {e['task_id'] or '-':<10} {e['event']:<22} {e['actor']:<13} "
                  f"{e['outcome']:<8} {e['from_state'] or '-'} -> {e['to_state'] or '-'}  {e['reason']}")
    elif args.command == "verify":
        report = plane.verify_integrity()
        print("integrity OK" if report["ok"] else "integrity problems:")
        for problem in report["problems"]:
            print(f"  {problem}")
        return 0 if report["ok"] else 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentgov", description="Deterministic control plane for AI-agent tasks (demo).")
    parser.add_argument("--home", default=DEFAULT_HOME, help="control-plane state directory (default .agentgov or $AGENTGOV_HOME)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, *, task: bool = True, actor: bool = True, json_flag: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        if task:
            p.add_argument("task")
        if actor:
            p.add_argument("--actor", required=True)
        if json_flag:
            p.add_argument("--json", action="store_true")
        return p

    sub.add_parser("init", help="create a control-plane home with the demo policy and actors")
    explain = sub.add_parser("explain", help="classify a request file without storing anything")
    explain.add_argument("file")
    explain.add_argument("--json", action="store_true")
    add("submit", "submit a task request (JSON file)", task=False).add_argument("file")
    add("classify", "compute the risk level and requirements")
    evidence = add("evidence", "submit evidence content (JSON file)")
    evidence.add_argument("--type", required=True, choices=["plan", "test_report", "risk_assessment"])
    evidence.add_argument("--file", required=True)
    evidence.add_argument("--source", default="manual", help="where the evidence came from (fictional)")
    add("plan", "accept the plan (plan gate)")
    add("review", "check all required evidence (evidence gate)")
    add("release-plan", "generate the release plan from current evidence")
    add("authorize", "request authorization (policy grant or human approval)")
    add("approve", "approve the exact review fingerprint you inspected").add_argument("--fingerprint", required=True)
    add("reject", "reject the task").add_argument("--reason", required=True)
    add("amend", "replace the task facts (invalidates approvals)").add_argument("file")
    add("execute", "run the simulated worker (agent actor only)").add_argument("--grant")
    add("complete", "close a verified task")
    add("block", "stop a task").add_argument("--reason", required=True)
    add("show", "show a task, its gates and fingerprints", actor=False)
    add("list", "list tasks", task=False, actor=False, json_flag=False)
    audit = add("audit", "print the audit log", task=False, actor=False, json_flag=False)
    audit.add_argument("--task")
    add("verify", "verify the audit chain and every task's integrity", task=False, actor=False, json_flag=False)
    demo = sub.add_parser("demo", help="run the reproducible scenarios")
    demo.add_argument("--scenario")
    demo.add_argument("--list", action="store_true")
    demo.add_argument("--verbose", "-v", action="store_true")
    demo.add_argument("--out", help="also write demo data (e.g. demo/data.js)")
    demo.add_argument("--check", action="store_true", help="with --out: fail if the file differs from a fresh run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except GovernanceError as err:
        print(f"REJECTED {err.code}: {err.message}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
