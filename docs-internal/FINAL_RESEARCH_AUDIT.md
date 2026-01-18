# Final Research Audit: Modal GitHub Runner

## 1. Codebase Audit & Analysis

This audit examines the `modal-github-runner/` repository for technical issues, security vulnerabilities, and performance bottlenecks.

### 1.1 Technical Issues
*   **Async/Sync Mismatch**: The `github_webhook` function in `app.py` is `async`, but it uses the `requests` library, which is synchronous and blocking. Under high volume, this can block the FastAPI event loop, leading to increased latency or timeouts for other incoming webhooks.
    *   *Recommendation*: Replace `requests` with `httpx` for non-blocking asynchronous HTTP calls.
*   **Redundant Image Definitions**: Both `app.py` and `image.py` define a `runner_image`. However, `app.py` contains a more robust version that includes the creation of a non-root `runner` user.
    *   *Recommendation*: Centralize the image definition in `image.py` and import it into `app.py`.
*   **Incomplete Error Handling**: The JIT config generation (`requests.post(...).raise_for_status()`) lacks a `try/except` block. If GitHub's API is down or the token is invalid, the webhook will return a generic 500 error without detailed logging.
    *   *Recommendation*: Implement structured error handling to log specific failure reasons and provide meaningful responses.
*   **Hardcoded Runner Version**: The GitHub runner version (`v2.311.0`) is hardcoded in the image build. While good for stability, it requires manual updates for security patches.

### 1.2 Security Vulnerabilities
*   **Missing Webhook Authentication (CRITICAL)**: The application does not verify the `X-Hub-Signature-256` HMAC header from GitHub. This means any attacker who discovers the webhook URL can trigger the provisioning of Modal Sandboxes, leading to potential resource exhaustion and unauthorized costs.
    *   *Recommendation*: Implement HMAC signature verification using a shared secret.
*   **Sudo Usage**: The image installs `sudo` but doesn't explicitly restrict its use. While the runner runs as a non-root user, the presence of `sudo` without a password (if configured) or with potential exploits could lead to container-level privilege escalation.
    *   *Recommendation*: Minimize `sudo` usage or configure `/etc/sudoers` with the principle of least privilege.
*   **Secrets Exposure**: The JIT configuration is passed as a command-line argument to the sandbox. While Modal Sandboxes are isolated, it is generally safer to pass such configurations via environment variables to avoid them appearing in process lists.

### 1.3 Performance Bottlenecks
*   **Sandbox Startup Latency**: Copying the runner files from `/actions-runner` to the home directory (`cp -r /actions-runner/* ~/`) happens on every sandbox start. This adds avoidable overhead.
    *   *Recommendation*: Pre-configure the runner in the image so it's ready to run from its final location.
*   **Lack of Concurrency Management**: There is no mechanism to limit the total number of concurrent sandboxes. A sudden spike in GitHub Actions could launch hundreds of sandboxes, potentially exceeding Modal account limits or incurring significant costs.

---

## 2. Deep Research

### 2.1 Modal Sandbox Security & Best Practices
Modal Sandboxes are built on **gVisor**, a user-space kernel that intercepts system calls and provides a strong isolation boundary between the application and the host kernel.

**Best Practices for Modal Sandboxes:**
*   **Non-Root Execution**: Always use the `user=` parameter in `Sandbox.create()` to run as a non-privileged user.
*   **Minimalist Images**: Use `debian_slim` or `alpine` to reduce the attack surface and improve startup speed.
*   **Explicit Timeouts**: Always set a `timeout` (e.g., `timeout=3600`) to ensure sandboxes are automatically reaped if the runner process hangs.

### 2.2 GitHub Actions Runner Isolation: Root vs. Non-Root
Running a GitHub Action as `root` is discouraged because any malicious code in a dependency or a pull request would have full control over the container environment.

*   **Risk of Root**: If an attacker escapes the container, having root privileges inside the container significantly simplifies host exploitation.
*   **Non-Root Benefits**: Restricts file system access, prevents unauthorized system-level changes, and provides a closer approximation of a standard developer environment.

### 2.3 Webhook Security (HMAC Signature Verification)
GitHub provides an `X-Hub-Signature-256` header which is an HMAC hex digest of the request body, using the webhook secret as the key.

**Implementation Example (FastAPI):**
```python
import hmac
import hashlib
from fastapi import Request, HTTPException

async def verify_signature(request: Request, secret: str):
    signature = request.headers.get("X-Hub-Signature-256")
    if not signature:
        raise HTTPException(status_code=403, detail="Signature missing")
    
    body = await request.body()
    hash_object = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    
    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")
```

### 2.4 Resource Management (Handling Concurrent Runners)
Handling many concurrent runners on Modal requires balancing responsiveness with cost control.

*   **Concurrency Limits**: Use `modal.Dict` to track active `job_id`s. In the webhook, check the current count before spawning a new sandbox.
*   **Queueing**: If Modal account limits are hit, consider implementing a simple queue (e.g., using a Modal Queue) to buffer incoming requests.

---

## 3. Final Recommendations Summary
1.  **Security**: Immediately implement HMAC verification for the webhook endpoint.
2.  **Architecture**: Consolidate image definitions into `image.py`.
3.  **Efficiency**: Switch to `httpx` and optimize sandbox initialization.
4.  **Governance**: Add basic concurrency tracking to prevent runaway costs.
