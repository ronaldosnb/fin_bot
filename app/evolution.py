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
