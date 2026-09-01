import os
import hmac
import hashlib
import requests
import json
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

GITHUB_SECRET = os.getenv("GITHUB_SECRET", "your_webhook_secret")
GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
OLLAMA_URL    = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

# ─── SYSTEM PROMPT ────────────────────────────────────────────
SYSTEM_PROMPT = """You are a professional code reviewer. Your job is to review code submitted to you and provide structured, actionable feedback.

When reviewing code, always respond in this exact format:

## Summary
One or two sentences describing what the code does.

## Issues Found

### [CRITICAL] Issue title
**Line:** X
**Problem:** Explain what is wrong and why it is a security or correctness risk.
**Fix:**
```
corrected code here
```

### [MEDIUM] Issue title
**Line:** X
**Problem:** Explain the issue.
**Fix:**
```
corrected code here
```

### [LOW] Issue title
**Line:** X
**Problem:** Explain the issue.
**Fix:**
```
corrected code here
```

## Overall Assessment
A brief overall assessment of the code quality.

Rules:
- Only review the submitted code. Do not answer unrelated questions.
- If asked something unrelated to the submitted code, respond: "I can only assist with reviewing the submitted code."
- Be concise and specific. Reference actual line numbers.
- If the code has no issues, say so clearly.
- Severity levels: CRITICAL (security/crash), MEDIUM (logic/performance), LOW (style/minor)
"""

# ─── OLLAMA CALL ──────────────────────────────────────────────
def review_code(code, language="auto", follow_up=None, history=None):
    messages = []

    if history:
        messages.extend(history)
    else:
        messages.append({
            "role": "user",
            "content": f"Please review this {language} code:\n\n```{language}\n{code}\n```"
        })

    if follow_up:
        messages.append({"role": "user", "content": follow_up})

    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.2}
            },
            timeout=120
        )
        response.raise_for_status()
        data = response.json()
        return data["message"]["content"], None
    except requests.exceptions.ConnectionError:
        return None, "Cannot connect to Ollama. Make sure Ollama is running on your machine."
    except requests.exceptions.Timeout:
        return None, "Request timed out. The model is taking too long to respond."
    except Exception as e:
        return None, str(e)

# ─── ROUTES ───────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", model=OLLAMA_MODEL)
@app.route("/api/review", methods=["POST"])
def api_review():
    data = request.get_json()
    code     = data.get("code", "").strip()
    language = data.get("language", "auto")

    if not code:
        return jsonify({"error": "No code provided."}), 400

    if len(code) > 50000:
        return jsonify({"error": "Code too large."}), 400

    def generate():
        try:
            response = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Review this {language} code:\n\n```{language}\n{code}\n```"}
                    ],
                    "stream": True,
                    "options": {"temperature": 0.2}
                },
                stream=True,
                timeout=(10, 300)
            )
            for line in response.iter_lines():
                if line:
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        yield f"data: {json.dumps({'token': token})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data     = request.get_json()
    code     = data.get("code", "").strip()
    language = data.get("language", "auto")
    question = data.get("question", "").strip()
    history  = data.get("history", [])

    if not question:
        return jsonify({"error": "No question provided."}), 400

    if not history and not code:
        return jsonify({"error": "No code context available."}), 400

    # Build history if this is first message
    if not history:
        history = [
            {
                "role": "user",
                "content": f"Please review this {language} code:\n\n```{language}\n{code}\n```"
            }
        ]

    history.append({"role": "user", "content": question})

    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
                "stream": False,
                "options": {"temperature": 0.2}
            },
            timeout=120
        )
        response.raise_for_status()
        result = response.json()["message"]["content"]
        history.append({"role": "assistant", "content": result})
        return jsonify({"reply": result, "history": history})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/models", methods=["GET"])
def api_models():
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in response.json().get("models", [])]
        return jsonify({"models": models, "current": OLLAMA_MODEL})
    except:
        return jsonify({"models": [], "current": OLLAMA_MODEL, "error": "Ollama not reachable"})

@app.route("/api/status", methods=["GET"])
def api_status():
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return jsonify({"ollama": "connected", "model": OLLAMA_MODEL})
    except:
        return jsonify({"ollama": "disconnected", "model": OLLAMA_MODEL})

# ─── GITHUB WEBHOOK ───────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def github_webhook():
    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    body = request.get_data()

    expected = "sha256=" + hmac.new(
        GITHUB_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "Invalid signature"}), 403

    event = request.headers.get("X-GitHub-Event", "")
    payload = request.get_json()

    if event == "pull_request":
        action = payload.get("action", "")

        if action in ["opened", "synchronize"]:
            pr = payload["pull_request"]
            diff_url  = pr["diff_url"]
            pr_number = pr["number"]
            repo      = payload["repository"]["full_name"]
            pr_url    = pr["url"]

            try:
                # Fetch the diff
                headers = {}
                if GITHUB_TOKEN:
                    headers["Authorization"] = f"token {GITHUB_TOKEN}"
                    headers["Accept"] = "application/vnd.github.v3.diff"

                diff_response = requests.get(diff_url, headers=headers, timeout=30)
                diff = diff_response.text

                if not diff.strip():
                    return jsonify({"status": "empty diff"}), 200

                # Trim very large diffs
                if len(diff) > 30000:
                    diff = diff[:30000] + "\n\n[Diff truncated — showing first 30000 characters]"

                # Review
                review_prompt = f"Review this pull request diff:\n\n```diff\n{diff}\n```"
                review, error = review_code(review_prompt, language="diff")

                if error:
                    print(f"Review error: {error}")
                    return jsonify({"status": "review_error", "error": error}), 500

                # Post comment back to GitHub PR
                if GITHUB_TOKEN:
                    comment_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
                    comment_body = f"## 🔍 Local AI Code Review\n\n*Reviewed by {OLLAMA_MODEL} running locally — no code was sent to any external AI provider.*\n\n---\n\n{review}"

                    requests.post(
                        comment_url,
                        headers={
                            "Authorization": f"token {GITHUB_TOKEN}",
                            "Accept": "application/vnd.github.v3+json"
                        },
                        json={"body": comment_body},
                        timeout=15
                    )

                print(f"Reviewed PR #{pr_number} on {repo}")
                return jsonify({"status": "reviewed", "pr": pr_number})

            except Exception as e:
                print(f"Webhook error: {e}")
                return jsonify({"error": str(e)}), 500

    return jsonify({"status": "ignored"}), 200

if __name__ == "__main__":
    print(f"\n  Local AI Code Reviewer")
    print(f"  Model : {OLLAMA_MODEL}")
    print(f"  Ollama: {OLLAMA_URL}")
    print(f"  Open  : http://localhost:5001\n")
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
