# FinBot na VPS

O FinBot roda integralmente na VPS `72.60.63.41`: painel FastAPI, worker, PostgreSQL do FinBot, PostgreSQL da Evolution, Redis e Evolution API. O computador local não precisa ficar ligado.

Os containers são administrados diretamente pelo Docker Compose em `/opt/finbot-test`. O Traefik do Dokploy fornece o HTTPS do painel; este Compose não está cadastrado no painel do Dokploy.

## Acessos

- Painel: <https://finbot-painel-72-60-63-41.sslip.io/admin>.
- A senha administrativa é a mesma do teste local, disponível neste computador em `.finbot-admin-password`.
- Evolution Manager: <http://72.60.63.41:18081/manager/>. Use `EVOLUTION_API_KEY` do `.env` local. A sessão WhatsApp existente foi preservada.
- PostgreSQL do FinBot: `72.60.63.41:15432`, usuário e banco `finbot`, senha `POSTGRES_PASSWORD` do `.env` local.

As portas diretas `15432` e `18081` aceitam somente os IPs de saída configurados: `201.28.97.2`, `179.108.105.216` e `200.15.20.67`. Mudanças de IP exigem atualização das regras UFW. O app e o worker usam as conexões internas do Docker.

O webhook da Evolution aponta para `http://app:8000/webhook/evolution` na rede privada. O proxy público encaminha apenas `/admin`, `/static` e `/healthz` ao app.

Na validação de 02/10/2026, o painel autenticou por HTTPS na VPS e o worker enviou um PDF de teste. A rede deste computador apresentou um certificado de inspeção do Fortinet não reconhecido pelo navegador de teste; o certificado do servidor foi validado pela VPS. O modelo `gemini-3.8-flash` retornou HTTP 503 por alta demanda em duas chamadas de teste, enquanto as funções que não usam IA continuaram funcionando.

## Operação na VPS

```bash
ssh root@72.60.63.41
cd /opt/finbot-test
docker compose -p finbot-test-infra-yh1cl6 -f compose.yaml -f proxy.yaml -f app.yaml ps
docker compose -p finbot-test-infra-yh1cl6 -f compose.yaml -f proxy.yaml -f app.yaml logs --tail=80 app worker evolution
docker compose -p finbot-test-infra-yh1cl6 -f compose.yaml -f proxy.yaml -f app.yaml up -d app worker evolution
```

Os serviços reiniciam automaticamente com o Docker. A migração do banco é executada antes do app e do worker.

Arquivos e dados:

- `compose.yaml`: infraestrutura, baseada no arquivo local `compose.dokploy-test.yaml`.
- `proxy.yaml`: rede externa do Traefik e configuração do antigo túnel.
- `app.yaml`: painel, worker, migração e webhook interno, baseado no arquivo local `compose.vps-app.yaml`.
- `.env`: credenciais da infraestrutura; `app.env`: credenciais de runtime do app e worker. Ambos têm permissão `600` e não são versionados.
- `releases/3de2467`: código usado para construir a imagem `finbot-app:3de2467`.
- `backups/antes-app-vps-*.dump`: backup do PostgreSQL feito antes da mudança.
- Volumes persistentes: bancos, Redis e sessão WhatsApp da Evolution.

Para parar app e worker sem apagar dados:

```bash
docker compose -p finbot-test-infra-yh1cl6 -f compose.yaml -f proxy.yaml -f app.yaml stop app worker
```

## Modelo Gemini na VPS

Em 02/10/2026, `GEMINI_MODEL` em `/opt/finbot-test/app.env` foi alterado para
`gemini-3.5-flash-lite`. App e worker foram recriados para carregar a configuração.
Uma chamada real interpretou corretamente uma despesa de R$ 25 com alimentação,
sem gravar lançamento ou enviar mensagem. O painel permaneceu disponível.

Backup anterior: `/opt/finbot-test/backups/app-antes-gemini-lite-20261002T174412Z.env`.
Para reverter apenas o modelo, altere `GEMINI_MODEL` para `gemini-3.8-flash` em `app.env`
e execute `up -d --no-deps app worker` com os três arquivos Compose.

## Acesso ao painel pelo IP

Endereço: `http://72.60.63.41:8000/admin`. Usa a mesma senha do painel HTTPS.
Este acesso usa HTTP: senha e sessão trafegam sem criptografia. Prefira HTTPS quando possível.

O serviço `panel-ip` usa `panel-ip.conf` para encaminhar apenas `/admin`, `/static` e `/healthz`.
O proxy remove a flag `Secure` do cookie somente nessa entrada HTTP; o acesso HTTPS mantém a flag.
A porta TCP 8000 está publicada no IPv4 da VPS e liberada no firewall.

Para retirar o acesso por IP sem parar o bot:

```bash
cd /opt/finbot-test
docker compose -p finbot-test-infra-yh1cl6 -f compose.yaml -f proxy.yaml -f app.yaml stop panel-ip
ufw delete allow to 72.60.63.41 port 8000 proto tcp
```

## Execução local anterior

O app, o worker e o cliente `finbot-local-tunnel` deste computador foram parados após a validação da VPS. O servidor do túnel também ficou parado na VPS. Seus containers e arquivos foram preservados para eventual retorno.

Para voltar temporariamente à execução local, primeiro pare app e worker na VPS com o comando acima. Na VPS, restaure o webhook antigo e inicie o túnel usando os arquivos de infraestrutura:

```bash
cd /opt/finbot-test
docker compose -p finbot-test-infra-yh1cl6 -f compose.yaml -f proxy.yaml up -d evolution tunnel
```

Depois, neste computador:

```bash
docker start finbot-local-tunnel
docker compose -f compose.local-test.yaml up -d migrate app worker
```

Para voltar novamente à execução integral na VPS, use `up -d app worker evolution` com os três arquivos Compose e pare os processos locais.
