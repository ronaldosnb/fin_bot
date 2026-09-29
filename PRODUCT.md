# FinBot

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

O proprietário administra o painel e usa seu número pessoal no WhatsApp. Contatos autorizados mantêm controles individuais. Participantes de grupos autorizados compartilham uma conta geral do grupo.

## Product Purpose

Registrar receitas e despesas a partir de comandos de texto no WhatsApp, categorizar lançamentos e responder consultas financeiras em texto.

## Operating Context

Uma instância Evolution API conectada ao número pessoal recebe mensagens. A interface web na VPS serve à autorização de conversas e à correção administrativa. O Gemini interpreta lançamentos com linguagem natural.

## Capabilities and Constraints

- Comandos começam com `/fin`; apenas contatos e grupos liberados respondem.
- Cada contato tem uma conta própria. Cada grupo autorizado tem uma conta compartilhada.
- Compras parceladas, recorrências, lançamentos previstos e realizados.
- Parcelas entram pelo vencimento. Recorrências futuras precisam de confirmação.
- Relatórios em texto, em português, com valores em BRL.
- O chat “Mensagem para mim” é preferido para o proprietário; um grupo privado serve de alternativa.
- Somente o proprietário entra no painel.
- A primeira versão aceita texto e não controla faturas de cartão.

## Product Principles

- Não lançar nada a partir de conversa comum.
- Mostrar claramente o que foi realizado e o que ainda é previsão.
- Manter controles individuais e de grupos separados.
- Permitir correção dos lançamentos sem perder o histórico administrativo.
