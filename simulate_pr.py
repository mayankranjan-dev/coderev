import hmac
import hashlib
import json
import requests

SECRET = b"your_secret_here"
URL = "http://127.0.0.1:8000/api/webhook/github"

# Mock GitHub Webhook Payload.
# Shape mirrors a real "pull_request" event: repo info lives in the
# top-level "repository" object, and "pull_request.html_url"/"diff_url"
# are both present, same as GitHub actually sends.
payload = {
    "action": "opened",
    "number": 42,
    "repository": {
        "name": "fastapi",
        "full_name": "fastapi/fastapi",
    },
    "pull_request": {
        "number": 42,
        "title": "Fix: Implement exponential backoff for DB retries",
        "html_url": "https://github.com/fastapi/fastapi/pull/10000",
        # Real public diff URL for testing
        "diff_url": "https://github.com/fastapi/fastapi/pull/10000.diff",
        "head": {"sha": "f3g4h5j6k7l8"},
        "base": {
            "repo": {
                "name": "coderev-bot",
                "owner": {"login": "mayankranjan-dev"}
            }
        }
    }
}

body = json.dumps(payload).encode("utf-8")
signature = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()

headers = {
    "X-Hub-Signature-256": signature,
    "X-GitHub-Event": "pull_request",
    "Content-Type": "application/json"
}

print(f"Sending signed webhook to {URL}...")
try:
    response = requests.post(URL, data=body, headers=headers)
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.json()}")
except Exception as e:
    print(f"Connection failed: {e}")