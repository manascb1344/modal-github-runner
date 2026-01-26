import modal
import os
import hmac
import hashlib
import logging
import httpx
import json
import time
import re
from urllib.parse import urlparse
from fastapi import Request, HTTPException

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("modal-github-runner")

# =============================================================================
# CONFIGURATION
# =============================================================================

# Runner version - configurable via environment for security updates
RUNNER_VERSION = os.environ.get("RUNNER_VERSION", "2.311.0")

# Sandbox timeout
TIMEOUT_SECONDS = 3600

# Request body size limit (1MB)
MAX_BODY_SIZE = 1_000_000

# Rate limiting - in-memory deduplication
_processed_jobs: dict[str, float] = {}
_processed_deliveries: set[str] = set()
JOB_DEDUP_WINDOW_SECONDS = 300  # 5 minutes
MAX_PROCESSED_CACHE_SIZE = 10000

# Replay protection - delivery ID cache
DELIVERY_CACHE_MAX_SIZE = 10000

# Repository allowlist (comma-separated, empty = allow all)
# Set via environment: ALLOWED_REPOS="owner/repo1,owner/repo2"
ALLOWED_REPOS_STR = os.environ.get("ALLOWED_REPOS", "")
ALLOWED_REPOS = [r.strip() for r in ALLOWED_REPOS_STR.split(",") if r.strip()]

# HTTP client timeout
HTTP_TIMEOUT_SECONDS = 30.0

# =============================================================================
# TRUST MODEL
# =============================================================================
# SECURITY NOTE: This runner executes with RUNNER_ALLOW_RUNASROOT=1
#
# Trust Model:
# - Only repositories in ALLOWED_REPOS can trigger runner creation
# - Each job runs in an ephemeral, isolated Modal sandbox
# - JIT tokens are single-use and job-specific
# - Sandbox is destroyed after job completion
#
# Risks:
# - A malicious workflow in an allowed repo could access secrets during execution
# - Root access allows full control within the sandbox during job lifetime
#
# Mitigations:
# - Use ALLOWED_REPOS to restrict to trusted repositories only
# - Modal sandbox isolation limits blast radius
# - JIT tokens cannot be reused after job completion
# - Consider using fine-grained PATs with minimal repository access
# =============================================================================

# Pinned dependency versions for reproducibility and supply chain security
runner_image = (
    modal.Image.debian_slim()
    .apt_install("curl", "git", "ca-certificates", "sudo", "jq")
    .pip_install("fastapi==0.115.0", "httpx==0.27.0")
    .run_commands(
        "mkdir -p /actions-runner",
        f"curl -L https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz | tar -xz -C /actions-runner",
        "/actions-runner/bin/installdependencies.sh",
    )
)

app = modal.App("modal-github-runner")

# Secrets should contain GITHUB_TOKEN, WEBHOOK_SECRET, and optionally ALLOWED_REPOS
github_secret = modal.Secret.from_name("github-secret")


def _cleanup_job_cache():
    """Remove expired entries from job deduplication cache."""
    global _processed_jobs
    if len(_processed_jobs) > MAX_PROCESSED_CACHE_SIZE:
        current_time = time.time()
        _processed_jobs = {
            job_id: timestamp
            for job_id, timestamp in _processed_jobs.items()
            if current_time - timestamp < JOB_DEDUP_WINDOW_SECONDS
        }


def _cleanup_delivery_cache():
    """Limit delivery cache size to prevent memory issues."""
    global _processed_deliveries
    if len(_processed_deliveries) > DELIVERY_CACHE_MAX_SIZE:
        _processed_deliveries = set(
            list(_processed_deliveries)[DELIVERY_CACHE_MAX_SIZE // 2 :]
        )


def _validate_github_url(url: str) -> bool:
    """Validate URL is a legitimate GitHub API URL."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        # Only allow github.com and api.github.com
        # Also support GitHub Enterprise with custom domains if needed
        allowed_domains = {"github.com", "api.github.com"}
        github_enterprise_domain = os.environ.get("GITHUB_ENTERPRISE_DOMAIN", "")
        if github_enterprise_domain:
            allowed_domains.add(github_enterprise_domain)
            allowed_domains.add(f"api.{github_enterprise_domain}")

        return parsed.netloc in allowed_domains and parsed.scheme == "https"
    except Exception:
        return False


def _sanitize_error_message(error_text: str, max_length: int = 200) -> str:
    """Sanitize error messages to prevent information disclosure."""
    if not error_text:
        return "[empty response]"

    # Remove potential sensitive patterns
    sanitized = re.sub(
        r'(token|key|secret|password|auth)["\']?\s*[:=]\s*["\']?[^"\'\s]+',
        r"\1=[REDACTED]",
        error_text,
        flags=re.IGNORECASE,
    )

    # Truncate to prevent log flooding
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "...[truncated]"

    return sanitized


async def verify_signature(request: Request, body: bytes) -> str | None:
    """
    Verify GitHub webhook signature using HMAC-SHA256.

    Returns the X-GitHub-Delivery header value if valid, for replay protection.
    """
    webhook_secret = os.environ.get("WEBHOOK_SECRET")
    if not webhook_secret:
        logger.error("Webhook secret not configured")
        raise HTTPException(status_code=500, detail="Internal server error")

    # Validate Content-Type
    content_type = request.headers.get("Content-Type", "")
    if "application/json" not in content_type:
        logger.warning(f"Invalid Content-Type: {content_type}")
        raise HTTPException(status_code=400, detail="Invalid Content-Type")

    signature = request.headers.get("X-Hub-Signature-256")
    if not signature:
        logger.error("Missing X-Hub-Signature-256 header")
        raise HTTPException(status_code=403, detail="Signature missing")

    # Get delivery ID for replay protection
    delivery_id = request.headers.get("X-GitHub-Delivery")
    if not delivery_id:
        logger.warning("Missing X-GitHub-Delivery header")
        raise HTTPException(status_code=400, detail="Missing delivery ID")

    hash_object = hmac.new(webhook_secret.encode(), msg=body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        logger.error("Invalid signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    return delivery_id


@app.function(image=runner_image, secrets=[github_secret])
@modal.fastapi_endpoint(method="POST")
async def github_webhook(request: Request):
    # Check body size before reading
    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_SIZE:
                logger.warning(f"Request body too large: {content_length} bytes")
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError:
            pass  # Invalid Content-Length, let it fail later

    body = await request.body()

    # Verify actual body size
    if len(body) > MAX_BODY_SIZE:
        logger.warning(f"Request body too large: {len(body)} bytes")
        raise HTTPException(status_code=413, detail="Payload too large")

    # Verify signature and get delivery ID
    delivery_id = await verify_signature(request, body)

    # Replay protection
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing delivery ID")

    if delivery_id in _processed_deliveries:
        logger.warning(f"Duplicate delivery ID detected: {delivery_id}")
        return {"status": "duplicate", "message": "Request already processed"}

    _cleanup_delivery_cache()
    _processed_deliveries.add(delivery_id)

    try:
        payload = json.loads(body)
    except Exception as e:
        logger.error(f"Failed to parse JSON payload: {type(e).__name__}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Only spawn sandboxes for jobs that are queued
    # GitHub's max-parallel setting is enforced by the job queue
    if payload.get("action") != "queued":
        logger.debug(
            f"Ignoring action '{payload.get('action')}' - only processing queued jobs"
        )
        return {"status": "ignored"}

    workflow_job = payload.get("workflow_job", {})
    repo_url = payload.get("repository", {}).get("url")
    repo_full_name = payload.get("repository", {}).get("full_name", "")
    job_id = workflow_job.get("id", "unknown")
    job_labels = workflow_job.get("labels", [])

    # CHECK FOR MODAL LABEL
    # Ignore jobs that don't explicitly request 'modal' runner
    if "modal" not in job_labels:
        logger.info(
            f"Ignoring job {job_id} without 'modal' label (labels: {job_labels})"
        )
        return {"status": "ignored", "reason": "no modal label"}

    # Repository allowlist validation
    if ALLOWED_REPOS and repo_full_name not in ALLOWED_REPOS:
        logger.warning(f"Rejected webhook from unauthorized repo: {repo_full_name}")
        raise HTTPException(status_code=403, detail="Repository not authorized")

    if not repo_url:
        logger.error("Missing repository URL in payload")
        raise HTTPException(status_code=400, detail="Missing repository URL")

    # Validate repo URL domain
    if not _validate_github_url(repo_url):
        logger.error(
            f"Invalid repository URL domain: {urlparse(repo_url).netloc if repo_url else 'empty'}"
        )
        raise HTTPException(status_code=400, detail="Invalid repository URL")

    # Rate limiting - deduplicate job IDs
    current_time = time.time()
    if str(job_id) in _processed_jobs:
        last_processed = _processed_jobs[str(job_id)]
        if current_time - last_processed < JOB_DEDUP_WINDOW_SECONDS:
            logger.warning(f"Duplicate job ID detected: {job_id}")
            return {"status": "duplicate", "job_id": job_id}

    _cleanup_job_cache()
    _processed_jobs[str(job_id)] = current_time

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

    logger.info(f"Requesting JIT config for job {job_id} from {repo_full_name}...")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                f"{repo_url}/actions/runners/generate-jitconfig",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            jit_config = response.json()["encoded_jit_config"]
        except httpx.HTTPStatusError as e:
            sanitized_error = _sanitize_error_message(e.response.text)
            logger.error(
                f"GitHub API error for job {job_id}: status={e.response.status_code}, response={sanitized_error}"
            )
            raise HTTPException(
                status_code=e.response.status_code,
                detail="Failed to generate JIT config",
            )
        except httpx.TimeoutException:
            logger.error(f"GitHub API timeout for job {job_id}")
            raise HTTPException(status_code=504, detail="GitHub API timeout")
        except Exception as e:
            logger.error(
                f"Unexpected error calling GitHub API for job {job_id}: {type(e).__name__}"
            )
            raise HTTPException(status_code=500, detail="Internal server error")

    logger.info(f"Spawning sandbox for job {job_id}...")

    try:
        # JIT config is base64-encoded by GitHub and used directly by the runner
        # Modal's sandbox isolation provides security boundary
        # JIT tokens are single-use and expire after job completion
        cmd = "cd /actions-runner && export RUNNER_ALLOW_RUNASROOT=1 && ./run.sh --jitconfig $GHA_JIT_CONFIG"

        modal.Sandbox.create(
            "bash",
            "-c",
            cmd,
            image=runner_image,
            app=app,
            timeout=TIMEOUT_SECONDS,
            env={"GHA_JIT_CONFIG": jit_config},
        )
    except Exception as e:
        logger.error(f"Failed to create sandbox for job {job_id}: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Failed to spawn runner sandbox")

    logger.info(f"Successfully provisioned runner for job {job_id}")
    return {"status": "provisioned", "job_id": job_id}
