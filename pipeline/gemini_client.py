"""
Minimal Gemini client for WARDEN cross-model review.
Loads the API key from ~/.warden/secrets.env (never from the repo) and exposes
a single function, ask_gemini(prompt), used by the implement and review stages.
"""
import os
from pathlib import Path

_DEFAULT_MODEL = "gemini-3.1-pro-preview"
_loaded = False


def _load_secrets():
    """Load ~/.warden/secrets.env into the environment once."""
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


def ask_gemini(prompt: str, model: str = _DEFAULT_MODEL, timeout: int = 60) -> str:
    """
    Send a prompt to Gemini and return the text response.
    Raises RuntimeError with a clear message if the key is missing or the call fails.
    """
    _load_secrets()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or key == "PASTE_YOUR_KEY_HERE":
        raise RuntimeError("GEMINI_API_KEY not found in ~/.warden/secrets.env")

    try:
        from google import genai
    except ImportError as e:
        raise RuntimeError("google-genai not installed: pip3 install google-genai --break-system-packages") from e

    try:
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(model=model, contents=prompt)
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError("Gemini returned an empty response")
        # Track spend (best-effort; never let tracking break a call).
        try:
            from gemini_budget import record_usage
            um = resp.usage_metadata
            in_tok = getattr(um, "prompt_token_count", 0) or 0
            out_tok = ((getattr(um, "candidates_token_count", 0) or 0)
                       + (getattr(um, "thoughts_token_count", 0) or 0))
            record_usage(in_tok, out_tok)
        except Exception:
            pass
        return text
    except Exception as e:
        raise RuntimeError(f"Gemini call failed: {e}") from e


if __name__ == "__main__":
    print(ask_gemini("Reply with exactly: gemini_client module works"))
