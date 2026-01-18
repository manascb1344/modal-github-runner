# Feasibility Study: Modal.com as a Self-Hosted GitHub Actions Runner

## 1. Executive Summary
Using Modal.com as a self-hosted GitHub Actions runner is **fully feasible** and offers significant advantages for ephemeral, resource-intensive, or GPU-based CI workloads. Modal's `Sandbox` primitive provides a programmable, isolated, and scalable environment that can host the GitHub Runner binary with minimal overhead.

The most robust architecture involves a **Modal Webhook** that listens for GitHub `workflow_job` events and dynamically spawns **ephemeral Modal Sandboxes** to handle individual jobs.

## 2. Modal Host Capabilities: Sandboxes & Containers
Modal's infrastructure is built for exactly this type of workload:
- **`modal.Sandbox`**: This is the core primitive for running arbitrary binaries. A sandbox can be created via the Modal SDK/API and runs a specific command in an isolated container.
- **Custom Images**: You can create a `modal.Image` that contains the GitHub Runner binary and all necessary dependencies (Node.js, Python, Docker-in-Docker via `modal.Sandbox`, etc.).
- **Resources**: Sandboxes allow fine-grained control over CPU, Memory, and GPUs (`gpu="any"`, `gpu="a100"`, etc.). This is ideal for CI tasks that require specific hardware.
- **Timeouts**: Sandboxes support execution times up to **24 hours**, which easily covers standard CI job limits.
- **Auto-scaling**: Modal handles the scaling; if 10 jobs are queued, 10 sandboxes can be spawned simultaneously.

## 3. Community Patterns & Examples
While there is no "official" Modal GitHub Runner, several community patterns exist:
- **`gpu-mode/kernelbot`**: This project uses GitHub Actions to schedule jobs on Modal and Slurm. It demonstrates using GitHub as a scheduler for high-performance computing jobs on Modal.
- **`modal-labs/ci-on-modal`**: A sample repo showing how to run `pytest` suites on Modal from within a GitHub Action. 
- **The "Inversion of Control" Pattern**: Several developers have discussed using Modal Webhooks to receive GitHub events and spawn Sandboxes as runners to leverage Modal's GPU availability and cost-efficiency.

## 4. The "Ephemeral Runner" Workflow
GitHub's **Ephemeral Runners** (using the `--ephemeral` flag) are the recommended approach for Modal.

### High-Level Architecture:
1.  **GitHub Webhook**: Configure a GitHub App or Repository Webhook to send `workflow_job` events to a Modal Webhook (`@app.web_endpoint`).
2.  **Runner Provisioning**: The Modal Webhook function:
    -   Receives the `queued` job event.
    -   Calls the GitHub API to generate a **JIT (Just-In-Time) Configuration** (using `POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig`).
    -   Spawns a `modal.Sandbox` via `modal.Sandbox.create()`.
3.  **Runner Execution**: The Sandbox starts, takes the JIT config as an environment variable, and runs the runner binary:
    -   `./run.sh --jitconfig $GHA_JIT_CONFIG`
4.  **Auto-Cleanup**: After the job completes, the runner automatically exits and deregisters. The Modal Sandbox then terminates, ensuring you only pay for the exact duration of the job.

## 5. Networking and Security Requirements

### Networking
- **Outbound Connectivity**: Modal Sandboxes have outbound internet access by default, allowing them to communicate with GitHub's APIs and download dependencies.
- **No Inbound Requirements**: The GitHub Runner uses a long-polling mechanism. **No ports need to be opened** on the Sandbox, keeping the runner isolated from the public internet.
- **Webhook Security**: The Modal Webhook should validate GitHub's HMAC signature to ensure only authorized events trigger runner creation.

### Security
- **Secrets Management**: GitHub Personal Access Tokens (PATs) or App Private Keys should be stored in **Modal Secrets** and never exposed to the job environment.
- **Isolation**: Modal's sandboxes provide high-grade isolation (similar to gVisor), making them suitable for running untrusted code from pull requests.
- **Least Privilege**: The JIT configuration approach is superior to registration tokens as it limits the runner's scope to a single specific job.

## 6. Challenges & Considerations
- **Startup Latency**: While Modal is fast (sub-second cold starts for some images), downloading large images or the runner binary at runtime can add 10-30 seconds of latency. Pre-baking the binary into the image is recommended.
- **State Persistence**: By default, Sandboxes are stateless. For heavy dependency caching (e.g., `node_modules`), Modal's `NetworkFileSystem` or `Volume` can be mounted to the Sandbox.
- **Orchestration Cost**: There is a small overhead for the Webhook function, but since it only runs for a few seconds per job, the cost is negligible compared to the runner itself.

## 7. Conclusion
Modal.com is an **excellent** platform for hosting self-hosted GitHub Runners, particularly for teams that need **on-demand GPU access** or want to avoid the cost of persistent VMs. The "Webhook-to-Sandbox" pattern is the most efficient and scalable implementation method.
