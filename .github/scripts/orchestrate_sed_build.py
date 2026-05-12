#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

SOC_TRIGGER = "soc_sign_trigger.txt"
SOC_DONE = "soc_sign_done.txt"
FIT1_TRIGGER = "fit1_sign_trigger.txt"
FIT1_DONE = "fit1_sign_done.txt"

POLL_SECONDS = 5
STAGE_DONE_TIMEOUT_SECONDS = 24 * 60 * 60


def log(msg):
    print(msg, flush=True)


def send_workflow_dispatch(repo, workflow_file, github_token, inputs):
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    payload = {"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": inputs}
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req) as resp:
        status = resp.getcode()
        if status not in (204, 201):
            raise RuntimeError(f"Workflow dispatch failed for {workflow_file} with status {status}")


def wait_for_done(done_path, label, timeout_seconds=STAGE_DONE_TIMEOUT_SECONDS):
    log(f"[INFO] Waiting for {label} done file: {done_path}")
    start = time.time()

    while True:
        if os.path.exists(done_path):
            log(f"[OK] {label} done file detected: {done_path}")
            return

        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Timed out waiting for {label} done file: {done_path}")

        time.sleep(POLL_SECONDS)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--shared-dir", required=True)
    parser.add_argument("--workflow-soc", required=True)
    parser.add_argument("--workflow-fit1", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--trigger-source", required=False, default="")
    parser.add_argument("--callback-url", required=False, default="")
    parser.add_argument("--resume-from", required=False, default="")
    parser.add_argument("--build-command", required=True)
    return parser.parse_args()


def main():
    args = parse_args()

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise RuntimeError("GITHUB_TOKEN is not set")

    soc_trigger_path = os.path.join(args.shared_dir, SOC_TRIGGER)
    soc_done_path = os.path.join(args.shared_dir, SOC_DONE)
    fit1_trigger_path = os.path.join(args.shared_dir, FIT1_TRIGGER)
    fit1_done_path = os.path.join(args.shared_dir, FIT1_DONE)

    soc_dispatched = args.resume_from in ("soc", "fit1")
    fit1_dispatched = args.resume_from == "fit1"

    build_cmd = [args.build_command]
    if args.resume_from:
        build_cmd.append(args.resume_from)

    log("[INFO] Starting build process")
    log(f"[INFO] Build command: {' '.join(build_cmd)}")
    log(f"[INFO] Shared dir: {args.shared_dir}")
    log(f"[INFO] Resume mode: {args.resume_from or 'normal'}")

    process = subprocess.Popen(
        build_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        while True:
            line = process.stdout.readline()
            if line:
                print(line, end="", flush=True)

            if (not soc_dispatched) and os.path.exists(soc_trigger_path):
                token = open(soc_trigger_path, "r", encoding="utf-8").read().strip()
                log(f"[INFO] SOC trigger detected: {token}")

                send_workflow_dispatch(
                    repo=args.repo,
                    workflow_file=args.workflow_soc,
                    github_token=github_token,
                    inputs={
                        "session_id": args.session_id,
                        "trigger_source": args.trigger_source,
                        "callback_url": args.callback_url,
                    },
                )
                soc_dispatched = True
                log("[OK] SOC workflow dispatched")

            if soc_dispatched and os.path.exists(soc_trigger_path) and not os.path.exists(soc_done_path):
                pass

            if (not fit1_dispatched) and os.path.exists(fit1_trigger_path):
                token = open(fit1_trigger_path, "r", encoding="utf-8").read().strip()
                log(f"[INFO] FIT1 trigger detected: {token}")

                send_workflow_dispatch(
                    repo=args.repo,
                    workflow_file=args.workflow_fit1,
                    github_token=github_token,
                    inputs={
                        "session_id": args.session_id,
                        "trigger_source": args.trigger_source,
                        "callback_url": args.callback_url,
                    },
                )
                fit1_dispatched = True
                log("[OK] FIT1 workflow dispatched")

            if process.poll() is not None:
                remaining = process.stdout.read()
                if remaining:
                    print(remaining, end="", flush=True)
                break

            time.sleep(POLL_SECONDS)

        exit_code = process.returncode
        log(f"[INFO] Build process exited with code: {exit_code}")

        if exit_code != 0:
            sys.exit(exit_code)

        if soc_dispatched and not os.path.exists(soc_done_path) and args.resume_from not in ("soc", "fit1"):
            wait_for_done(soc_done_path, "SOC")

        if fit1_dispatched and not os.path.exists(fit1_done_path) and args.resume_from != "fit1":
            wait_for_done(fit1_done_path, "FIT1")

        log("[OK] Orchestrated build completed successfully")

    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
