from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResponse:
    chat_jid: str
    text: str
    pdf_bytes: bytes | None = None
    file_name: str | None = None
