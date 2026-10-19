# Assistente pessoal no Teams

Você conversa com o assistente pelo **chat de notas pessoais** do Teams (o chat com você mesmo), só com a
Microsoft Graph e a sua conta. Sem Azure Bot, sem túnel, sem porta aberta. Ele:

- **guarda lembretes** e te avisa na hora (`!me lembra amanhã às 10 de revisar o PR`);
- **lê os chats que você escolher** (via Graph, como a sua conta);
- **gera reports** sob demanda (`/report 3 7d`) e um **report diário** automático;
- **cria memórias sozinho enquanto lê** os chats: papel, responsabilidades e prioridade de resposta de cada pessoa;
- ordena o que está **esperando sua resposta** e avisa na hora quando alguém prioritário escreve.

```
Você ⇄ chat de notas (48:notes) ⇄ Graph ⇄ worker (sync + comandos + agendador) ⇄ Postgres
                                                  └→ seus chats (polling)
```

| Serviço | Função |
|---|---|
| `worker` | Processo único: sincroniza os chats (Graph delegado), lê comandos do chat de notas e roda lembretes, report diário e alertas |
| `db` | Postgres 16 (schema criado automaticamente) |

## 1. Entra ID: registro do app

1. Portal Azure → **Microsoft Entra ID → App registrations → New registration**, **Single tenant**.
2. Anote **Application (client) ID** (`GRAPH_CLIENT_ID`) e **Directory (tenant) ID** (`GRAPH_TENANT_ID`).
3. *Authentication* → **Allow public client flows = Yes** (login por device code).
4. *API permissions* → **Microsoft Graph → Delegated**: `User.Read`, `Chat.Read`, `ChatMessage.Send`
   (e `Mail.Send` só se usar `NOTIFY_EMAIL=true`). Não crie client secret.

## 2. Stack no Portainer

**Opção A (recomendada):** suba esta pasta para um repositório Git e crie a stack em
*Stacks → Add stack → Repository* (o Portainer faz o build).

**Opção B:** copie a pasta para o host, rode `docker build -t assistente-teams:latest .`
e cole o `docker-compose.yml` no editor web.

Preencha as variáveis de ambiente (modelo em `.env.example`). Nenhuma porta é exposta.

## 3. Primeiro uso

1. Portainer → container **worker** → *Console* → `python -m app.login`, abra o link e digite o código.
   O token fica no volume `msal`; o worker passa a usá-lo sem reiniciar. Refaça só se o token expirar
   (o aviso aparece nos logs do worker).
2. No Teams, abra o chat de notas (você com você mesmo) e escreva `/ajuda`.
3. `/chats` → `/acompanhar N` → depois de alguns minutos, `/report N`.

## Como falar com o assistente

O assistente só executa mensagens **suas** no chat de notas. As respostas começam com 🤖 e são ignoradas na leitura.

| Você escreve | O que acontece |
|---|---|
| `/comando ...` | Executa o comando (tabelas abaixo) |
| `!texto livre` | Remove o `!` e interpreta em linguagem natural (lembrete, report, nota sobre pessoa) |
| qualquer outra coisa | Ignorada: o chat continua servindo para suas anotações |

## Comandos

| Comando | O que faz |
|---|---|
| `/chats [filtro]` | Lista seus chats com número |
| `/acompanhar N` / `/parar N` | Liga/desliga a leitura de um chat |
| `/monitorados` | Chats acompanhados |
| `/report [N] [7d]` | Report de um chat ou de todos; padrão 1 dia |
| `/lembretes` / `/cancelar ID` | Lembretes pendentes |
| `/sync` | Força sincronização |
| `!texto livre` | Lembretes, reports e notas sobre pessoas em linguagem natural (ex.: `!me lembra amanhã às 10 de revisar o PR`) |

### Memória por pessoa

| Comando | O que faz |
|---|---|
| `/pendentes` | Chats acompanhados com mensagens depois da sua última resposta, ordenados por prioridade de quem escreveu e depois pela mais antiga |
| `/pessoas` / `/pessoa Nome` | Lista e ficha (aceita parte do nome ou apelido) |
| `/nota Nome: texto` / `/apagar-nota ID` | Notas sobre a pessoa |
| `/prioridade Nome 1\|2\|3` | 1 alta, 2 normal (padrão), 3 baixa |
| `/apelido Nome: apelido` | Para achar a pessoa por outro nome |
| `/aprender [auto\|revisar\|off]` | Modo de aprendizado (veja abaixo) |
| `/aprendido [7d]` | O que ele aprendeu no período, com o comando para apagar cada nota |
| `/sugerir [N] [7d]` | Análise manual de chats, sempre com revisão |
| `/sugestoes` / `/aprovar IDs\|todas` / `/rejeitar IDs\|todas` | Revisão das sugestões pendentes |

### Aprendizado automático

O worker, ao sincronizar um chat acompanhado, junta as mensagens novas e (a cada `LEARN_MIN_MESSAGES`
mensagens ou 24 h) pede ao LLM fatos duráveis sobre quem escreveu. Notas aparecem com 🤖 em `/pessoa`.

- **Modos:** `auto` grava direto; `revisar` só sugere; `off` desliga. Troca em tempo real com `/aprender`.
- **Resumo diário:** junto do report diário vem *"O que aprendi"*, com `/apagar-nota ID` para cada item.
- **Prioridade:** ao aprender, só classifica quem ainda está em *normal*. Se você definir a prioridade
  à mão, ela fica travada e o aprendizado nunca mais mexe. Mudanças depois da primeira classificação
  viram sugestão para você aprovar.
- **Salvaguardas:** só entram pessoas que realmente escreveram no chat (nomes inventados são descartados),
  fatos repetidos são ignorados, há teto de notas automáticas por pessoa (`AUTO_FACT_CAP`) e o prompt
  proíbe dados pessoais ou sensíveis e qualquer coisa sobre você.
- **Custo:** um lote por chat a cada ~10 mensagens novas, no máximo `LEARN_MAX_CALLS` chamadas por ciclo.
  Na primeira vez ele aprende com os últimos `BACKFILL_DAYS` dias já sincronizados.

Como a memória é usada:

- **Reports** recebem o contexto das pessoas do chat e ganham a seção *Responder primeiro*.
- **Alerta imediato** quando alguém com prioridade ≤ `ALERT_PRIORITY` (padrão 1) escreve num chat acompanhado.
- Pessoas criadas só pelo nome são vinculadas ao usuário do Teams assim que esse nome aparece nas mensagens.
- O prompt de sugestões evita dados pessoais ou sensíveis e guarda só contexto de trabalho.

## Notas

- **Canais de times** ficaram de fora de propósito (exigem `ChannelMessage.Read.All`, com consentimento de admin).
  Dá para adicionar na sincronização depois com `/teams/{id}/channels/{id}/messages/delta`.
- Respostas que usam o LLM (reports) chegam como uma segunda mensagem.
- Testes: `pip install -r requirements.txt pytest && pytest -q`.

## Limitações

- **Latência:** os comandos são lidos por polling a cada `COMMAND_POLL_SECONDS` (padrão 20 s); a resposta vem depois disso.
- **Notificação:** mensagens postadas na sua própria conta podem não gerar notificação no celular. Para lembretes e
  alertas de prioridade, use `NOTIFY_EMAIL=true` (também envia por e-mail a você mesmo, via `Mail.Send`).
- **Chat de notas:** se o chat `48:notes` não existir (404), o log avisa e `selfchat_status` fica registrado; envie
  qualquer mensagem no chat de notas do Teams para criá-lo.
- Só chats que você acompanhar são lidos; mensagens antigas do chat de notas anteriores à primeira execução não são executadas.
