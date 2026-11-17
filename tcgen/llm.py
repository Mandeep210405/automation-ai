# """
# llm.py — tiny Ollama client (local, free). Uses Ollama "structured outputs":
# we pass a JSON schema in `format`, so the model is forced to reply with JSON
# that matches it. We still validate the result ourselves afterwards.
# """
# from __future__ import annotations

# import json
# import re
# import time
# from collections import defaultdict

# import requests


# class LLMError(RuntimeError):
#     pass


# class OllamaClient:
#     def __init__(self, cfg, logger=None):
#         self.cfg = cfg
#         self.host = cfg.OLLAMA_HOST.rstrip("/")
#         self.logger = logger            # callable(dict) for run log
#         self.usage = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0})

#     # ---------------------------------------------------------------- health
#     def installed_models(self) -> list[str]:
#         r = requests.get(f"{self.host}/api/tags", timeout=10)
#         r.raise_for_status()
#         return [m["name"] for m in r.json().get("models", [])]

#     def check(self) -> list[str]:
#         """Return a list of problems (empty list = all good)."""
#         try:
#             have = self.installed_models()
#         except Exception as exc:
#             return [f"Cannot reach Ollama at {self.host} ({exc}). Is the Ollama app running?"]
#         problems = []
#         for role, model in self.cfg.MODELS.items():
#             ok = model in have or f"{model}:latest" in have
#             if not ok:
#                 problems.append(f"Model '{model}' (role: {role}) is not pulled. Run:  ollama pull {model}")
#         return sorted(set(problems))

#     # ---------------------------------------------------------------- chat
#     def chat_json(self, role: str, system: str, user: str, schema: dict) -> dict:
#         model = self.cfg.MODELS[role]
#         payload = {
#             "model": model,
#             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
#             "stream": False,
#             "format": schema,
#             "keep_alive": self.cfg.OLLAMA_KEEP_ALIVE,
#             "options": {"temperature": self.cfg.OLLAMA_TEMPERATURE, "num_ctx": self.cfg.OLLAMA_NUM_CTX},
#         }
#         if self.cfg.OLLAMA_THINK is not None:
#             payload["think"] = self.cfg.OLLAMA_THINK

#         last_err = None
#         for attempt in range(2):
#             t0 = time.time()
#             try:
#                 r = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.cfg.OLLAMA_TIMEOUT_S)
#             except requests.RequestException as exc:
#                 raise LLMError(f"Ollama request failed: {exc}") from exc
#             if r.status_code != 200:
#                 raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
#             data = r.json()
#             secs = time.time() - t0
#             u = self.usage[role]
#             u["calls"] += 1
#             u["prompt_tokens"] += data.get("prompt_eval_count", 0) or 0
#             u["output_tokens"] += data.get("eval_count", 0) or 0
#             u["seconds"] += secs
#             if (data.get("prompt_eval_count") or 0) >= self.cfg.OLLAMA_NUM_CTX - 50:
#                 print(f"   ! warning: prompt filled the whole context window ({self.cfg.OLLAMA_NUM_CTX}). "
#                       f"Increase OLLAMA_NUM_CTX or lower the *_CONTEXT_TOKENS budgets.")
#             content = data.get("message", {}).get("content", "")
#             if self.logger:
#                 self.logger({"type": "llm", "role": role, "model": model, "seconds": round(secs, 1),
#                              "prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count"),
#                              "system": system, "user": user, "response": content})
#             try:
#                 return _parse_json(content)
#             except ValueError as exc:
#                 last_err = exc
#                 payload["messages"].append({"role": "assistant", "content": content})
#                 payload["messages"].append({"role": "user", "content": "That was not valid JSON. Reply again with ONLY the JSON object."})
#         raise LLMError(f"Model did not return valid JSON: {last_err}")


# def _parse_json(text: str) -> dict:
#     text = (text or "").strip()
#     text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
#     try:
#         obj = json.loads(text)
#     except json.JSONDecodeError:
#         m = re.search(r"\{.*\}", text, re.S)
#         if not m:
#             raise ValueError("no JSON object found")
#         obj = json.loads(m.group(0))
#     if not isinstance(obj, dict):
#         raise ValueError("JSON is not an object")
#     return obj


"""
llm.py — tiny Ollama client. Works with Ollama Cloud (https://ollama.com, API key)
and with a local Ollama server. Uses Ollama "structured outputs":
we pass a JSON schema in `format`, so the model is forced to reply with JSON
that matches it. We still validate the result ourselves afterwards.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict

import requests


class LLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.host = cfg.OLLAMA_HOST.rstrip("/")
        self.logger = logger            # callable(dict) for run log
        self.usage = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0})
        api_key = getattr(cfg, "OLLAMA_API_KEY", "") or ""
        self.is_cloud = "ollama.com" in self.host
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # ---------------------------------------------------------------- health
    def installed_models(self) -> list[str]:
        r = requests.get(f"{self.host}/api/tags", headers=self.headers, timeout=15)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def check(self) -> list[str]:
        """Return a list of problems (empty list = all good)."""
        if self.is_cloud and not self.headers:
            return ["OLLAMA_API_KEY is not set. Create a key at https://ollama.com/settings/keys, "
                    "then set it (Windows: setx OLLAMA_API_KEY \"your_key\" and open a NEW terminal)."]
        try:
            have = self.installed_models()
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            if code in (401, 403):
                return [f"Ollama rejected the API key (HTTP {code}). Check OLLAMA_API_KEY."]
            return [f"Ollama at {self.host} returned HTTP {code}."]
        except Exception as exc:
            where = "Check your internet connection." if self.is_cloud else "Is the Ollama app running?"
            return [f"Cannot reach Ollama at {self.host} ({exc}). {where}"]
        problems = []
        for role, model in self.cfg.MODELS.items():
            ok = model in have or f"{model}:latest" in have
            if not ok:
                if self.is_cloud:
                    problems.append(f"Model '{model}' (role: {role}) is not in the Ollama cloud list. "
                                    f"See https://ollama.com/search?c=cloud (use the name without ':cloud' / '-cloud').")
                else:
                    problems.append(f"Model '{model}' (role: {role}) is not pulled. Run:  ollama pull {model}")
        return sorted(set(problems))

    # ---------------------------------------------------------------- http
    def _post(self, payload: dict) -> requests.Response:
        """POST /api/chat; retries on rate limit / temporary server errors."""
        waits = [5, 15, 30, 60]
        for i in range(len(waits) + 1):
            try:
                r = requests.post(f"{self.host}/api/chat", json=payload, headers=self.headers,
                                  timeout=self.cfg.OLLAMA_TIMEOUT_S)
            except requests.RequestException as exc:
                raise LLMError(f"Ollama request failed: {exc}") from exc
            if r.status_code in (429, 502, 503, 504) and i < len(waits):
                print(f"   ! Ollama busy / rate-limited (HTTP {r.status_code}); retrying in {waits[i]}s ...")
                time.sleep(waits[i])
                continue
            return r
        return r  # pragma: no cover

    # ---------------------------------------------------------------- chat
    def chat_json(self, role: str, system: str, user: str, schema: dict) -> dict:
        model = self.cfg.MODELS[role]
        options = {"temperature": self.cfg.OLLAMA_TEMPERATURE}
        if not self.is_cloud:
            options["num_ctx"] = self.cfg.OLLAMA_NUM_CTX      # local only; cloud manages its own context
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": options,
        }
        if not self.is_cloud:
            payload["keep_alive"] = self.cfg.OLLAMA_KEEP_ALIVE
        if self.cfg.OLLAMA_THINK is not None:
            payload["think"] = self.cfg.OLLAMA_THINK          # False/True, or "low"/"medium"/"high" for gpt-oss

        last_err = None
        for attempt in range(2):
            t0 = time.time()
            r = self._post(payload)
            if r.status_code in (401, 403):
                raise LLMError(f"Ollama rejected the API key (HTTP {r.status_code}). Check OLLAMA_API_KEY.")
            if r.status_code != 200:
                raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
            data = r.json()
            secs = time.time() - t0
            u = self.usage[role]
            u["calls"] += 1
            u["prompt_tokens"] += data.get("prompt_eval_count", 0) or 0
            u["output_tokens"] += data.get("eval_count", 0) or 0
            u["seconds"] += secs
            if not self.is_cloud and (data.get("prompt_eval_count") or 0) >= self.cfg.OLLAMA_NUM_CTX - 50:
                print(f"   ! warning: prompt filled the whole context window ({self.cfg.OLLAMA_NUM_CTX}). "
                      f"Increase OLLAMA_NUM_CTX or lower the *_CONTEXT_TOKENS budgets.")
            content = data.get("message", {}).get("content", "")
            if self.logger:
                self.logger({"type": "llm", "role": role, "model": model, "seconds": round(secs, 1),
                             "prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count"),
                             "system": system, "user": user, "response": content})
            try:
                return _parse_json(content)
            except ValueError as exc:
                last_err = exc
                payload["messages"].append({"role": "assistant", "content": content})
                payload["messages"].append({"role": "user", "content": "That was not valid JSON. Reply again with ONLY the JSON object."})
        raise LLMError(f"Model did not return valid JSON: {last_err}")


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("no JSON object found")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON is not an object")
    return obj