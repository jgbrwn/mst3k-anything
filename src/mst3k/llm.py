"""LLM helpers: OpenAI-compatible chat-completions client (stdlib urllib)."""
import base64
import json
import os
import time
import urllib.request
import urllib.error


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
        # transient provider errors (502/503/429/timeout) retry with backoff.
        # 4xx like 400/404 are client errors — fail immediately with detail.
        last_err = None
        for attempt in range(4):
            if attempt:
                time.sleep(2.5 * attempt)  # 2.5s, 5s, 7.5s
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.load(resp)
                last_err = None
                break
            except urllib.error.HTTPError as exc:
                body_repr = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code in (429, 502, 503, 504) and attempt < 3:
                    print(f"    [llm] {exc.code} from {self.model}; retry {attempt+1}/3 in {2*attempt+2}s", flush=True)
                    last_err = RuntimeError(
                        f"LLM {exc.code} at {exc.geturl()}\n"
                        f"requested model={self.model!r}\n"
                        f"response body: {body_repr}")
                    continue
                raise RuntimeError(
                    f"LLM {exc.code} at {exc.geturl()}\n"
                    f"requested model={self.model!r}\n"
                    f"response body: {body_repr}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 3:
                    print(f"    [llm] network error ({exc!r}); retry {attempt+1}/3", flush=True)
                    last_err = exc
                    continue
                raise RuntimeError(f"LLM unreachable at {self.api_base}: {exc}") from exc
        if last_err:
            raise last_err
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

def _client(job: dict, role: str = "write") -> "LLM":
    from . import providers
    r = providers.resolve(job, role=role)
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
              max_tokens: int = 2000, model: str | None = None,
              role: str = "write"):
    job["_model"] = model
    client = _client(job, role=role)
    try:
        return client.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens)
    finally:
        job.pop("_model", None)
