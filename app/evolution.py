import base64

import httpx

from app.config import get_settings


def send_text(chat_jid: str, text: str) -> None:
    settings = get_settings()
    if not settings.evolution_api_key:
        raise RuntimeError("Chave da Evolution não configurada")
    url = f"{settings.evolution_api_url.rstrip('/')}/message/sendText/{settings.evolution_instance}"
    with httpx.Client(timeout=20) as client:
        response = client.post(
            url,
            headers={"apikey": settings.evolution_api_key},
            json={"number": chat_jid, "text": text},
        )
        response.raise_for_status()


def send_document(chat_jid: str, content: bytes, file_name: str, caption: str) -> None:
    settings = get_settings()
    if not settings.evolution_api_key:
        raise RuntimeError("Chave da Evolution não configurada")
    if not file_name or not content.startswith(b"%PDF-"):
        raise ValueError("Documento PDF inválido")
    url = f"{settings.evolution_api_url.rstrip('/')}/message/sendMedia/{settings.evolution_instance}"
    with httpx.Client(timeout=60) as client:
        response = client.post(
            url, headers={"apikey": settings.evolution_api_key},
            json={"number": chat_jid, "mediatype": "document", "mimetype": "application/pdf",
                  "fileName": file_name, "caption": caption,
                  "media": base64.b64encode(content).decode("ascii")},
        )
        response.raise_for_status()
