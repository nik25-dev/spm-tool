"""Optional plain-language polish with a FREE-tier LLM (Gemini via Google AI Studio).
UNTESTED against the live API. Always falls back to the template sentence, so the project works with no LLM at all.
Only send simulated/demo data: free tiers may use inputs to improve the provider's products."""
import os
import requests


def polish(template_text: str) -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return template_text
    model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")   # copy the exact model id from AI Studio if this errors
    prompt = ("Rewrite this project status in one or two friendly sentences for a non-technical manager. "
              "Do not add, remove or change any numbers or facts.\n\n" + template_text)
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        r.raise_for_status()
        out = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        # guard: the numbers in the template must survive unchanged
        import re
        if set(re.findall(r"\d+(?:\.\d+)?", template_text)) <= set(re.findall(r"\d+(?:\.\d+)?", out)):
            return out
    except Exception as e:           # rate limit, bad model id, network...
        print("LLM skipped:", e)
    return template_text
