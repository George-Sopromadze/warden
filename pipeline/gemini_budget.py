"""
Tracks estimated Gemini spend and alerts via Telegram when the remaining
prepaid balance drops below a threshold. Estimate-only (counts WARDEN's own
calls); glance at AI Studio occasionally to stay calibrated.
"""
import json
import os
import subprocess
from pathlib import Path

_USD_IN_PER_M = 2.0
_USD_OUT_PER_M = 12.0
_USD_TO_GBP = 0.79

_STATE = Path.home() / ".warden" / "gemini_spend.json"
_ROOT = Path(__file__).resolve().parent.parent


def _cfg(name, default):
    return os.environ.get(name, default)


def _load():
    if _STATE.exists():
        try:
            return json.loads(_STATE.read_text())
        except Exception:
            pass
    start = float(_cfg("GEMINI_START_BALANCE_GBP", "40"))
    return {"starting_balance_gbp": start, "spent_gbp": 0.0, "alerted": False}


def _save(state):
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(_STATE)


def _notify(message: str):
    try:
        subprocess.run([str(_ROOT / "hooks" / "notify.sh"), message],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def cost_gbp(in_tokens: int, out_tokens: int) -> float:
    usd = (in_tokens / 1_000_000) * _USD_IN_PER_M + (out_tokens / 1_000_000) * _USD_OUT_PER_M
    return usd * _USD_TO_GBP


def record_usage(in_tokens: int, out_tokens: int):
    state = _load()
    state["spent_gbp"] = round(state["spent_gbp"] + cost_gbp(in_tokens, out_tokens), 6)
    remaining = state["starting_balance_gbp"] - state["spent_gbp"]
    threshold = float(_cfg("GEMINI_ALERT_THRESHOLD_GBP", "5"))
    if remaining <= threshold and not state.get("alerted"):
        _notify(
            f"WARDEN: estimated Gemini credit low — about £{remaining:.2f} left "
            f"(spent ~£{state['spent_gbp']:.2f} of £{state['starting_balance_gbp']:.0f}). "
            f"Top up at AI Studio to keep two-model review running."
        )
        state["alerted"] = True
    _save(state)
    return remaining


def reset_balance(new_balance_gbp: float):
    _save({"starting_balance_gbp": float(new_balance_gbp),
           "spent_gbp": 0.0, "alerted": False})
    print(f"balance reset to £{new_balance_gbp:.2f}, alert re-armed")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "reset":
        reset_balance(float(sys.argv[2]))
    else:
        s = _load()
        rem = s["starting_balance_gbp"] - s["spent_gbp"]
        print(f"spent ~£{s['spent_gbp']:.4f} | remaining ~£{rem:.2f} | alerted={s['alerted']}")
