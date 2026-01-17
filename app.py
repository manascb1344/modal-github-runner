import modal
import os
import hmac
import hashlib
import logging
import httpx
from fastapi import Request, HTTPException
from image import runner_image

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("modal-github-runner")

# Constants
RUNNER_GROUP_ID = 1
RUNNER_LABELS = ["self-hosted", "modal"]
TIMEOUT_SECONDS = 3600

app = modal.App("modal-github-runner")

# Secrets should contain GITHUB_TOKEN and WEBHOOK_SECRET
github_secret = modal.Secret.from_name("github-secret")

async def verify_signature(request: Request):
    """Verify GitHub webhook signature using HMAC-SHA256."""
    webhook_secret = os.environ.get("WEBHOOK_SECRET")
    if not webhook_secret:
        logger.warning("WEBHOOK_SECRET not set, skipping verification (unsafe!)")
        return

    signature = request.headers.get("X-Hub-Signature-256")
    if not signature:
        logger.error("Missing X-Hub-Signature-256 header")
        raise HTTPException(status_code=403, detail="Signature missing")

    body = await request.body()
    hash_object = hmac.new(webhook_secret.encode(), msg=body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        logger.error("Invalid signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

@app.function(image=runner_image, secrets=[github_secret])
@modal.fastapi_endpoint(method="POST")
async def github_webhook(request: Request):
    # Verify signature before processing
    await verify_signature(request)

    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse JSON payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if payload.get("action") != "queued":
        return {"status": "ignored"}

    workflow_job = payload.get("workflow_job", {})
    repo_url = payload.get("repository", {}).get("url")
    job_id = workflow_job.get("id", "unknown")

    if not repo_url:
        logger.error("Missing repository URL in payload")
        raise HTTPException(status_code=400, detail="Missing repository URL")

    headers = {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }
    
    data = {
        "name": f"modal-runner-{job_id}",
        "runner_group_id": RUNNER_GROUP_ID,
        "labels": RUNNER_LABELS,
        "work_directory": "_work",
    }
    
    logger.info(f"Requesting JIT config for job {job_id}...")
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{repo_url}/actions/runners/generate-jitconfig", 
                headers=headers, 
                json=data
            )
            response.raise_for_status()
            jit_config = response.json()['encoded_jit_config']
        except httpx.HTTPStatusError as e:
            logger.error(f"GitHub API error for job {job_id}: {e.response.text}")
            raise HTTPException(status_code=e.response.status_code, detail="Failed to generate JIT config")
        except Exception as e:
            logger.error(f"Unexpected error calling GitHub API for job {job_id}: {e}")
            raise HTTPException(status_code=500, detail="Internal server error")

    logger.info(f"Spawning sandbox for job {job_id}...")
    
    try:
        # Pass the JIT configuration via an environment variable ('GHA_JIT_CONFIG')
        # to the Sandbox instead of a command-line argument.
        modal.Sandbox.create(
            "bash", "-c", "cp -r /actions-runner/* ~/ && ./run.sh --jitconfig $GHA_JIT_CONFIG",
            image=runner_image,
            app=app,
            user="runner",
            workdir="/home/runner",
            timeout=TIMEOUT_SECONDS,
            secrets=[github_secret],
            env={"GHA_JIT_CONFIG": jit_config}
        )
    except Exception as e:
        logger.error(f"Failed to create sandbox for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to spawn runner sandbox")

    return {"status": "provisioned", "job_id": job_id}
