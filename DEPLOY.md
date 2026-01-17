## Deployment Guide

This guide outlines the steps to deploy this project using Modal.

### Pre-requisites:

*   A Modal account.
*   The `modal` CLI tool installed.

### Steps:

1.  **Create a GitHub Personal Access Token (PAT):**
    *   Generate a PAT with the `repo` scope.

2.  **Create a Modal Secret:**
    ```bash
    modal secret create github-secret GITHUB_TOKEN=your_pat_here
    ```
    *   Replace `your_pat_here` with the PAT you generated.

3.  **Deploy the app:**
    ```bash
    modal deploy app.py
    ```

4.  **Configure the GitHub Webhook:**
    *   Use the URL provided by `modal deploy`.
    *   Set the Content type to `application/json`.
    *   Select the `Workflow jobs` event.

5.  **Update your GitHub Actions workflow:**
    *   Ensure the `runs-on` field includes `modal` and `self-hosted`.

    ```yaml
    runs-on: [self-hosted, modal]
    ```

### How it Works

Every time a job is queued, Modal will spawn an ephemeral sandbox that runs the job and then exits. This ensures a clean and isolated environment for each job execution.