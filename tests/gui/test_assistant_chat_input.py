from argusai.gui.assistant_common import assistant_chat_has_input


def test_assistant_chat_has_input_text() -> None:
    assert assistant_chat_has_input({"message": "hello"})


def test_assistant_chat_has_input_voice_only() -> None:
    assert assistant_chat_has_input({
        "message": "",
        "voice": {"mime": "audio/webm", "data_base64": "abc", "duration_ms": 3000},
    })


def test_assistant_chat_has_input_audio_attachment_only() -> None:
    assert assistant_chat_has_input({
        "message": "",
        "attachments": [{"type": "audio", "name": "voice.webm", "data_base64": "abc"}],
    })


def test_assistant_chat_has_input_rejects_empty() -> None:
    assert not assistant_chat_has_input({"message": "  "})
    assert not assistant_chat_has_input({})
