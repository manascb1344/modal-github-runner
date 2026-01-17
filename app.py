import modal
import os
import requests

from image import runner_image

app = modal.App("modal-github-runner")

github_secret = modal.Secret.from_name("github-secret")

@app.function(image=runner_image, secret=github_secret)
@modal.web_endpoint(method="POST")
async def github_webhook(request: modal.Request):
    payload = await request.json()

    if payload["action"] != "queued":
        return "Ignoring action"

    repo_url = payload["repository"]["url"]

    headers = {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }
    data = {
        "name": "modal-runner",
        "runner_group_id": 1,
        "labels": ["self-hosted", "modal"],
        "work_directory": "_work",
    }
    response = requests.post(f"{repo_url}/actions/runners/generate-jitconfig", headers=headers, json=data)
    response.raise_for_status()

    jit_config = response.json()
    encoded_jit_config = jit_config['encoded_jit_config']

    await modal.Sandbox.create(
        command=['/actions-runner/run.sh', '--jitconfig', encoded_jit_config],
        image=runner_image,
        app=app,
    )



