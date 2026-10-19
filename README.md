# Assistente pessoal no Teams

Você conversa com o bot no chat 1:1 do Teams. Ele:

- **guarda lembretes** e te avisa na hora (`me lembra amanhã às 10 de revisar o PR`);
- **lê os chats que você escolher** (via Graph, como a sua conta; ninguém vê bot entrando);
- **gera reports** sob demanda (`/report 3 7d`) e um **report diário** automático;
- **cria memórias sozinho enquanto lê** os chats: papel, responsabilidades e prioridade de resposta de cada pessoa;
- ordena o que está **esperando sua resposta** e avisa na hora quando alguém prioritário escreve.

```
Você ⇄ Teams 1:1 ⇄ Azure Bot ⇄ Cloudflare Tunnel ⇄ bot (+ agendador)
                                                     │
                               Postgres ← worker (Graph, polling) ← seus chats
```

| Serviço | Função |
|---|---|
| `bot` | Recebe comandos em `/api/messages`, agenda lembretes e reports, envia mensagens proativas |
| `worker` | Sincroniza a lista de chats e as mensagens dos chats acompanhados (Graph delegado) |
| `db` | Postgres 16 (schema criado automaticamente) |
| `cloudflared` | Expõe o bot sem abrir porta |

## 1. Azure: registro do bot

1. Portal Azure → **Azure Bot** → tipo **Single Tenant**, plano **F0**.
2. No App Registration criado: anote **Application (client) ID** e **Directory (tenant) ID**; em *Certificates & secrets*, crie um **client secret**.
3. Ainda no App Registration, ajuste duas coisas para o worker:
   - *API permissions* → **Microsoft Graph → Delegated**: `User.Read`, `Chat.Read`, `offline_access`.
   - *Authentication* → **Allow public client flows = Yes** (necessário para o login por device code).
4. No Azure Bot: *Configuration* → Messaging endpoint `https://bot.seudominio.com.br/api/messages`; *Channels* → **Microsoft Teams**.

## 2. Cloudflare Tunnel

Zero Trust → Networks → Tunnels → crie um túnel, copie o **token**.
Public hostname: `bot.seudominio.com.br` → `http://bot:3978`.

## 3. Stack no Portainer

**Opção A (recomendada):** suba esta pasta para um repositório Git e crie a stack em
*Stacks → Add stack → Repository* (o Portainer faz o build).

**Opção B:** copie a pasta para o host, rode `docker build -t assistente-teams:latest .`
e cole o `docker-compose.yml` no editor web.

Preencha as variáveis de ambiente (modelo em `.env.example`). Deixe `OWNER_AAD_ID` vazio por enquanto.

## 4. Instalar no Teams

```sh
./manifest/build.sh <MICROSOFT_APP_ID> bot.seudominio.com.br
```

No Teams: **Apps → Gerenciar seus apps → Carregar um app** → `manifest/assistente-teams.zip`.
(Se a opção não aparecer, o admin do Teams precisa liberar o upload de apps personalizados.)

## 5. Primeiro uso

1. Mande "oi" para o bot. Ele responde com o seu **OWNER_AAD_ID**.
   Coloque o valor na stack e faça *Update the stack*.
2. Login da sua conta para o worker: Portainer → container **worker** → *Console* →
   `python -m app.login`, abra o link e digite o código. Só precisa refazer se o token expirar
   (o bot te avisa no chat).
3. No Teams: `/chats` → `/acompanhar N` → depois de alguns minutos, `/report N`.

## Comandos

| Comando | O que faz |
|---|---|
| `/chats [filtro]` | Lista seus chats com número |
| `/acompanhar N` / `/parar N` | Liga/desliga a leitura de um chat |
| `/monitorados` | Chats acompanhados |
| `/report [N] [7d]` | Report de um chat ou de todos; padrão 1 dia |
| `/lembretes` / `/cancelar ID` | Lembretes pendentes |
| `/sync` | Força sincronização |
| texto livre | Lembretes, reports e notas sobre pessoas em linguagem natural |

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
  Dá para adicionar no worker depois com `/teams/{id}/channels/{id}/messages/delta`.
- Respostas que usam o LLM (reports) chegam como uma segunda mensagem, para não estourar o timeout de ~15 s do Teams.
- Só o `OWNER_AAD_ID` é atendido; qualquer outra pessoa recebe uma recusa.
- Testes: `pip install -r requirements.txt pytest && pytest -q`.
# Discovery
