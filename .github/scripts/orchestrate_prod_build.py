#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import time

SOC_TRIGGER = "soc_sign_trigger_prod.txt"
SOC_DONE = "soc_sign_done_prod.txt"
FIT1_TRIGGER = "fit1_sign_trigger_prod.txt"
FIT1_DONE = "fit1_sign_done_prod.txt"

POLL_SECONDS = 5
STAGE_DONE_TIMEOUT_SECONDS = 24 * 60 * 60


def log(msg):
    print(msg, flush=True)


def read_token(path):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        log(f"[WARN] Failed to read token from {path}: {e}")
        return ""


def send_workflow_dispatch(repo, workflow_file, github_token, inputs):
    ref = os.environ.get("GITHUB_REF_NAME", "master")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"

    payload = json.dumps({
        "ref": ref,
        "inputs": inputs
    })

    response_file = "/tmp/github_dispatch_response.txt"

    cmd = [
        "curl",
        "-L",
        "-X", "POST",
        url,
        "-H", "Accept: application/vnd.github+json",
        "-H", f"Authorization: Bearer {github_token}",
        "-H", "Content-Type: application/json",
        "-d", payload,
        "-o", response_file,
        "-w", "%{http_code}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    http_code = result.stdout.strip()
    response_body = ""
    if os.path.exists(response_file):
        with open(response_file, "r", encoding="utf-8", errors="ignore") as f:
            response_body = f.read()

    if result.returncode != 0:
        raise RuntimeError(
            f"curl dispatch failed for {workflow_file}. "
            f"returncode={result.returncode}, stderr={result.stderr}"
        )

    if http_code not in ("201", "204"):
        raise RuntimeError(
            f"Workflow dispatch failed for {workflow_file}. "
            f"http_code={http_code}, response={response_body}"
        )

    log(f"[OK] Workflow dispatch accepted for {workflow_file} (HTTP {http_code})")


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

    initial_soc_token = read_token(soc_trigger_path)
    initial_fit1_token = read_token(fit1_trigger_path)

    last_soc_token = initial_soc_token
    last_fit1_token = initial_fit1_token

    soc_dispatched = False
    fit1_dispatched = False

    build_cmd = [args.build_command]
    if args.resume_from:
        build_cmd.append(args.resume_from)

    log("[INFO] Starting build process")
    log(f"[INFO] Build command: {' '.join(build_cmd)}")
    log(f"[INFO] Shared dir: {args.shared_dir}")
    log(f"[INFO] Resume mode: {args.resume_from or 'normal'}")
    log(f"[INFO] Initial SOC trigger token : {initial_soc_token or '<none>'}")
    log(f"[INFO] Initial FIT1 trigger token: {initial_fit1_token or '<none>'}")

    process = subprocess.Popen(build_cmd)

    try:
        while True:
            current_soc_token = read_token(soc_trigger_path)
            current_fit1_token = read_token(fit1_trigger_path)

            if (
                not soc_dispatched
                and current_soc_token
                and current_soc_token != initial_soc_token
                and current_soc_token != last_soc_token
            ):
                log(f"[INFO] New SOC trigger detected: {current_soc_token}")
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
                last_soc_token = current_soc_token
                log("[OK] SOC workflow dispatched")

            if (
                not fit1_dispatched
                and current_fit1_token
                and current_fit1_token != initial_fit1_token
                and current_fit1_token != last_fit1_token
            ):
                log(f"[INFO] New FIT1 trigger detected: {current_fit1_token}")
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
                last_fit1_token = current_fit1_token
                log("[OK] FIT1 workflow dispatched")

            if process.poll() is not None:
                break

            time.sleep(POLL_SECONDS)

        exit_code = process.returncode
        log(f"[INFO] Build process exited with code: {exit_code}")

        if exit_code != 0:
            sys.exit(exit_code)

        if soc_dispatched and not os.path.exists(soc_done_path):
            wait_for_done(soc_done_path, "SOC")

        if fit1_dispatched and not os.path.exists(fit1_done_path):
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
