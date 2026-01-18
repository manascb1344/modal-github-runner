import modal
import os
import hmac
import hashlib
import logging
import httpx
import json
from fastapi import Request, HTTPException

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("modal-github-runner")

# Constants
RUNNER_VERSION = "2.311.0"
TIMEOUT_SECONDS = 3600

# Canonical runner image definition
runner_image = (
    modal.Image.debian_slim()
    .apt_install('curl', 'git', 'ca-certificates', 'sudo', 'jq')
    .pip_install("fastapi", "httpx")
    .run_commands(
        'mkdir -p /actions-runner',
        f'curl -L https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz | tar -xz -C /actions-runner',
        '/actions-runner/bin/installdependencies.sh'
    )
)

app = modal.App("modal-github-runner")

# Secrets should contain GITHUB_TOKEN and WEBHOOK_SECRET
github_secret = modal.Secret.from_name("github-secret")

async def verify_signature(request: Request):
    """Verify GitHub webhook signature using HMAC-SHA256."""
    webhook_secret = os.environ.get("WEBHOOK_SECRET")
    if not webhook_secret:
        logger.error("WEBHOOK_SECRET not set. Security verification failed.")
        raise HTTPException(status_code=500, detail="Server configuration error: WEBHOOK_SECRET missing")

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

    # Fetch configuration from environment with defaults
    runner_group_id = int(os.environ.get("RUNNER_GROUP_ID", 1))
    runner_labels_str = os.environ.get("RUNNER_LABELS", '["self-hosted", "modal"]')
    try:
        runner_labels = json.loads(runner_labels_str)
    except Exception:
        runner_labels = ["self-hosted", "modal"]

    headers = {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }
    
    data = {
        "name": f"modal-runner-{job_id}",
        "runner_group_id": runner_group_id,
        "labels": runner_labels,
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
        # Optimized command: Run directly from image directory to avoid copy overhead
        cmd = (
            "cd /actions-runner && export RUNNER_ALLOW_RUNASROOT=1 && ./run.sh --jitconfig $GHA_JIT_CONFIG"
        )
        
        modal.Sandbox.create(
            "bash", "-c", cmd,
            image=runner_image,
            app=app,
            timeout=TIMEOUT_SECONDS,
            env={"GHA_JIT_CONFIG": jit_config}
        )
    except Exception as e:
        logger.error(f"Failed to create sandbox for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to spawn runner sandbox")

    return {"status": "provisioned", "job_id": job_id}
