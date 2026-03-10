import os
import tempfile
from google.cloud import storage
# from datetime import timedelta

_client = None

def get_client():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client

def upload_image_bytes(image_bytes: bytes, image_id: str) -> str:
    bucket_name = os.getenv("GCS_BUCKET", "war-room-outputs")
    tmp_path = os.path.join(tempfile.gettempdir(), f"{image_id}.png")

    with open(tmp_path, "wb") as f:
        f.write(image_bytes)

    bucket = get_client().bucket(bucket_name)
    blob = bucket.blob(f"images/{image_id}.png")
    blob.upload_from_filename(tmp_path)
    blob.make_public()  

    os.remove(tmp_path)
    return blob.public_url  


# def upload_image_bytes(image_bytes: bytes, image_id: str) -> str:
#     bucket_name = os.getenv("GCS_BUCKET", "war-room-outputs")
#     tmp_path = os.path.join(tempfile.gettempdir(), f"{image_id}.png")

#     with open(tmp_path, "wb") as f:
#         f.write(image_bytes)

#     bucket = get_client().bucket(bucket_name)
#     blob = bucket.blob(f"images/{image_id}.png")
#     blob.upload_from_filename(tmp_path)
#     os.remove(tmp_path)

#     # URL valid for 7 days — enough time for user to download
#     signed_url = blob.generate_signed_url(
#         expiration=timedelta(days=7),
#         method="GET",
#         version="v4",
#     )
#     return signed_url    ## for further implementation 