"""Print the gcloud commands that create (or update) the Cloud Scheduler jobs in
deploy/scheduler.yaml. Prints only — review, then paste (deploy/RUNBOOK.md §9).

    python deploy/scheduler_commands.py \
        --project <PROJECT_ID> --region asia-south1 \
        --service-url https://gridnova-jobs-<hash>-el.a.run.app \
        --scheduler-sa gridnova-scheduler@<PROJECT_ID>.iam.gserviceaccount.com \
        [--update]

The X-Internal-Key header value is read from Secret Manager by the printed command itself
($(gcloud secrets versions access ...)), so it never appears in this script's output.
Note that Cloud Scheduler stores header values in the job definition: anyone with
cloudscheduler.jobs.get on the project can read it — keep that role to deployers.
"""
from __future__ import annotations

import argparse
import shlex
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def commands(config: dict, project: str, region: str, service_url: str, scheduler_sa: str, update: bool,
             secret: str) -> list[str]:
    d = config["defaults"]
    retry = d["retry"]
    verb = "update" if update else "create"
    key = f'"$(gcloud secrets versions access latest --secret={secret} --project={project})"'
    out = []
    for job in config["jobs"]:
        uri = service_url.rstrip("/") + job["path"]
        parts = [
            f"gcloud scheduler jobs {verb} http {job['name']}",
            f"--project={project}", f"--location={region}",
            f"--schedule={shlex.quote(job['schedule'])}", f"--time-zone={d['time_zone']}",
            f"--uri={uri}", f"--http-method={d['http_method']}",
            f"--attempt-deadline={d['attempt_deadline']}",
            f"--max-retry-attempts={retry['max_retry_attempts']}",
            f"--min-backoff={retry['min_backoff']}", f"--max-backoff={retry['max_backoff']}",
            f"--max-doublings={retry['max_doublings']}",
            f"--oidc-service-account-email={scheduler_sa}", f"--oidc-token-audience={service_url.rstrip('/')}",
            ("--update-headers" if update else "--headers") + f"=X-Internal-Key={key}",
        ]
        out.append(" \\\n    ".join(parts))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--region", default="asia-south1")
    p.add_argument("--service-url", required=True)
    p.add_argument("--scheduler-sa", required=True)
    p.add_argument("--secret", default="gridnova-internal-api-key")
    p.add_argument("--update", action="store_true")
    a = p.parse_args()
    config = yaml.safe_load((HERE / "scheduler.yaml").read_text())
    print("\n\n".join(commands(config, a.project, a.region, a.service_url, a.scheduler_sa, a.update, a.secret)))


if __name__ == "__main__":
    main()
