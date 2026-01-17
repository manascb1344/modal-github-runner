import modal

# Externalized constants
RUNNER_VERSION = "2.311.0"

# Canonical runner image definition
# Includes essential packages for the GitHub runner and common build tools
runner_image = (
    modal.Image.debian_slim()
    .apt_install(
        'curl',
        'git',
        'ca-certificates',
        'sudo',
        'jq'
    )
    .pip_install("fastapi", "httpx")
    .run_commands(
        # Create a directory for the runner
        'mkdir /actions-runner',
        # Download and extract the GitHub runner binary
        f'curl -L https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz | tar -xz -C /actions-runner',
        # Install dependencies required by the runner
        '/actions-runner/bin/installdependencies.sh',
        # Create a non-root 'runner' user for security
        'useradd -m runner',
        # Ensure the runner user owns the runner directory
        'chown -R runner:runner /actions-runner'
    )
)
