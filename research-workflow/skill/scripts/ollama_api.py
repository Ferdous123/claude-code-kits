"""Tiny client for a local Ollama server (standard library only).

Used by local_run.py, bakeoff.py and search.py. Talks only to the local machine
(default http://127.0.0.1:11434, or the address in the OLLAMA_HOST variable).
Nothing here sends paper text anywhere except to that local server.
"""
import json
import os
import time
import urllib.error
import urllib.request


def base_url():
    """Server address from OLLAMA_HOST or the default."""
    h = os.environ.get("OLLAMA_HOST", "").strip() or "127.0.0.1:11434"
    h = h.replace("0.0.0.0", "127.0.0.1")
    if not h.startswith("http"):
        h = "http://" + h
    if h.count(":") < 2:
        h += ":11434"
    return h.rstrip("/")


class OllamaError(Exception):
    """Raised when the server is missing, refuses, or returns something unusable."""


def _request(path, payload=None, timeout=600):
    url = base_url() + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise OllamaError("HTTP %s from %s: %s" % (exc.code, path, body))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaError("cannot reach the local model server at %s (%s). Is Ollama running?" % (base_url(), exc))


def version(timeout=3):
    """Server version string, or None if it is not reachable."""
    try:
        return _request("/api/version", None, timeout).get("version")
    except OllamaError:
        return None


def tags(timeout=5):
    """List of models installed: [{name, size_bytes}]."""
    out = []
    for m in _request("/api/tags", None, timeout).get("models", []):
        out.append({"name": m.get("name"), "size_bytes": m.get("size")})
    return out


def has_model(name):
    """True if a model with this tag is installed."""
    try:
        names = [m["name"] for m in tags()]
    except OllamaError:
        return False
    return name in names or (":" not in name and (name + ":latest") in names)


def chat(model, system, user, schema=None, num_ctx=4096, num_predict=1024, temperature=0.0, seed=0, think=False, timeout=600):
    """One chat call. Returns dict(content, done_reason, tokens_in, tokens_out, seconds).

    `schema` is a JSON schema; the server then constrains the output to it.
    Temperature 0 and a fixed seed make repeated runs as repeatable as the server allows.
    """
    payload = {
        "model": model, "stream": False, "think": bool(think),
        "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}],
        "options": {"temperature": temperature, "seed": seed, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    if schema is not None:
        payload["format"] = schema
    t0 = time.time()
    r = _request("/api/chat", payload, timeout)
    msg = r.get("message") or {}
    return {"content": msg.get("content", ""), "done_reason": r.get("done_reason"), "tokens_in": r.get("prompt_eval_count"),
            "tokens_out": r.get("eval_count"), "seconds": round(time.time() - t0, 3), "params": dict(payload["options"], think=bool(think))}


def embed(model, texts, timeout=600):
    """Embed a list of texts. Returns a list of vectors (lists of floats)."""
    r = _request("/api/embed", {"model": model, "input": texts}, timeout)
    vecs = r.get("embeddings")
    if not vecs or len(vecs) != len(texts):
        raise OllamaError("embedding call returned %s vectors for %d texts" % (len(vecs or []), len(texts)))
    return vecs


def unload(model):
    """Ask the server to free the model's memory now (used between roles so only one model is resident)."""
    try:
        _request("/api/generate", {"model": model, "keep_alive": 0}, 30)
    except OllamaError:
        pass
