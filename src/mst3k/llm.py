"""LLM helpers: OpenAI-compatible chat-completions client (stdlib urllib)."""
import base64
import json
import os
import urllib.request


def load_env() -> dict:
    """Read .env from the project root (KEY=value lines, # comments)."""
    env = {}
    path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    path = os.path.abspath(path)
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class LLM:
    def __init__(self, api_base: str, api_key: str, model: str):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model

    def chat(self, messages: list[dict], temperature: float = 0.9,
             max_tokens: int = 2000) -> str:
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        req = urllib.request.Request(
            f"{self.api_base}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.load(resp)
        return data["choices"][0]["message"]["content"]

    def chat_json(self, messages: list[dict], temperature: float = 0.9,
                  max_tokens: int = 2000):
        """Chat + defensive JSON extraction (models love fences/prose)."""
        raw = self.chat(messages, temperature, max_tokens)
        # strip markdown fences wherever they are
        s = raw.replace("```json", "```").replace("```", "")
        return json.loads(_extract_json(s))


def _extract_json(s: str) -> str:
    """Find the first balanced {...} or [...] block, ignoring braces inside
    strings and escaping. Handles trailing/leading prose."""
    start = next((i for i, c in enumerate(s) if c in "[{"), -1)
    if start == -1:
        return s.strip()
    open_c = s[start]
    close_c = "}" if open_c == "{" else "]"
    stack = []
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c in "[{":
            stack.append(c)
        elif c in "]}":
            stack.pop()
            if not stack:
                return s[start:i + 1]
    return s[start:]  # truncated; let json.loads raise for visibility


def b64_image(path) -> str:
    return base64.b64encode(open(path, "rb").read()).decode()


# ---- module-level helpers used by the pipeline stages -----------------

def _client(job: dict) -> "LLM":
    from . import providers
    r = providers.resolve(job)
    return LLM(r["base_url"], r["key"], job.get("_model") or r["model"])


def chat(job: dict, system: str, user, temperature: float = 0.9,
         max_tokens: int = 2000, model: str | None = None) -> str:
    job["_model"] = model
    client = _client(job)
    try:
        return client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens)
    finally:
        job.pop("_model", None)


def chat_json(job: dict, system: str, user, temperature: float = 0.9,
              max_tokens: int = 2000, model: str | None = None):
    job["_model"] = model
    client = _client(job)
    try:
        return client.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens)
    finally:
        job.pop("_model", None)
