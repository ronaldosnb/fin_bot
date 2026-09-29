from datetime import date

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings


class TransactionDraft(BaseModel):
    kind: str = Field(description="expense ou income")
    amount: str | None = Field(description="Valor total em reais, sem símbolo; null se não informado")
    description: str | None = Field(description="Descrição curta do lançamento")
    category: str | None = Field(description="Uma das categorias permitidas")
    date: str | None = Field(description="Data YYYY-MM-DD ou null")
    installments: int = Field(default=1, description="Número de parcelas, 1 quando não parcelado")
    recurrence_frequency: str | None = Field(default=None, description="daily, weekly, monthly, yearly ou null")
    recurrence_count: int | None = Field(default=None, description="Quantidade de ocorrências, ou null se sem prazo")


class ReportDraft(BaseModel):
    start_date: str = Field(description="Data inicial inclusiva em YYYY-MM-DD")
    end_date: str = Field(description="Data final inclusiva em YYYY-MM-DD")
    kind: str = Field(description="expense, income ou all")
    category: str | None = Field(default=None, description="Uma categoria existente ou null para todas")
    status: str = Field(description="realized, planned ou both")


def parse_transaction(message: str, today: date, categories: dict[str, list[str]]) -> TransactionDraft:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("Chave Gemini não configurada")
    client = genai.Client(api_key=settings.gemini_api_key)
    prompt = (
        "Extraia exatamente um lançamento financeiro da mensagem em português do Brasil. "
        "Use 'expense' para despesa e 'income' para receita. "
        "O valor de compra parcelada é o total, não o valor de cada parcela. "
        "Se não houver data explícita, use null. A data atual é " + today.isoformat() + ". "
        "Se a mensagem disser 'primeira parcela em', essa data é o vencimento da primeira parcela. "
        "Recorrência e parcelamento são diferentes. Não invente valor ou descrição. "
        "A categoria deve ser exatamente uma das permitidas para o tipo; se não encaixar, use Outros. "
        f"Categorias: {categories}. Mensagem: {message}"
    )
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TransactionDraft,
            temperature=0,
        ),
    )
    if not response.text:
        raise ValueError("O Gemini não retornou um lançamento")
    try:
        return TransactionDraft.model_validate_json(response.text)
    except ValidationError as exc:
        raise ValueError("Não consegui interpretar o lançamento. Reformule com valor e descrição.") from exc


def parse_report_query(message: str, today: date, categories: dict[str, list[str]]) -> ReportDraft:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("Chave Gemini não configurada")
    client = genai.Client(api_key=settings.gemini_api_key)
    prompt = (
        "Transforme a pergunta financeira em filtros de relatório. "
        "Datas são inclusivas em YYYY-MM-DD. Hoje é " + today.isoformat() + ". "
        "Se não houver período, use o mês atual. Se não pedir previsões, use status realized. "
        "Use kind expense para gastos, income para receitas e all para saldo ou resumo geral. "
        "A categoria precisa existir exatamente na lista; caso contrário use null. "
        f"Categorias: {categories}. Pergunta: {message}"
    )
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ReportDraft,
            temperature=0,
        ),
    )
    if not response.text:
        raise ValueError("Não consegui interpretar o período do relatório")
    try:
        return ReportDraft.model_validate_json(response.text)
    except ValidationError as exc:
        raise ValueError("Não entendi a consulta. Exemplo: /fin quanto gastei com alimentação este mês") from exc
