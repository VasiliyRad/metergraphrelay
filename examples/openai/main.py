"""Sample OpenAI call demonstrating store=True.

Generates a real stored completion you can pull:
    python examples/openai/main.py
    metergraphrelay pull openai
"""

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "How can I reduce AI costs?"}],
    store=True,
    metadata={"source": "metergraphrelay-example"},
)

print(response.choices[0].message.content)
