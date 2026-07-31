from types import SimpleNamespace
from unittest.mock import MagicMock

from metergraphrelay.demo import DEMO_PROMPTS, run_demo


def make_completion(reply_text):
    message = SimpleNamespace(content=reply_text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_run_demo_sends_each_prompt_with_store_true():
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        make_completion("Hello there."),
        make_completion("4"),
    ]

    results = run_demo(client, model="gpt-4o-mini")

    assert client.chat.completions.create.call_count == len(DEMO_PROMPTS)
    for call, prompt in zip(client.chat.completions.create.call_args_list, DEMO_PROMPTS):
        kwargs = call.kwargs
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["store"] is True
        assert kwargs["messages"] == [{"role": "user", "content": prompt}]
    assert results == [
        {"prompt": DEMO_PROMPTS[0], "reply": "Hello there."},
        {"prompt": DEMO_PROMPTS[1], "reply": "4"},
    ]


def test_run_demo_prints_prompt_and_reply(capsys):
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        make_completion("Hello there."),
        make_completion("4"),
    ]

    run_demo(client, model="gpt-4o-mini")

    captured = capsys.readouterr()
    assert DEMO_PROMPTS[0] in captured.out
    assert "Hello there." in captured.out
