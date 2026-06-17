"""
Minimal GPT (OpenAI) client for WARDEN's neutral judge role.
Loads the API key from ~/.warden/secrets.env (never from the repo) and exposes
ask_gpt(prompt). Used as the independent judge in the review debate, so the
final verdict comes from a different model family than either reviewer.
"""
import os
from pathlib import Path

_DEFAULT_MODEL = "gpt-5.4"
_loaded = False


def _load_secrets():
    global _loaded
    if _loaded:
        return
    secrets = Path.home() / ".warden" / "secrets.env"
    if secrets.exists():
        for line in secrets.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    _loaded = True


def ask_gpt(prompt: str, model: str = _DEFAULT_MODEL) -> str:
    _load_secrets()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key or not key.startswith("sk-"):
        raise RuntimeError("OPENAI_API_KEY not found / invalid in ~/.warden/secrets.env")
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai not installed: pip3 install openai --break-system-packages") from e
    try:
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("GPT returned an empty response")
        try:
            from gemini_budget import record_usage
            u = resp.usage
            record_usage(getattr(u, "prompt_tokens", 0) or 0,
                         getattr(u, "completion_tokens", 0) or 0)
        except Exception:
            pass
        return text
    except Exception as e:
        raise RuntimeError(f"GPT call failed: {e}") from e


if __name__ == "__main__":
    print(ask_gpt("Reply with exactly: gpt_client module works"))
