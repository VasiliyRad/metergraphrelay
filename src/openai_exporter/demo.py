from __future__ import annotations

from typing import Any

DEMO_PROMPTS = [
    "Say hello in one sentence.",
    "What's 2+2?",
]


def run_demo(client: Any, *, model: str = "gpt-4o-mini") -> list[dict]:
    results = []
    for prompt in DEMO_PROMPTS:
        completion = client.chat.completions.create(
            model=model,
            store=True,
            messages=[{"role": "user", "content": prompt}],
        )
        reply = completion.choices[0].message.content
        print(f"> {prompt}\n{reply}\n")
        results.append({"prompt": prompt, "reply": reply})
    return results
