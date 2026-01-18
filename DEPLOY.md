## Deployment Guide

This guide outlines the steps to deploy this project using Modal.

### Pre-requisites:

*   A Modal account.
*   The `modal` CLI tool installed.

### Steps:

1.  **Create a GitHub Personal Access Token (PAT):**

    > ⚠️ **Security Best Practice**: Use a **Fine-grained Personal Access Token** instead of a classic PAT for minimal permissions.

    **Option A: Fine-grained PAT (Recommended)**
    - Go to GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens
    - Set **Repository access** to "Only select repositories" and choose your target repos
    - Under **Permissions**, set:
      - `Actions`: Read and Write (required for JIT runner registration)
      - `Administration`: Read and Write (required for runner management)
    - This limits the token to specific repositories only

    **Option B: Classic PAT (Less Secure)**
    - Generate a PAT with the `repo` and `workflow` scope
    - ⚠️ This grants broad access to all your repositories

2.  **Define a Webhook Secret (Mandatory):**
    *   Create a random string to use as your `WEBHOOK_SECRET`. This is required for validating that requests actually come from GitHub.
    *   Generate a secure random secret:
        ```bash
        openssl rand -hex 32
        ```

3.  **Configure Repository Allowlist (Recommended):**
    *   For additional security, specify which repositories can trigger runners:
    *   Format: comma-separated list of `owner/repo` names

4.  **Create a Modal Secret:**
    ```bash
    modal secret create github-secret \
      GITHUB_TOKEN=your_pat_here \
      WEBHOOK_SECRET=your_webhook_secret_here \
      ALLOWED_REPOS="owner/repo1,owner/repo2"
    ```
    *   Replace `your_pat_here` with the PAT you generated.
    *   Replace `your_webhook_secret_here` with your random string.
    *   Replace `owner/repo1,owner/repo2` with your allowed repositories (or omit to allow all).

    **Optional Configuration:**
    ```bash
    # Additional optional settings
    modal secret create github-secret \
      GITHUB_TOKEN=your_pat_here \
      WEBHOOK_SECRET=your_webhook_secret_here \
      ALLOWED_REPOS="owner/repo1,owner/repo2" \
      RUNNER_VERSION="2.311.0" \
      RUNNER_GROUP_ID="1" \
      RUNNER_LABELS='["self-hosted", "modal"]'
    ```

5.  **Deploy the app:**
    ```bash
    modal deploy app.py
    ```

6.  **Configure the GitHub Webhook:**
    *   Go to your repository Settings > Webhooks > Add webhook.
    *   **Payload URL**: Use the URL provided by `modal deploy`.
    *   **Content type**: `application/json`.
    *   **Secret**: Use the same `WEBHOOK_SECRET` you defined in step 2.
    *   **Events**: Select `Let me select individual events` and check `Workflow jobs`.

7.  **Update your GitHub Actions workflow:**
    *   Ensure the `runs-on` field includes `modal` and `self-hosted`.

    ```yaml
    runs-on: [self-hosted, modal]
    ```

### ⚠️ Security Considerations

*   **Trust Model:** This runner executes with root privileges in isolated Modal sandboxes. Only allow trusted repositories via `ALLOWED_REPOS`.
*   **JIT Tokens:** Runner tokens are single-use and job-specific, limiting exposure if compromised.
*   **Ephemeral Execution:** Each job runs in a fresh sandbox that is destroyed after completion.
*   **Webhook Verification:** All requests are verified using HMAC-SHA256 signature validation.

### ⚠️ Limitations

*   **Docker-in-Docker:** Standard GitHub "Container Actions" (actions that run inside a Docker container) are not supported by default within Modal Sandboxes.
*   **Wiping State:** Every job runs in a fresh sandbox. Files saved outside the repository workspace will be lost after the job completes.

### How it Works

Every time a job is queued, Modal will spawn an ephemeral sandbox that runs the job and then exits. This ensures a clean and isolated environment for each job execution. The webhook is secured using HMAC-SHA256 signature verification.

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | Yes | - | GitHub PAT for runner registration |
| `WEBHOOK_SECRET` | Yes | - | Secret for webhook signature validation |
| `ALLOWED_REPOS` | No | (all) | Comma-separated allowlist of `owner/repo` |
| `RUNNER_VERSION` | No | `2.311.0` | GitHub Actions runner version |
| `RUNNER_GROUP_ID` | No | `1` | Runner group ID |
| `RUNNER_LABELS` | No | `["self-hosted", "modal"]` | JSON array of runner labels |
| `GITHUB_ENTERPRISE_DOMAIN` | No | - | Custom domain for GitHub Enterprise |
