"""A throwaway S3-compatible storage server for local development — never production.

    pip install "moto[server]"        # once, inside the backend venv
    python scripts/dev_storage.py     # leave it running in its own Terminal tab

It serves on http://127.0.0.1:5055 and creates the bucket named in STORAGE_BUCKET, so the
backend can store proposal PDFs and uploads exactly as it will with Cloudflare R2. Everything
lives in memory: stopping the script forgets the files (the database rows stay).
Matches .env: STORAGE_ENDPOINT_URL=http://127.0.0.1:5055, STORAGE_ACCESS_KEY=dev,
STORAGE_SECRET_KEY=devsecret.
"""
from __future__ import annotations

import os
import sys
import time

HOST, PORT = "127.0.0.1", 5055


def main() -> int:
    try:
        from moto.server import ThreadedMotoServer
    except ImportError:
        print('moto is not installed. Run:  pip install "moto[server]"')
        return 1
    import boto3

    bucket = os.environ.get("STORAGE_BUCKET", "gridnova-dev")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "dev")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "devsecret")
    server = ThreadedMotoServer(ip_address=HOST, port=PORT, verbose=False)
    server.start()
    s3 = boto3.client("s3", endpoint_url=f"http://{HOST}:{PORT}", region_name="auto",
                      aws_access_key_id="dev", aws_secret_access_key="devsecret")
    try:
        s3.create_bucket(Bucket=bucket)
    except Exception as exc:  # already exists is fine
        print(f"(bucket: {exc})")
    print(f"Dev storage ready: http://{HOST}:{PORT}  bucket={bucket}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
