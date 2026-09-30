# Teste atual: Dokploy + computador local

Após a restauração do backup em 30/09/2026, a infraestrutura foi recriada diretamente pelo Docker Compose na VPS, em `/opt/finbot-test`. Ela executa PostgreSQL para o FinBot, PostgreSQL para a Evolution, Redis, Evolution API e o servidor do túnel. Neste computador rodam o túnel, o app FastAPI e o worker. O túnel HTTPS liga os serviços às portas locais; as duas portas diretas de teste são liberadas somente para os IPs indicados abaixo.

A chave anterior da API do Dokploy deixou de funcionar após a restauração. Este Compose é administrado por SSH; o Traefik do Dokploy fornece o HTTPS do túnel. O backup restaurado não continha os volumes do FinBot, portanto os bancos foram recriados e o WhatsApp precisa de novo pareamento.

Para o teste, também há acesso direto à Evolution em `72.60.63.41:18081` e ao PostgreSQL do FinBot em `72.60.63.41:15432`. As portas padrão `8080` e `5432` já pertencem a outros serviços nessa VPS. O firewall aceita as novas portas somente dos IPs de saída observados neste computador: `201.28.97.2`, `179.108.105.216` e `200.15.20.67`. Se o IP de saída mudar, o acesso direto precisará de uma nova regra. O app e o worker continuam configurados para usar o túnel.

## Acessar e conectar o WhatsApp

1. Abra o painel do bot em <http://127.0.0.1:8000/admin>. A senha gerada para o administrador está no arquivo local `.finbot-admin-password` (use `cat .finbot-admin-password` neste diretório).
2. Abra o Manager da Evolution em <http://127.0.0.1:18080/manager>. A chave solicitada está em `EVOLUTION_API_KEY` no arquivo local `.env` (use `sed -n 's/^EVOLUTION_API_KEY=//p' .env`).
3. A instância `finbot` já está criada. Abra-a no Manager e conecte o WhatsApp escaneando o QR Code em **Aparelhos conectados → Conectar um aparelho**. Se o QR Code tiver expirado, peça um novo no Manager.
4. Envie `/fin ajuda` para **Mensagem para mim** no WhatsApp. Abra **Conversas** no painel e autorize a conversa se ela aparecer como pendente. Depois teste `/fin gastei 1 real em teste`.

O número do proprietário em `.env` é `5517981683809@s.whatsapp.net`. Se o teste em **Mensagem para mim** não chegar ao bot, experimente um grupo privado, autorize a conversa no painel e marque **Grupo privado do dono**. O comportamento de mensagens enviadas para si próprio pode variar na conexão Baileys.

## Verificar os serviços

```bash
docker ps --filter name=finbot-local-tunnel
docker compose -f compose.local-test.yaml ps
curl http://127.0.0.1:8000/healthz
docker compose -f compose.local-test.yaml logs --tail=80 app worker
docker logs --tail=50 finbot-local-tunnel
```

## Reiniciar após desligar o computador

O Docker reinicia automaticamente o túnel, o app e o worker. Se necessário, inicie-os manualmente:

```bash
docker start finbot-local-tunnel
docker compose -f compose.local-test.yaml up -d migrate app worker
```

Para parar apenas os processos locais de teste:

```bash
docker compose -f compose.local-test.yaml stop app worker
docker stop finbot-local-tunnel
```

Os serviços na VPS continuam ligados quando o computador está desligado, mas o bot não recebe nem responde comandos enquanto o túnel, o app e o worker locais estiverem parados. A VPS contém um `.env` restrito às credenciais da infraestrutura; a senha do administrador e a chave do Gemini ficam neste computador. Não compartilhe esses arquivos.

## Recursos na VPS

- Diretório: `/opt/finbot-test`, com `compose.yaml`, `proxy.yaml` e `.env` privado.
- Compose: `finbot-test-infra-yh1cl6`, criado a partir de `compose.dokploy-test.yaml`.
- Volumes persistentes: dados dos dois PostgreSQL, Redis e sessão da Evolution.
- O webhook da Evolution aponta para o app local através do túnel reverso.
- Encaminhadores diretos na VPS: `finbot-test-postgres-port.service` e `finbot-test-evolution-port.service`. Eles resolvem o IP atual dos containers a cada conexão e são iniciados automaticamente pelo systemd.

Para verificar ou reiniciar a infraestrutura na VPS:

```bash
ssh root@72.60.63.41
cd /opt/finbot-test
docker compose -p finbot-test-infra-yh1cl6 -f compose.yaml -f proxy.yaml ps
docker compose -p finbot-test-infra-yh1cl6 -f compose.yaml -f proxy.yaml up -d
```

Para interromper o teste na VPS, use `stop` no mesmo comando Compose. Os volumes persistentes são mantidos. Excluir os volumes apaga os bancos e a sessão do WhatsApp.

Para fechar apenas as portas diretas, desative os dois serviços systemd e remova as regras `15432` e `18081` no UFW. O túnel HTTPS e os processos locais continuarão funcionando.
