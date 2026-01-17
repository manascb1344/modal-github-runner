## Deployment Guide

This guide outlines the steps to deploy this project using Modal.

### Pre-requisites:

*   A Modal account.
*   The `modal` CLI tool installed.

### Steps:

1.  **Create a GitHub Personal Access Token (PAT):**
    *   Generate a PAT with the `repo` or `workflow` scope.

2.  **Define a Webhook Secret:**
    *   Create a random string to use as your `WEBHOOK_SECRET`.

3.  **Create a Modal Secret:**
    ```bash
    modal secret create github-secret GITHUB_TOKEN=your_pat_here WEBHOOK_SECRET=your_webhook_secret_here
    ```
    *   Replace `your_pat_here` with the PAT you generated.
    *   Replace `your_webhook_secret_here` with your random string.

4.  **Deploy the app:**
    ```bash
    modal deploy app.py
    ```

5.  **Configure the GitHub Webhook:**
    *   Go to your repository Settings > Webhooks > Add webhook.
    *   **Payload URL**: Use the URL provided by `modal deploy`.
    *   **Content type**: `application/json`.
    *   **Secret**: Use the same `WEBHOOK_SECRET` you defined in step 2.
    *   **Events**: Select `Let me select individual events` and check `Workflow jobs`.

6.  **Update your GitHub Actions workflow:**
    *   Ensure the `runs-on` field includes `modal` and `self-hosted`.

    ```yaml
    runs-on: [self-hosted, modal]
    ```

### How it Works

Every time a job is queued, Modal will spawn an ephemeral sandbox that runs the job and then exits. This ensures a clean and isolated environment for each job execution. The webhook is secured using HMAC-SHA256 signature verification.
