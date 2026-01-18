# Modal GitHub Runner

[![Modal](https://img.shields.io/badge/Powered%20By-Modal-000000?style=flat-square&logo=modal&logoColor=white)](https://modal.com)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Runner-2088FF?style=flat-square&logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)

A high-performance, ephemeral self-hosted GitHub Actions runner powered by [**Modal**](https://modal.com). Achieve zero idle costs and instant horizontal scaling with Just-In-Time (JIT) security.

## 🚀 Key Features

- **⚡ Ephemeral:** Every job runs in a fresh, hardware-isolated Modal Sandbox, ensuring a clean state and preventing side effects between runs.
- **💰 Zero Idle Cost:** No long-running servers or "warm" instances. You only pay for the exact seconds your runner is executing jobs.
- **🛡️ JIT Security:** Utilizes GitHub's Just-In-Time runner registration. Runners are created on-demand and automatically cleaned up by GitHub after a single use.
- **📈 Horizontal Scaling:** Modal's serverless infrastructure allows you to scale to hundreds of concurrent runners instantly. Each job gets its own dedicated resources without queueing delays.

## 🏗️ Architecture

The runner follows a reactive, event-driven flow:

```mermaid
sequenceDiagram
    participant GH as GitHub Actions
    participant WE as Modal Web Endpoint
    participant GA as GitHub API
    participant MS as Modal Sandbox
    
    GH->>WE: 1. workflow_job (queued) Webhook
    Note over WE: Verify Signature (HMAC-SHA256)
    WE->>GA: 2. Request JIT Config (generate-jitconfig)
    GA-->>WE: 3. Return JIT Config String
    WE->>MS: 4. modal.Sandbox.create(image, JIT_CONFIG)
    Note over MS: 5. Execute run.sh (as root in /tmp)
    MS->>GH: 6. Connect & Execute Job
    GH-->>MS: 7. Job Finished
    MS->>MS: 8. Exit & Terminate Sandbox
```

1.  **Workflow Queued:** A GitHub Action workflow is triggered and a job enters the `queued` state.
2.  **Webhook Trigger:** GitHub sends a `workflow_job` webhook to the Modal web endpoint.
3.  **JIT Handshake:** The Modal app validates the request and calls the GitHub API to generate a JIT (Just-In-Time) runner configuration.
4.  **Sandbox Spawning:** A Modal Sandbox is provisioned immediately with the pre-configured runner image.
5.  **Execution & Cleanup:** The runner connects to GitHub, executes the specific job, and the Sandbox is terminated immediately upon completion.

## 🏁 Quick Start

Setting up your own Modal runner takes only a few minutes.

Refer to the [**DEPLOY.md**](DEPLOY.md) for step-by-step instructions on:
- Setting up Modal secrets.
- Deploying the webhook endpoint.
- Configuring GitHub repository webhooks.

## 🛠️ Technical Details

-   **Modal Sandbox:** Built on top of Modal's serverless runtime, providing sub-second startup times and robust isolation using micro-VM technology.
-   **JIT Configuration:** Instead of persistent runner tokens, this project uses the `generate-jitconfig` endpoint. This ensures that even if a runner environment were compromised, the credentials are valid for only one specific job.
-   **Custom Images:** The runner environment is defined directly within `app.py`, allowing you to easily add dependencies (e.g., specific versions of Python, Node.js, or system libraries) that are pre-baked into the runner image.
-   **Root Execution:** Sandboxes run with `RUNNER_ALLOW_RUNASROOT=1` in ephemeral `/tmp` directories, ensuring compatibility with all GitHub Actions features without permission hurdles.

## 📄 License

This project is licensed under the [MIT License](LICENSE).

---

## 👤 Author

**Manas C. Bavaskar**
- GitHub: [@manascb1344](https://github.com/manascb1344)
- Website: [manascb.com](https://manascb.com)
- LinkedIn: [manas-bavaskar](https://linkedin.com/in/manas-bavaskar)
