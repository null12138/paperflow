"""Optional S3-compatible archive upload."""
from __future__ import annotations
import os
from pathlib import Path

def enabled() -> bool:
    return all(os.getenv(k, "").strip() for k in ("PAPERFLOW_OSS_BUCKET", "PAPERFLOW_OSS_ACCESS_KEY", "PAPERFLOW_OSS_SECRET_KEY", "PAPERFLOW_OSS_ENDPOINT"))

def upload_archive(path: Path, job_id: int) -> str | None:
    if not enabled(): return None
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    bucket=os.environ["PAPERFLOW_OSS_BUCKET"].strip(); endpoint=os.environ["PAPERFLOW_OSS_ENDPOINT"].strip().rstrip("/")
    key=f"paperflow/archives/{path.name}"
    client=boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=os.environ["PAPERFLOW_OSS_ACCESS_KEY"], aws_secret_access_key=os.environ["PAPERFLOW_OSS_SECRET_KEY"], region_name=os.getenv("PAPERFLOW_OSS_REGION", "us-east-1"), config=Config(retries={"max_attempts":10,"mode":"adaptive"}, s3={"addressing_style":"path"}))
    transfer=TransferConfig(multipart_threshold=8*1024*1024, multipart_chunksize=16*1024*1024, max_concurrency=4, use_threads=True)
    client.upload_file(str(path), bucket, key, ExtraArgs={"ContentType":"application/zip","ContentDisposition":f'attachment; filename="{path.name}"'}, Config=transfer)
    return f"{endpoint}/{bucket}/{key}"

def signed_archive_url(job_id: int, expires: int = 3600) -> str | None:
    if not enabled(): return None
    import boto3
    from botocore.config import Config
    bucket=os.environ["PAPERFLOW_OSS_BUCKET"].strip(); endpoint=os.environ["PAPERFLOW_OSS_ENDPOINT"].strip().rstrip("/")
    name=f"pdf-task-{int(job_id)}.zip"; key=f"paperflow/archives/{name}"
    client=boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=os.environ["PAPERFLOW_OSS_ACCESS_KEY"], aws_secret_access_key=os.environ["PAPERFLOW_OSS_SECRET_KEY"], region_name=os.getenv("PAPERFLOW_OSS_REGION", "us-east-1"), config=Config(s3={"addressing_style":"path"}))
    try: client.head_object(Bucket=bucket, Key=key)
    except Exception: return None
    return client.generate_presigned_url("get_object", Params={"Bucket":bucket,"Key":key,"ResponseContentDisposition":f'attachment; filename="{name}"'}, ExpiresIn=expires)
