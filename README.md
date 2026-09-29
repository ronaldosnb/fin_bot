# FinBot

Bot financeiro em português para um número pessoal de WhatsApp conectado à Evolution API. Comandos começam com `/fin`. Conversas privadas autorizadas têm contas individuais; grupos autorizados têm uma conta geral por grupo. O painel web é exclusivo do administrador.

## Componentes

- FastAPI: painel e webhook interno.
- Worker: interpreta comandos, cria ocorrências recorrentes e envia respostas.
- Dois serviços PostgreSQL isolados: `finbot` para dados financeiros e `evolution` para a Evolution.
- Redis: cache da Evolution.
- Evolution API v2.3.7: conexão WhatsApp via Baileys.
- Caddy: HTTPS do painel. O webhook só é acessado pela rede interna do Docker.
- Gemini API: interpretação e categorização de lançamentos de texto.

## Preparação da VPS

Requisitos: Ubuntu/Linux com Docker e Docker Compose, um domínio apontado para a VPS, portas 80 e 443 liberadas, e uma chave da Gemini API criada no Google AI Studio. A porta 8080 da Evolution fica restrita a `127.0.0.1` da VPS.

1. Copie `.env.example` para `.env` e preencha `DOMAIN`, `ACME_EMAIL`, `OWNER_JID`, `GEMINI_API_KEY` e `EVOLUTION_INSTANCE`. O JID do proprietário usa o formato `55DDDNUMERO@s.whatsapp.net`.
2. Gere `POSTGRES_PASSWORD`, `EVOLUTION_DB_PASSWORD`, `APP_SECRET` e `EVOLUTION_API_KEY` com `openssl rand -hex 24`. Use apenas caracteres hexadecimais nas senhas do PostgreSQL para manter as URLs de conexão válidas.
3. Gere o hash da senha do painel:

   ```bash
   docker build -t finbot-setup .
   docker run --rm -it finbot-setup python -m app.password
   ```

   Copie a saída para `ADMIN_PASSWORD_HASH_B64` em `.env`. A senha digitada não fica no arquivo de configuração.
4. Restrinja a leitura de `.env` com `chmod 600 .env` e suba os serviços:

   ```bash
   docker compose up -d --build
   docker compose ps
   docker compose logs --tail=100 app worker evolution caddy
   ```

5. Acesse `https://SEU_DOMINIO/admin` e entre com a senha configurada.

O `migrate` aplica o esquema inicial antes de iniciar aplicação e worker. Cada serviço PostgreSQL cria seu próprio banco na primeira inicialização do volume.

## Conectar o WhatsApp

Crie um túnel SSH para acessar a Evolution sem publicar sua porta na internet:

```bash
ssh -L 8080:127.0.0.1:8080 USUARIO@IP_DA_VPS
```

Abra `http://localhost:8080/manager`, informe `EVOLUTION_API_KEY`, crie uma instância **Baileys** com o nome exato de `EVOLUTION_INSTANCE` e conecte o QR code com seu WhatsApp. A configuração do container envia eventos `MESSAGES_UPSERT` para `http://app:8000/webhook/evolution` na rede interna. Confirme no Manager que a instância está conectada e que o webhook está ativo.

Envie `/fin ajuda` em uma conversa privada ou grupo. A conversa aparecerá como pendente no painel; autorize-a em **Conversas**. A mensagem enviada ao chat “Mensagem para mim” é aceita para o `OWNER_JID`. Se ela aparecer com um JID alternativo, configure-o em `OWNER_SELF_JID` e reinicie app e worker. Como a entrega de mensagens próprias pode variar conforme a sessão Baileys, faça esse ensaio antes de depender do bot. Se falhar, crie um grupo privado, envie `/fin ajuda`, autorize-o e marque **Grupo privado do dono**. Nesse modo, apenas comandos do proprietário são processados e os lançamentos entram na conta pessoal dele.

## Comandos

```text
/fin ajuda
/fin gastei 35 no almoço
/fin recebi 2500 de salário
/fin comprei notebook por 1200 em 6x, primeira parcela em 10/10/2026
/fin pago aluguel de 1000 todo mês
/fin relatorio
/fin relatorio 09/2026
/fin quanto gastei com alimentação no mês passado
/fin listar
/fin confirmar 12
/fin editar 12 valor 42,50
/fin editar 12 categoria Alimentação
/fin editar 12 descricao Almoço de trabalho
/fin editar 12 data 25/09/2026
/fin excluir 12
/fin cancelar recorrencia 12
```

O bot interpreta um lançamento por comando. Se a data não vier na mensagem, usa a data de envio em `America/Sao_Paulo`. Para parcelas, o valor informado é o **total** e a data indicada é o vencimento da primeira parcela; sem data, a primeira parcela vence na data da mensagem. Ocorrências recorrentes futuras ficam previstas. A primeira ocorrência com data presente ou passada é marcada como realizada. Os relatórios mensais separam realizados e previstos.

Cada pessoa consulta e corrige somente sua conta privada. Em grupos autorizados, qualquer membro pode consultar e corrigir a conta geral do grupo. O painel mostra todas as contas, inclusive lançamentos e recorrências, e permite revisar mensagens que falharam.

Mensagens sem `/fin` são descartadas pelo webhook. Depois de processado, o texto do comando é removido da fila de entrada e o texto da resposta é removido após o envio. Lançamentos e auditoria permanecem no banco financeiro; mensagens com falha ficam na fila até revisão.

## Operação e backup

```bash
docker compose logs -f app worker evolution
docker compose ps
bash scripts/backup.sh
```

O backup contém os dois bancos e os arquivos de sessão da Evolution. Copie o diretório gerado para armazenamento fora da VPS. Faça um backup antes de atualizar imagens ou migrar o banco.

Para restaurar, use uma instalação com os mesmos segredos e uma versão de código compatível com o backup. Confirme os caminhos e pare os serviços que escrevem dados. A restauração abaixo **substitui dados atuais**; experimente em uma cópia antes de usar em produção:

```bash
docker compose stop app worker evolution
cat backups/SEU_BACKUP/finbot.dump | docker compose exec -T postgres pg_restore -U finbot -d finbot --clean --if-exists --no-owner
cat backups/SEU_BACKUP/evolution.dump | docker compose exec -T evolution_db pg_restore -U evolution -d evolution --clean --if-exists --no-owner
docker compose cp backups/SEU_BACKUP/evolution-instances evolution:/evolution/instances
docker compose up -d
```

Depois confira `docker compose ps`, os logs e a conexão da instância no Manager.

O estado `failed` em **Falhas** permite nova tentativa pelo painel. As tabelas `inbox` e `outbox` registram o andamento do processamento. Atualizações devem preservar os volumes PostgreSQL, Redis, Evolution e Caddy. O rollback de código é voltar à imagem anterior e restaurar o backup caso a versão nova tenha alterado o esquema.

## Desenvolvimento local

Use `SESSION_SECURE=false` em `.env` somente para acessar o painel por HTTP local. Para executar fora do Compose, configure `DATABASE_URL` para um PostgreSQL acessível, instale `requirements.txt`, rode `alembic upgrade head` e inicie `uvicorn app.main:app --reload` e `python -m app.worker` em processos separados.
