# 🚀 TeleEmpuxo — Sistema de Telemetria de Empuxo

**Versão:** ESP32 v2.5 · Servidor v4.0  
**Objetivo:** Medir, registrar e visualizar em tempo real a força de empuxo de motores de foguete usando uma célula de carga HX711 e um ESP32, com transmissão via WebSocket e interface web.

---

## Índice

1. [Visão geral do sistema](#1-visão-geral-do-sistema)
2. [Requisitos](#2-requisitos)
3. [Configuração do hardware](#3-configuração-do-hardware)
4. [Configuração do ESP32](#4-configuração-do-esp32)
5. [Instalação do servidor](#5-instalação-do-servidor)
6. [Primeiro uso — passo a passo](#6-primeiro-uso--passo-a-passo)
7. [As três interfaces web](#7-as-três-interfaces-web)
8. [Como conduzir um teste](#8-como-conduzir-um-teste)
9. [Lendo e interpretando os dados](#9-lendo-e-interpretando-os-dados)
10. [Máquina de missão (countdown)](#10-máquina-de-missão-countdown)
11. [LEDs do ESP32](#11-leds-do-esp32)
12. [Arquivos gerados](#12-arquivos-gerados)
13. [Solução de problemas](#13-solução-de-problemas)
14. [Parâmetros técnicos](#14-parâmetros-técnicos)

---

## 1. Visão geral do sistema

```
[Célula de Carga]
      │
   [HX711]
      │
   [ESP32] ──WiFi──► [server.py] ──► [dashboard.html]   ← operação
                          │      ──► [overlay.html]      ← transmissão ao vivo
                          │      ──► [layer_control.html] ← controle de missão
                          │
                     [logs/*.csv]   ← dados brutos
                     [plots/*.png]  ← gráficos automáticos
```

O ESP32 lê a célula de carga a **50 Hz**, converte os valores para Newtons e envia via WebSocket ao servidor Python. O servidor distribui os dados para todas as interfaces abertas em tempo real, detecta automaticamente o início e fim da queima, integra o impulso e salva tudo em CSV.

---

## 2. Requisitos

### Hardware
| Componente | Observação |
|---|---|
| ESP32 (qualquer variante com WiFi) | Testado com ESP32-WROOM-32 |
| Módulo HX711 | Qualquer breakout padrão |
| Célula de carga compatível | Capacidade recomendada: 5–50 kg |
| LED amarelo + resistor 220Ω | Pino 18 |
| LED vermelho + resistor 220Ω | Pino 19 |
| LED verde (opcional) | Ligado direto no VCC — indica alimentação |

### Software — ESP32
- Arduino IDE 2.x ou PlatformIO
- Bibliotecas (instalar via Library Manager):
  - `HX711 by Bogdan Necula`
  - `WebSockets by Markus Sattler`
  - `WiFiManager by tzapu`
  - `ArduinoJson by Benoit Blanchon`

### Software — Servidor
- Python 3.10+
- Bibliotecas:
  ```bash
  pip install websockets matplotlib numpy aiohttp
  ```

---

## 3. Configuração do hardware

### Pinagem ESP32

| Pino ESP32 | Função |
|---|---|
| GPIO 15 | HX711 DOUT (dados) |
| GPIO 5 | HX711 SCK (clock) |
| GPIO 18 | LED amarelo (status) |
| GPIO 19 | LED vermelho (queima/perigo) |
| GPIO 0 | Botão BOOT (forçar portal WiFi) |
| VCC / GND | LED verde (alimentação sempre acesa) |

### Diagrama simplificado
```
ESP32 GPIO15 ──► HX711 DOUT
ESP32 GPIO5  ──► HX711 SCK
HX711 VDD    ──► 3.3V
HX711 GND    ──► GND
HX711 E+/E-  ──► Célula de carga (excitação)
HX711 A+/A-  ──► Célula de carga (sinal)
```

---

## 4. Configuração do ESP32

### Antes de compilar

Abra `esp32_telemetria.ino` e ajuste estas duas linhas:

```cpp
// ⚠️ OBRIGATÓRIO: mude para false antes de usar em campo
#define FORCE_PORTAL  false

// Ajuste conforme sua calibração (veja seção de calibração abaixo)
#define CAL_FACTOR   -2280.0f
```

> **Por que FORCE_PORTAL?** Quando `true`, o ESP32 apaga as credenciais WiFi salvas a cada reinicialização e abre o portal de configuração. Deixe `true` apenas durante a configuração inicial. Em campo, deixe `false`.

### Calibração do CAL_FACTOR

1. Compile e grave com `FORCE_PORTAL true`
2. Conecte ao WiFi e deixe o ESP32 rodando
3. Coloque um peso conhecido (ex: 500 g) na célula de carga
4. Abra o Serial Monitor (115200 baud) e observe o valor bruto
5. Ajuste `CAL_FACTOR` até que o valor lido corresponda ao peso em kgf
6. O sinal negativo (`-2280.0f`) indica que a célula está invertida — normal

### Configuração WiFi (primeiro uso)

1. Grave o firmware com `FORCE_PORTAL true`
2. Ligue o ESP32 — ele vai criar uma rede chamada **`Telemetria-Config`** (senha: `12345678`)
3. Conecte seu celular ou notebook nessa rede
4. Acesse **http://192.168.4.1** no navegador
5. Clique em **"Configurar WiFi"**
6. Escolha sua rede, insira a senha
7. Preencha o campo **"IP do Servidor Python"** com o IP da máquina onde o servidor roda (ex: `192.168.1.100`)
8. Clique **Salvar** — o ESP32 reinicia e conecta automaticamente

> **Como saber o IP do servidor?** No Windows: `ipconfig` no terminal. No Linux/Mac: `ip a` ou `ifconfig`. Use o IP da interface WiFi/Ethernet conectada à mesma rede.

### Trocando de rede depois

Segure o botão **BOOT** ao ligar o ESP32. O portal abrirá novamente.

---

## 5. Instalação do servidor

```bash
# Instalar dependências
pip install websockets matplotlib numpy aiohttp

# Colocar todos os arquivos na mesma pasta:
# server.py
# dashboard.html
# overlay.html
# layer_control.html

# Rodar o servidor
python server.py
```

O terminal mostrará:

```
==========================================================
  TELEMETRIA DE EMPUXO — Servidor Python  v4.0
==========================================================
  WebSocket ESP32   : ws://0.0.0.0:8765
  WebSocket Clientes: ws://0.0.0.0:8766
  Dashboard HTTP    : http://localhost:8080/dashboard.html
  Overlay HTTP      : http://localhost:8080/overlay.html
  Layer Control     : http://localhost:8080/layer_control.html
==========================================================
```

Acesse **http://localhost:8080/dashboard.html** no navegador.

---

## 6. Primeiro uso — passo a passo

```
1. Iniciar o servidor Python
      └─► python server.py

2. Ligar o ESP32
      └─► Aguardar LED amarelo piscar lento (conectado)

3. Abrir o dashboard
      └─► http://<IP_DO_SERVIDOR>:8080/dashboard.html

4. Verificar os dots de status no topo
      └─► WS: verde (servidor OK)
      └─► ESP32: verde (hardware conectado)

5. Fazer a tara
      └─► Clique em TARE com a célula de carga vazia

6. Prender o motor na célula de carga

7. Iniciar sessão e fazer o teste
      └─► Veja seção 8
```

---

## 7. As três interfaces web

### Dashboard (`dashboard.html`) — Operação principal

A tela principal de monitoramento. Use esta durante o teste.

```
┌─────────────────────────────────────────┬──────────┐
│  [Logo]  [● WS]  [● ESP32]  [ID SESSÃO] │          │
├─────────────────────────────────────────┤          │
│                                         │  LOG DE  │
│         GRÁFICO DE EMPUXO               │  EVENTOS │
│         (tempo real, 50 Hz)             │          │
│                                         │  ─────── │
├──────────────────────────────────────┬──┤          │
│ FORÇA  │ PICO │ IMPULSO │ DURAÇÃO │ ST│  ARQUIVOS │
├─────────────────────────────────────┴──┴──────────┤
│  [▶ START] [■ STOP] [📊 PLOT] [⚖ TARE] [↺ RESET] │
└────────────────────────────────────────────────────┘
```

**Métricas exibidas:**

| Métrica | O que significa |
|---|---|
| **FORÇA** | Empuxo instantâneo em Newtons (atualizado a 50 Hz) |
| **PICO** | Maior força registrada na sessão atual |
| **IMPULSO** | Área sob a curva de empuxo (N·s) — energia total do motor |
| **DURAÇÃO** | Tempo de queima em segundos |
| **STATE** | Estado atual: IDLE / RUNNING / QUEIMA / COMPLETO |

**Botões:**

| Botão | Quando usar |
|---|---|
| **START** | Inicia uma nova sessão de medição |
| **STOP** | Encerra a sessão manualmente |
| **PLOT** | Gera o gráfico PNG da sessão |
| **TARE** | Zera a balança (faça com célula vazia, antes de prender o motor) |
| **RESET** | Limpa os dados e volta ao estado IDLE |

**Dots de status (canto superior):**

- 🟢 Verde = OK
- 🟡 Amarelo piscando = aguardando / aviso
- 🔴 Vermelho = erro / desconectado

**Painel de configuração (ícone ⚙):**

Permite ajustar a escala do gráfico e o limiar visual de queima. **Atenção:** esses valores afetam apenas a exibição — o limiar de detecção real (2.0 N) fica no servidor.

---

### Layer Control (`layer_control.html`) — Controle de missão

Interface para operações mais formais, com countdown e checklist GO/NO-GO.

**Seções principais:**

**1. GO/NO-GO Checklist**
Antes de iniciar a contagem, todos os itens precisam estar marcados como GO:

| Item | Tipo | O que verificar |
|---|---|---|
| ESP32 | Automático | Hardware conectado ao servidor |
| Câmera | Manual | Câmera de registro posicionada e gravando |
| HX711 / Balança | Manual | Célula de carga montada e tarada |
| Área Livre | Manual | Ninguém no raio de segurança |
| Condição Climática | Manual | Vento e umidade aceitáveis |

**2. Countdown T-minus**

- Configure o tempo (ex: 60 s) no campo T-
- Clique **START MISSION** (só disponível com todos GO)
- Durante a contagem você pode:
  - **HOLD** — pausa a contagem (útil para resolver imprevistos)
  - **RESUME** — retoma de onde parou
  - **SCRUB** — aborta a missão completamente

**3. Auto-start**

Quando o countdown chega a T-0, o sistema entra em modo de espera. Assim que o ESP32 detectar empuxo acima de 2.0 N, a sessão inicia automaticamente — sem precisar clicar START.

---

### Overlay (`overlay.html`) — Transmissão ao vivo

Interface minimalista para exibir em segundo plano durante gravação ou livestream. Mostra apenas o essencial:

- Valor de empuxo em tempo real (grande, legível)
- Arco de progresso visual
- Gráfico de histórico
- Countdown de missão (quando ativo)
- Banner de resultado ao final da queima

**Como usar:**

Abra em uma aba separada ou em um software de captura (OBS, etc.) como fonte de navegador. Passe o IP do servidor como parâmetro:

```
http://<IP>:8080/overlay.html?ip=<IP>&port=8766
```

---

## 8. Como conduzir um teste

### Procedimento padrão (sem countdown)

```
1. Servidor rodando, ESP32 conectado (dots verdes no dashboard)
2. Fixar a célula de carga no suporte, sem o motor
3. Clicar TARE → aguardar log "Tara enviada ao ESP32"
4. Prender o motor na célula de carga
5. Afastar todos da área
6. Clicar START → state muda para RUNNING
7. Acionar a ignição
8. O sistema detecta automaticamente a queima (empuxo > 2 N)
9. Aguardar o fim da queima
10. O sistema encerra automaticamente (state → COMPLETO)
11. Clicar PLOT para gerar o gráfico
12. Dados salvos em logs/ e plots/
```

### Procedimento com countdown (Layer Control)

```
1. Abrir layer_control.html em paralelo com o dashboard
2. Marcar todos os itens como GO no checklist
3. Configurar o tempo de contagem (ex: 60 s)
4. Clicar START MISSION
5. Acompanhar o countdown — usar HOLD se necessário
6. Em T-0, o sistema aguarda o empuxo para iniciar automaticamente
7. Resto do procedimento igual ao padrão
```

---

## 9. Lendo e interpretando os dados

### O que é cada dado

| Dado | Unidade | Descrição |
|---|---|---|
| `force_N` | Newtons (N) | Empuxo filtrado (EMA α=0.15 + α=0.3 em cascata) |
| `force_raw_N` | Newtons (N) | Leitura bruta do HX711, sem suavização extra |
| `elapsed_s` | Segundos | Tempo desde o início da queima detectada |
| `timestamp_ms` | Milissegundos | Tempo do ESP32 desde o boot (millis()) |

### Métricas calculadas

**Empuxo de pico (N)**
O maior valor instantâneo registrado. Representa o máximo que o motor pode entregar por um instante. Não use este número para calcular se o foguete levantará.

**Empuxo médio (N)**
Média de todas as amostras durante a queima. Este é o número correto para calcular a capacidade de levantamento.

**Impulso total (N·s)**
Integral da força ao longo do tempo — a "energia" entregue pelo motor. Usado para classificar motores no sistema NARAM/NAR (A, B, C... cada letra dobra o impulso).

**Duração da queima (s)**
Tempo entre o primeiro e o último momento com empuxo acima de 2.0 N.

### Capacidade de levantamento

Para saber se o motor levanta o foguete:

```
Empuxo médio (N) ÷ 9.80665 = capacidade em kgf

Exemplo: 10.64 N ÷ 9.80665 = 1.08 kgf
```

O foguete precisa pesar **menos** que esse valor para subir. Para um voo estável, recomenda-se que o foguete pese no máximo **1/5** do empuxo médio em kgf.

### Lendo o CSV

O arquivo `logs/test_AAAAMMDD_HHMMSS.csv` tem quatro colunas:

```csv
timestamp_ms, elapsed_s, force_N, force_raw_N
622839,       0.0000,    -1.27,   -1.29    ← pré-queima (ruído)
...
630000,       1.2000,    15.43,   15.81    ← durante queima
...
```

- Linhas com `elapsed_s = 0.0000` são amostras pré-queima (sessão estava running mas queima não detectada ainda)
- Valores negativos pequenos (~−1 N) são ruído de fundo — indicam que a tara estava levemente off
- A queima real começa quando `force_N` cruza 2.0 N

### Lendo o gráfico PNG

O gráfico gerado em `plots/plot_AAAAMMDD_HHMMSS.png` tem dois painéis:

**Painel superior — Curva de empuxo**
- Área vermelha escura = empuxo total ao longo do tempo
- Área laranja mais clara = período de queima ativa
- Linha tracejada amarela = pico de empuxo
- Linha tracejada cinza = threshold de queima (2.0 N)
- Caixa de texto = resumo: impulso, força média, pico e duração

**Painel inferior — Taxa de variação (dF/dt)**
- Mostra com que velocidade a força está mudando (N/s)
- Pico positivo = fase de aceleração do motor
- Pico negativo = fase de desaceleração / extinção
- Útil para identificar instabilidades na combustão

---

## 10. Máquina de missão (countdown)

O sistema usa uma máquina de estados para controle de missão:

```
IDLE ──► COUNTING ──► (T-0) ──► aguarda empuxo ──► [sessão inicia]
           │
           ▼
          HOLD ──► COUNTING (resume)
           │
           ▼
         SCRUB
```

**Estados:**

| Estado | Significado |
|---|---|
| `mission_idle` | Aguardando início da contagem |
| `mission_counting` | Contagem regressiva ativa |
| `mission_hold` | Contagem pausada (Hold) |
| `mission_scrubbed` | Missão abortada |

**Regras importantes:**
- A contagem só inicia se **todos** os itens do GO/NO-GO estiverem marcados como GO
- Se a missão for Scrubbed enquanto há sessão ativa, a sessão é encerrada automaticamente
- Após T-0, o sistema aguarda o empuxo — se o motor não acionar, você precisa acionar um SCRUB manual

---

## 11. LEDs do ESP32

Os LEDs dão feedback visual sem precisar abrir nenhuma interface:

| LED Amarelo | LED Vermelho | Significado |
|---|---|---|
| Pisca rápido | Apagado | Sem WiFi ou sem WebSocket |
| Pisca lento | Apagado | Conectado, aguardando sessão |
| Pulso breve (heartbeat) | Apagado | Sessão armada, lendo HX711 |
| Apagado | Aceso fixo | Queima detectada — NÃO SE APROXIME |
| Apagado | Pisca | Cooldown pós-queima (15 s) — motor ainda quente |
| Pisca rápido | Pisca rápido | Erro: WebSocket perdido durante sessão ativa |

> **Importante:** O LED vermelho piscando (cooldown) significa que o motor ainda pode estar quente. Aguarde os 15 segundos antes de se aproximar.

---

## 12. Arquivos gerados

```
projeto/
├── server.py
├── dashboard.html
├── overlay.html
├── layer_control.html
├── logs/
│   ├── test_20260525_203021.csv    ← dados brutos do teste
│   └── test_20260526_141500.csv
└── plots/
    ├── plot_20260525_203021.png    ← gráfico automático
    └── plot_20260526_141500.png
```

Os arquivos são acessíveis diretamente pelo dashboard (painel lateral direito) ou via URL:

- CSV: `http://<IP>:8080/logs/test_AAAAMMDD_HHMMSS.csv`
- PNG: `http://<IP>:8080/plots/plot_AAAAMMDD_HHMMSS.png`

---

## 13. Solução de problemas

### ESP32 não conecta ao WiFi
- Verifique se `FORCE_PORTAL` está `false` após a configuração inicial
- Segure BOOT ao ligar para reabrir o portal
- Confirme que o IP do servidor está correto no portal

### Dashboard mostra "Backend inacessível"
- Verifique se `server.py` está rodando
- Confirme que você está acessando o IP correto da máquina do servidor
- O firewall pode estar bloqueando a porta 8080 — libere temporariamente

### Dot ESP32 não fica verde
- Verifique o Serial Monitor do ESP32 para ver se ele conectou ao WiFi
- Confirme que o IP do servidor salvo no ESP32 está correto
- Tente reconfigurar via portal BOOT

### Leituras negativas em repouso (ex: −1.2 N)
- A tara foi feita com algo pousado na célula, ou a célula está sobrecarregada
- Clique TARE com a célula completamente livre de carga
- Se persistir, verifique a montagem mecânica da célula

### Gráfico não gera / botão PLOT não funciona
- O botão PLOT só fica ativo após o estado ir para COMPLETO
- Verifique se há permissão de escrita na pasta `plots/`

### Valor de força oscila muito
- Ajuste `EMA_ALPHA` no firmware para um valor menor (mais suavização), ex: `0.08`
- Verifique se há vibração mecânica no suporte da célula de carga
- Certifique-se de que o cabo do HX711 não está perto de fontes de interferência

### Servidor trava no Windows ao minimizar o terminal
- O Windows tem um recurso chamado QuickEdit que pausa o processo quando você clica no terminal
- O servidor já desabilita isso automaticamente, mas se travar: clique na janela do terminal e pressione Enter

---

## 14. Parâmetros técnicos

### ESP32 — `esp32_telemetria.ino`

| Parâmetro | Valor padrão | Descrição |
|---|---|---|
| `CAL_FACTOR` | −2280.0 | Fator de calibração da célula de carga |
| `EMA_ALPHA` | 0.15 | Suavização exponencial (0 = máximo suave, 1 = sem filtro) |
| `NOISE_FLOOR` | 0.25 N | Leituras abaixo desse valor são zeradas |
| `SEND_INTERVAL_MS` | 20 ms | Taxa de envio = 50 Hz |
| `SERVER_PORT` | 8765 | Porta WebSocket do servidor |
| `LED_COOLDOWN_MS` | 15000 ms | Tempo de cooldown pós-queima |

### Servidor — `server.py`

| Parâmetro | Valor padrão | Descrição |
|---|---|---|
| `BURN_THRESHOLD` | 2.0 N | Limiar de detecção de queima |
| `BURN_END_DELAY` | 1.5 s | Tempo abaixo do limiar para confirmar fim da queima |
| `MAX_FORCE` | 120.0 N | Escala máxima dos gráficos PNG |
| `ESP32_PORT` | 8765 | Porta WebSocket para o ESP32 |
| `CLIENT_PORT` | 8766 | Porta WebSocket para os clientes (dashboard, overlay) |
| `HTTP_PORT` | 8080 | Porta HTTP para as interfaces web |
| `ESP_HEARTBEAT_TIMEOUT` | 5.0 s | Tempo sem mensagem para marcar ESP como desconectado |

### Portas utilizadas

| Porta | Protocolo | Uso |
|---|---|---|
| 8765 | WebSocket | Comunicação exclusiva com o ESP32 |
| 8766 | WebSocket | Broadcast para dashboard, overlay e layer control |
| 8080 | HTTP | Arquivos estáticos, API e download de logs/plots |

---

*Documentação gerada com base no código-fonte v2.5/v4.0 — maio de 2026.*
