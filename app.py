import modal
import os
import hmac
import hashlib
import logging
import httpx
import json
import time
import re
import yaml
import asyncio
from urllib.parse import urlparse
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
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
# QUEUE-BASED SANDBOX PROVISIONING
# =============================================================================
# Prevents creating idle sandboxes by respecting per-workflow max-parallel limits.
# Each workflow run has its own queue and active job count.


@dataclass
class QueuedJob:
    """A job waiting in the queue for a slot to open."""

    job_id: str
    jit_config: str
    provider: str  # From job name (e.g., "Search ANTHROPIC (Modal)")
    run_id: str
    repo_full_name: str
    created_at: float = field(default_factory=time.time)


@dataclass
class RunConfig:
    """Configuration for a single workflow run, including queue and limits."""

    max_parallel: int
    active_count: int = 0
    queue: deque[QueuedJob] = field(default_factory=deque)
    workflow_name: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class ActiveJob:
    """A job currently running in a sandbox."""

    sandbox: modal.Sandbox
    run_id: str
    created_at: float = field(default_factory=time.time)


# Per-run configuration (keyed by run_id)
# Each workflow run gets its own queue and max_parallel limit
_run_configs: dict[str, RunConfig] = {}

# Active jobs (keyed by job_id for cancellation lookup)
_active_jobs: dict[str, ActiveJob] = {}

# Initialization lock for run configs to prevent race conditions
_run_config_lock = asyncio.Lock()

# Stale run cleanup threshold (24 hours)
RUN_STALE_THRESHOLD_SECONDS = 86400


def _cleanup_stale_runs():
    """Remove run configs that have been inactive for too long."""
    global _run_configs
    current_time = time.time()
    stale_run_ids = [
        run_id
        for run_id, config in _run_configs.items()
        if current_time - config.created_at > RUN_STALE_THRESHOLD_SECONDS
    ]
    for run_id in stale_run_ids:
        logger.info(f"Cleaning up stale run config: {run_id}")
        del _run_configs[run_id]


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
    .pip_install("fastapi==0.115.0", "httpx==0.27.0", "pyyaml")
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


async def fetch_workflow_max_parallel(
    repo_url: str, workflow_name: str, github_token: str
) -> int:
    """
    Fetch the max-parallel setting from a workflow YAML file.

    Returns the max-parallel value if found, or a sensible default (2).
    """
    try:
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            # List workflow runs to get the workflow file path
            response = await client.get(
                f"{repo_url}/actions/workflows",
                headers=headers,
            )
            response.raise_for_status()

            workflows = response.json().get("workflows", [])
            workflow_file = None
            for wf in workflows:
                if wf.get("name") == workflow_name:
                    workflow_file = wf.get("path")
                    break

            if not workflow_file:
                logger.warning(f"Could not find workflow file for: {workflow_name}")
                return 2  # Default

            # Fetch the workflow YAML content
            response = await client.get(
                f"{repo_url}/contents/{workflow_file}",
                headers=headers,
            )
            response.raise_for_status()

            import base64

            content = base64.b64decode(response.json()["content"]).decode("utf-8")

            # Parse YAML to find max-parallel
            # Look for: jobs.<job>.strategy.max-parallel or strategy.max-parallel
            import yaml

            config = yaml.safe_load(content)

            # Check job-level max-parallel
            jobs = config.get("jobs", {})
            for job_name, job_config in jobs.items():
                if isinstance(job_config, dict):
                    strategy = job_config.get("strategy", {})
                    max_parallel = strategy.get("max-parallel")
                    if max_parallel is not None:
                        logger.info(
                            f"Found max-parallel={max_parallel} for job '{job_name}' "
                            f"in workflow '{workflow_name}'"
                        )
                        return int(max_parallel)

            # Check global strategy max-parallel
            global_strategy = config.get("strategy", {})
            max_parallel = global_strategy.get("max-parallel")
            if max_parallel is not None:
                logger.info(
                    f"Found global max-parallel={max_parallel} in workflow '{workflow_name}'"
                )
                return int(max_parallel)

            logger.info(f"No max-parallel found in '{workflow_name}', using default: 2")
            return 2  # Default if not specified

    except Exception as e:
        logger.warning(f"Failed to fetch max-parallel for '{workflow_name}': {e}")
        return 2  # Default on any error


async def _spawn_sandbox(
    jit_config: str,
    job_id: str,
    run_id: str,
) -> modal.Sandbox:
    """Create a Modal sandbox with the given JIT config."""
    cmd = "cd /actions-runner && export RUNNER_ALLOW_RUNASROOT=1 && ./run.sh --jitconfig $GHA_JIT_CONFIG"

    sandbox = modal.Sandbox.create(
        "bash",
        "-c",
        cmd,
        image=runner_image,
        app=app,
        timeout=TIMEOUT_SECONDS,
        env={"GHA_JIT_CONFIG": jit_config},
    )

    # Tag sandbox for cancellation handling
    sandbox.set_tags({"job_id": str(job_id)})

    # Track active job
    _active_jobs[str(job_id)] = ActiveJob(
        sandbox=sandbox,
        run_id=run_id,
    )

    logger.info(f"Spawned sandbox for job {job_id} in run {run_id}")
    return sandbox


async def _try_process_queue(run_id: str) -> Optional[modal.Sandbox]:
    """
    Try to spawn a job from the queue for the given run.

    Returns the spawned Sandbox if successful, None otherwise.
    """
    if run_id not in _run_configs:
        return None

    config = _run_configs[run_id]

    # Check if we have capacity and queued jobs
    if config.active_count >= config.max_parallel:
        return None

    if not config.queue:
        return None

    # Get oldest queued job
    queued_job = config.queue.popleft()

    try:
        # Create sandbox for the dequeued job
        sandbox = await _spawn_sandbox(queued_job.jit_config, queued_job.job_id, run_id)
        config.active_count += 1

        logger.info(
            f"Dequeued and spawned job {queued_job.job_id} for run {run_id} "
            f"(active: {config.active_count}/{config.max_parallel}, queue remaining: {len(config.queue)})"
        )
        return sandbox
    except Exception as e:
        logger.error(f"Failed to spawn dequeued job {queued_job.job_id}: {e}")
        # Put it back at the front of the queue
        config.queue.appendleft(queued_job)
        return None


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

    # Only process jobs that are queued or completed
    action = payload.get("action")
    workflow_run = payload.get("workflow_run", {})
    workflow_job = payload.get("workflow_job", {})
    run_id = str(workflow_run.get("id", "unknown"))
    job_id = str(workflow_job.get("id", "unknown"))
    job_name = workflow_job.get("name", "unknown")

    # Handle non-queued actions (cancellation, completion)
    if action != "queued":
        if action == "completed":
            conclusion = workflow_job.get("conclusion", "")

            # Handle cancellation - terminate sandbox
            if conclusion == "cancelled":
                # Check if job was in active jobs
                if job_id in _active_jobs:
                    try:
                        _active_jobs[job_id].sandbox.terminate()
                        logger.info(f"Terminated sandbox for cancelled job {job_id}")
                    except Exception as e:
                        logger.error(
                            f"Failed to terminate sandbox for job {job_id}: {type(e).__name__}"
                        )
                    finally:
                        del _active_jobs[job_id]

                        # Decrement active count and try to process queue
                        if run_id in _run_configs:
                            _run_configs[run_id].active_count = max(
                                0, _run_configs[run_id].active_count - 1
                            )
                            await _try_process_queue(run_id)

                # Fallback: also check by tag for robustness
                else:
                    for sb in modal.Sandbox.list(
                        app_id=app.app_id, tags={"job_id": job_id}
                    ):
                        if sb.poll() is None:  # Still running
                            logger.info(
                                f"Terminating sandbox for cancelled job {job_id}"
                            )
                            try:
                                sb.terminate()
                            except Exception as e:
                                logger.error(
                                    f"Failed to terminate sandbox for job {job_id}: {type(e).__name__}"
                                )

                return {"status": "terminated", "job_id": job_id}

            # Normal completion - just clean up tracking
            if job_id in _active_jobs:
                del _active_jobs[job_id]

                # Decrement active count and try to process queue
                if run_id in _run_configs:
                    _run_configs[run_id].active_count = max(
                        0, _run_configs[run_id].active_count - 1
                    )
                    await _try_process_queue(run_id)

                logger.info(
                    f"Job {job_id} completed, active count: {_run_configs[run_id].active_count}"
                )

            return {"status": "completed", "job_id": job_id}

        logger.debug(
            f"Ignoring action '{action}' - only processing queued/completed jobs"
        )
        return {"status": "ignored"}

    # Extract job details from webhook
    repo_url = payload.get("repository", {}).get("url")
    repo_full_name = payload.get("repository", {}).get("full_name", "")
    job_labels = workflow_job.get("labels", [])
    workflow_name = workflow_run.get("name", "")

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

    # Get or create run config for this workflow run
    # Use lock to prevent race conditions when multiple webhooks arrive simultaneously
    async with _run_config_lock:
        if run_id not in _run_configs:
            # Fetch max-parallel from workflow YAML
            max_parallel = await fetch_workflow_max_parallel(
                repo_url, workflow_name, os.environ["GITHUB_TOKEN"]
            )
            _run_configs[run_id] = RunConfig(
                max_parallel=max_parallel,
                workflow_name=workflow_name,
            )
            logger.info(
                f"Created run config for {repo_full_name}/{workflow_name} "
                f"(run_id={run_id}) with max_parallel={max_parallel}"
            )

    run_config = _run_configs[run_id]
    queue_position = len(run_config.queue) + run_config.active_count + 1

    # Fetch configuration from environment with defaults
    runner_group_id = int(os.environ.get("RUNNER_GROUP_ID", 1))

    # Use labels from the webhook directly - workflow defines unique labels
    # Workflow should use: runs-on: [self-hosted, modal, job-${{ github.run_id }}-${{ strategy.job-index }}]
    # This ensures 1:1 binding between runner and job
    # Note: job_labels is guaranteed to contain "modal" at this point (checked at line 525)
    runner_labels = job_labels

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

    logger.info(
        f"Requesting JIT config for job {job_id} (run {run_id}, queue position: {queue_position})"
    )

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

    # Check if we have capacity for this job
    if run_config.active_count < run_config.max_parallel:
        # Create sandbox immediately - we have capacity
        try:
            await _spawn_sandbox(jit_config, job_id, run_id)
            run_config.active_count += 1
            logger.info(
                f"Provisioned runner for job {job_id} "
                f"(active: {run_config.active_count}/{run_config.max_parallel})"
            )
            return {
                "status": "provisioned",
                "job_id": job_id,
                "active_count": run_config.active_count,
            }
        except Exception as e:
            logger.error(
                f"Failed to create sandbox for job {job_id}: {type(e).__name__}"
            )
            raise HTTPException(
                status_code=500, detail="Failed to spawn runner sandbox"
            )
    else:
        # Queue this job - we're at capacity
        queued_job = QueuedJob(
            job_id=job_id,
            jit_config=jit_config,
            provider=job_name,
            run_id=run_id,
            repo_full_name=repo_full_name,
        )
        run_config.queue.append(queued_job)
        logger.info(
            f"Queued job {job_id} for run {run_id} "
            f"(position: {len(run_config.queue)}, active: {run_config.active_count}/{run_config.max_parallel})"
        )
        return {
            "status": "queued",
            "job_id": job_id,
            "queue_position": len(run_config.queue),
            "active_count": run_config.active_count,
            "max_parallel": run_config.max_parallel,
        }
