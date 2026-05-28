#!/usr/bin/env python3
"""
====
  SERVIDOR DE TELEMETRIA DE EMPUXO  v4.0 (Layer Control)
====
  Arquitetura:
    - Máquina de estados central: idle → running → complete
    - Estado `complete` só sai via reset explícito
    - WebSocket ESP32  (porta 8765)
    - WebSocket Clientes (porta 8766) — broadcast em tempo real
    - HTTP (porta 8080) — arquivos estáticos + API REST
    - Log CSV + gráfico PNG automático

  Instalar:
    pip install websockets matplotlib numpy aiohttp

  Uso:
    python server.py
    Dashboard: http://localhost:8080/dashboard.html
====
"""

import asyncio
import json
import csv
import time
import sys
import logging
import signal
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from datetime import datetime
from pathlib import Path
from enum import Enum
from typing import Set, Dict, Optional, Tuple, List
from aiohttp import web
from websockets.server import serve

# ── Configuração ────
ESP32_HOST     = "0.0.0.0"
ESP32_PORT     = 8765
CLIENT_PORT    = 8766
HTTP_PORT      = 8080
LOG_DIR        = Path("logs")
PLOT_DIR       = Path("plots")
BURN_THRESHOLD = 2.0    # N — limiar para detectar queima
BURN_END_DELAY = 1.5    # s — tempo abaixo do limiar para encerrar queima
MAX_FORCE      = 120.0  # N — máximo esperado (para gráficos)

# ── Logging ────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("telemetria")


# ════
#  MÁQUINA DE ESTADOS
# ════
class State(str, Enum):
    """
    Estados possíveis do sistema:

        idle     → Sistema em espera, sem sessão ativa.
        running  → Sessão ativa; monitorando e registrando dados.
                   Sub-estado implícito: burning (detectado automaticamente).
        complete → Teste finalizado (automático ou manual).
                   NÃO sai deste estado sem um reset explícito.
    """
    IDLE     = "idle"
    RUNNING  = "running"
    COMPLETE = "complete"

# Transições permitidas: (estado_atual, novo_estado)
ALLOWED_TRANSITIONS: Set[Tuple[State, State]] = {
    (State.IDLE,     State.RUNNING),   # start
    (State.RUNNING,  State.COMPLETE),  # stop / burn_end automático
    (State.COMPLETE, State.IDLE),      # reset (único caminho de saída de complete)
}


class TestSession:
    """Encapsula todo o estado de uma sessão de teste."""

    def __init__(self):
        self._state: State = State.IDLE
        self.reset_data()

    # ── Máquina de estados ────
    @property
    def state(self) -> State:
        return self._state

    def transition(self, new_state: State) -> bool:
        """Tenta mudar de estado. Retorna True se a transição foi aceita."""
        if (self._state, new_state) not in ALLOWED_TRANSITIONS:
            log.warning(
                f"[FSM] Transição NEGADA: {self._state} → {new_state}"
            )
            return False
        log.info(f"[FSM] {self._state} → {new_state}")
        self._state = new_state
        return True

    # ── Dados da sessão ────
    def reset_data(self):
        """Zera os dados de medição sem mexer no estado."""
        self.burning       = False
        self.start_time: Optional[float]    = None
        self.burn_end_time: Optional[float] = None
        self.peak_force    = 0.0
        self.impulse       = 0.0
        self.last_t_epoch: Optional[float]  = None
        self.last_force    = 0.0
        self.samples: List[Tuple[float, float]] = []
        self.csv_file: Optional[object]      = None
        self.csv_writer: Optional[object]    = None
        self.session_name  = ""
        self.burn_duration = 0.0
        self._ema: Optional[float]  = None
        self._ema_alpha    = 0.3

    def full_reset(self) -> bool:
        """Reset completo: só permitido se não estiver em running."""
        if self._state == State.RUNNING:
            log.warning("[RESET] Pare a sessão antes de resetar.")
            return False
        self.reset_data()
        self._state = State.IDLE
        log.info("[RESET] Sistema resetado para idle.")
        return True

    # ── Filtro de sinal ────
    def filter(self, raw: float) -> float:
        if self._ema is None:
            self._ema = raw
        else:
            self._ema = self._ema_alpha * raw + (1 - self._ema_alpha) * self._ema
        return self._ema

    # ── CSV ────
    def open_log(self) -> str:
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_name = ts
        path = LOG_DIR / f"test_{ts}.csv"
        f = open(path, "w", newline="", encoding="utf-8")
        self.csv_file   = f
        self.csv_writer = csv.writer(f)
        self.csv_writer.writerow(["timestamp_ms", "elapsed_s", "force_N", "force_raw_N"])
        log.info(f"[LOG] Aberto: {path}")
        return str(path)

    def close_log(self):
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception as e:
                log.warning(f"[LOG] Erro ao fechar arquivo: {e}")
            finally:
                self.csv_file   = None
                self.csv_writer = None
                log.info("[LOG] Fechado.")

    def write_sample(self, t_ms: int, elapsed: float, force: float, raw: float):
        if self.csv_writer:
            try:
                self.csv_writer.writerow([t_ms, f"{elapsed:.4f}", f"{force:.4f}", f"{raw:.4f}"])
                self.csv_file.flush()
            except Exception as e:
                log.warning(f"[LOG] Erro ao escrever: {e}")

    # ── Snapshot do estado (para broadcast) ────
    def snapshot(self) -> Dict:
        return {
            "state":    self._state.value,
            "burning":  self.burning,
            "peak":     round(self.peak_force, 2),
            "impulse":  round(self.impulse, 4),
            "duration": round(self.burn_duration, 3),
            "session":  self.session_name,
            "samples":  len(self.samples),
        }



# ════════════════════════════════════════════════════════════════
#  CONTROLE DE MISSÃO — T-minus / Hold / Scrub / GO-NOGO
# ════════════════════════════════════════════════════════════════

class MissionState(str, Enum):
    IDLE     = "mission_idle"
    COUNTING = "mission_counting"
    HOLD     = "mission_hold"
    SCRUBBED = "mission_scrubbed"

DEFAULT_GONOGO = [
    {"id": "esp32",  "label": "ESP32",           "auto": True,  "go": False},
    {"id": "camera", "label": "Câmera",           "auto": False, "go": False},
    {"id": "hx711",  "label": "HX711 / Balança",  "auto": False, "go": False},
    {"id": "area",   "label": "Área Livre",        "auto": False, "go": False},
    {"id": "clima",  "label": "Condição Climática","auto": False, "go": False},
]

class MissionControl:
    def __init__(self):
        self.state: MissionState = MissionState.IDLE
        self.t_seconds: int      = 60
        self.seconds_left: int   = 60
        self.hold_reason: str    = ""
        self.scrub_reason: str   = ""
        self.countdown_task      = None
        self.t0_reached: bool    = False
        self.gonogo: list        = [dict(i) for i in DEFAULT_GONOGO]

    def all_go(self) -> bool:
        return all(item["go"] for item in self.gonogo)

    def reset(self):
        if self.countdown_task and not self.countdown_task.done():
            self.countdown_task.cancel()
        self.state        = MissionState.IDLE
        self.seconds_left = self.t_seconds
        self.hold_reason  = ""
        self.scrub_reason = ""
        self.t0_reached   = False
        self.countdown_task = None

    def snapshot(self) -> dict:
        return {
            "mission_state":   self.state.value,
            "mission_seconds": self.seconds_left,
            "mission_t":       self.t_seconds,
            "hold_reason":     self.hold_reason,
            "scrub_reason":    self.scrub_reason,
            "gonogo":          self.gonogo,
            "all_go":          self.all_go(),
            "t0_reached":      self.t0_reached,
        }


# ── Singletons globais ────
session            = TestSession()
mission            = MissionControl()
client_connections: Set = set()
esp_connected: bool     = False
esp_websocket           = None          # referencia ao websocket do ESP32
esp_last_seen: float    = 0.0   # epoch da última mensagem recebida do ESP
esp_cooldown_until: float = 0.0  # epoch até quando o ESP está em cooldown de LED
shutdown_event: Optional[asyncio.Event] = None
_start_time: float      = time.time()  # para uptime no /api/health

# Tempo máximo sem mensagem do ESP antes de considerar desconectado (s)
ESP_HEARTBEAT_TIMEOUT = 5.0


# ════
#  GERADOR DE GRÁFICO
# ════
def generate_plot(session_name: str, samples: List[Tuple[float, float]]) -> str:
    PLOT_DIR.mkdir(exist_ok=True)
    if len(samples) < 5:
        log.warning("[PLOT] Amostras insuficientes.")
        return ""

    times  = np.array([s[0] for s in samples], dtype=float)
    forces = np.array([s[1] for s in samples], dtype=float)
    times  = times - times[0]

    peak     = float(np.max(forces))
    
    # ✅ CORREÇÃO DE INCOMPATIBILIDADE (NumPy 1.x vs NumPy 2.0+)
    if hasattr(np, 'trapezoid'):
        impulse = float(np.trapezoid(forces, times))
    else:
        impulse = float(np.trapz(forces, times))

    above    = forces > BURN_THRESHOLD
    avg_f    = float(np.mean(forces[above])) if np.any(above) else 0.0
    burn_dur = float(times[above][-1] - times[above][0]) if np.any(above) else 0.0

    fig, axes = plt.subplots(2, 1, figsize=(14, 8),
                    gridspec_kw={'height_ratios': [3, 1]})
    fig.patch.set_facecolor('#0d0d0f')

    ax = axes[0]
    ax.set_facecolor('#111114')  
    ax.fill_between(times, forces, alpha=0.25, color='#e53935')
    ax.fill_between(times, forces, where=above, alpha=0.35, color='#ff6b35')
    ax.plot(times, forces, color='#ff4444', linewidth=2.2, solid_capstyle='round')
    ax.axhline(peak, color='#ffcc00', linewidth=0.8, linestyle='--', alpha=0.7)
    ax.text(times[-1] * 0.02, peak + max(peak, MAX_FORCE) * 0.02,
            f'Pico: {peak:.1f} N', color='#ffcc00', fontsize=9, fontweight='bold')
    ax.axhline(BURN_THRESHOLD, color='#555555', linewidth=0.6, linestyle=':', alpha=0.5)

    info = (
        f"Impulso Total: {impulse:.3f} N·s\n"
        f"Força Média:   {avg_f:.2f} N\n"
        f"Força de Pico: {peak:.1f} N\n"
        f"Tempo Queima:  {burn_dur:.3f} s"
    )
    ax.text(0.98, 0.97, info, transform=ax.transAxes, fontsize=10,
            verticalalignment='top', horizontalalignment='right',
            color='#cccccc',  
            fontfamily='monospace',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a1f',
                    edgecolor='#333333', alpha=0.9))

    ax.set_xlim(left=0)
    ax.set_ylim(bottom=-max(peak * 0.05, 1))
    ax.set_xlabel("Tempo (s)", color='#888', fontsize=10)
    ax.set_ylabel("Força (N)", color='#888', fontsize=10)
    ax.set_title(f"Curva de Empuxo — Sessão {session_name}",
                 color='#ddd', fontsize=13, fontweight='bold', pad=12)
    ax.tick_params(colors='#666')
    ax.spines[:].set_color('#2a2a2f')
    ax.grid(True, color='#2a2a2f', linewidth=0.5)
    ax.grid(True, which='minor', color='#1d1d22', linewidth=0.3, linestyle=':')
    ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())
    ax.xaxis.set_minor_locator(ticker.AutoMinorLocator())

    ax2 = axes[1]
    ax2.set_facecolor('#111114')  
    if len(forces) > 2:
        dt = np.diff(times)
        dt[dt == 0] = 1e-6
        dF = np.diff(forces) / dt
        ax2.plot(times[1:], dF, color='#4fc3f7', linewidth=1.2, alpha=0.8)
        ax2.axhline(0, color='#444', linewidth=0.6)
        ax2.fill_between(times[1:], dF, alpha=0.15, color='#4fc3f7')
    ax2.set_ylabel("dF/dt (N/s)", color='#888', fontsize=8)
    ax2.set_xlabel("Tempo (s)", color='#888', fontsize=8)
    ax2.tick_params(colors='#666')
    ax2.spines[:].set_color('#2a2a2f')
    ax2.grid(True, color='#2a2a2f', linewidth=0.4)
    ax2.set_xlim(left=0)

    plt.tight_layout(pad=1.5)
    out = PLOT_DIR / f"plot_{session_name}.png"
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    log.info(f"[PLOT] Salvo: {out}")
    return str(out)


# ════
#  BROADCAST (CORRIGIDO COM GLOBAL)
# ════
async def broadcast(msg: Dict):
    """Envia mensagem para todos os clientes conectados."""
    global client_connections  # ✅ NECESSÁRIO
    
    if not client_connections:
        return
    
    data = json.dumps(msg, ensure_ascii=False)
    dead: Set = set()
    
    for ws in list(client_connections):  # Cópia para segurança
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    
    if dead:
        client_connections -= dead


async def broadcast_state(extra: Optional[Dict] = None):
    """Broadcast padronizado do estado atual do sistema."""
    global esp_connected  # ✅ NECESSÁRIO
    
    msg: Dict = {
        "type": "state_update",
        **session.snapshot(),
        **mission.snapshot(),
        "esp": esp_connected
    }
    if extra:
        msg.update(extra)
    await broadcast(msg)


async def send_esp_led(state: str):
    """
    Envia comando de estado LED para o ESP32.
    Estados: 'armed' | 'burning' | 'cooldown' | 'idle'

    BUGFIX: Nunca envia 'idle' enquanto o ESP ainda está dentro do janela de
    cooldown — evita cancelar prematuramente o aviso de motor quente.
    O ESP também possui a mesma proteção do lado dele (defesa em profundidade).
    """
    global esp_websocket, esp_cooldown_until

    # LED_COOLDOWN_MS no ESP32 é 15 s — usamos 16 s de margem no servidor
    ESP_COOLDOWN_DURATION = 16.0

    if state == "idle" and time.time() < esp_cooldown_until:
        remaining = esp_cooldown_until - time.time()
        log.info(f"[LED→ESP] 'idle' ignorado — ESP em cooldown por mais {remaining:.1f}s")
        return

    if state == "cooldown":
        esp_cooldown_until = time.time() + ESP_COOLDOWN_DURATION
        log.info(f"[LED→ESP] Cooldown iniciado. 'idle' bloqueado por {ESP_COOLDOWN_DURATION}s")

    if esp_websocket is None:
        return
    try:
        await esp_websocket.send(json.dumps({"led": state}))
        log.info(f"[LED→ESP] estado={state}")
    except Exception as e:
        log.warning(f"[LED→ESP] Falha ao enviar comando: {e}")


# ════
#  HANDLER: ESP32
# ════
async def handle_esp32(websocket):
    global esp_connected, esp_last_seen, esp_websocket
    addr = websocket.remote_address
    log.info(f"[ESP32] Conectado: {addr}")
    esp_connected  = True
    esp_websocket  = websocket
    esp_last_seen  = time.time()
    for item in mission.gonogo:
        if item["id"] == "esp32": item["go"] = True
    await broadcast_state()
    await broadcast({"type": "gonogo_update", **mission.snapshot()})
    asyncio.create_task(resync_led_on_connect())  # reenvia estado LED atual

    try:
        async for raw in websocket:
            esp_last_seen = time.time()  # ✅ atualiza heartbeat a cada mensagem

            try:
                d = json.loads(raw)
            except Exception:
                continue

            if "hello" in d:
                log.info(f"[ESP32] Handshake: {d}")
                continue

            # ✅ Responde ping de heartbeat do ESP ({"ping": 1})
            if "ping" in d:
                continue

            if "f" not in d:
                continue
            
            try:
                esp_raw = float(d["f"])
                raw_r   = float(d.get("r", esp_raw))
                t_ms    = int(d.get("t", time.time() * 1000))
            except (ValueError, TypeError):
                log.debug("[ESP32] JSON inválido recebido")
                continue

            force = session.filter(esp_raw)
            if abs(force) < 0.15:
                force = 0.0

            # ── Auto-start: T-0 atingido + empuxo detectado → inicia sessão ────
            # DEVE ficar antes do early-continue, pois state ainda é IDLE aqui.
            if (session.state == State.IDLE and mission.t0_reached
                    and force >= BURN_THRESHOLD):
                if session.transition(State.RUNNING):
                    session.reset_data()
                    session.open_log()
                    mission.t0_reached = False
                    await send_esp_led("armed")
                    await broadcast_state({"event": "session_start",
                                          "session": session.session_name})
                    log.info("[MISSION] Auto-start por empuxo após T-0.")

            # Sempre transmite dado bruto mesmo sem sessão ativa
            if session.state != State.RUNNING:
                await broadcast({
                    "type":    "data",
                    "f":       round(force, 2),
                    "r":       round(raw_r, 2),
                    "t":       t_ms,
                    **session.snapshot(),
                    "esp":     esp_connected,
                })
                continue

            # ── Dentro de uma sessão running ────
            now_epoch = time.time()

            if not session.burning and force >= BURN_THRESHOLD:
                session.burning       = True
                session.start_time    = now_epoch
                session.burn_end_time = None
                log.info(f"[QUEIMA] Detectada! F={force:.2f}N")
                await broadcast({"type": "burn_start", **session.snapshot()})
                await send_esp_led("burning")    # Amarelo + vermelho fixos

            elapsed = (now_epoch - session.start_time) if session.start_time else 0.0

            # Detecta fim da queima
            if session.burning and force < BURN_THRESHOLD:
                if session.burn_end_time is None:
                    session.burn_end_time = now_epoch
                elif (now_epoch - session.burn_end_time) >= BURN_END_DELAY:
                    session.burning       = False
                    session.burn_duration = session.burn_end_time - session.start_time
                    log.info(f"[QUEIMA] Finalizada. Duração={session.burn_duration:.3f}s")
                    await broadcast({"type": "burn_end", **session.snapshot()})
                    await send_esp_led("cooldown")   # Vermelho pisca — motor quente
                    # Queima terminou: encerra sessão automaticamente
                    asyncio.create_task(finalize_session("auto"))
            elif session.burning and force >= BURN_THRESHOLD:
                session.burn_end_time = None

            # Integração trapezoidal do impulso
            if session.last_t_epoch is not None and session.burning:
                dt = now_epoch - session.last_t_epoch
                if 0 < dt < 1.0:
                    session.impulse += 0.5 * (force + session.last_force) * dt

            session.last_t_epoch = now_epoch
            session.last_force   = force

            if force > session.peak_force:
                session.peak_force = force

            session.samples.append((now_epoch, force))
            session.write_sample(t_ms, elapsed, force, raw_r)

            await broadcast({
                "type":    "data",
                "f":       round(force, 2),
                "r":       round(raw_r, 2),
                "t":       t_ms,
                "elapsed": round(elapsed, 3),
                **session.snapshot(),
                "esp":     esp_connected,
            })

    except Exception as e:
        log.warning(f"[ESP32] Conexão encerrada: {e}")
    finally:
        esp_connected = False
        esp_websocket = None
        for item in mission.gonogo:
            if item["id"] == "esp32":
                item["go"] = False
                break
        log.info("[ESP32] Desconectado.")
        await broadcast_state()
        await broadcast({"type": "gonogo_update", **mission.snapshot()})


# ════
#  HANDLER: CLIENTES (Dashboard / Overlay)
# ════
async def handle_client(websocket):
    global client_connections
    client_connections.add(websocket)
    remote = websocket.remote_address
    log.info(f"[Cliente] Conectado {remote}. Total: {len(client_connections)}")

    # Sincronização imediata do estado ao conectar
    try:
        await websocket.send(json.dumps({
            "type": "state_update",
            **session.snapshot(),
            "esp": esp_connected,
        }))
    except Exception as e:
        log.debug(f"[Cliente] Erro ao sincronizar: {e}")

    try:
        async for raw in websocket:
            try:
                cmd = json.loads(raw)
                await handle_command(cmd)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.warning(f"[CMD] Erro: {e}")
    except Exception:
        pass
    finally:
        client_connections.discard(websocket)
        log.info(f"[Cliente] Desconectado {remote}. Total: {len(client_connections)}")


# ════
#  PROCESSAMENTO DE COMANDOS
# ════

# ════
#  MISSÃO — COUNTDOWN / HOLD / SCRUB / GO-NOGO
# ════

async def mission_countdown_task():
    global mission
    log.info(f"[MISSION] Contagem T-{mission.t_seconds}s iniciada.")
    try:
        while True:
            if mission.state == MissionState.HOLD:
                await broadcast({"type": "mission_tick", **mission.snapshot()})
                await asyncio.sleep(5.0)
                continue
            if mission.state != MissionState.COUNTING:
                return
            await broadcast({"type": "mission_tick", **mission.snapshot()})
            if mission.seconds_left == 0:
                mission.state     = MissionState.IDLE
                mission.t0_reached = True
                log.info("[MISSION] T-0! Aguardando empuxo para auto-start.")
                await broadcast({"type": "mission_t0", **mission.snapshot()})
                return
            mission.seconds_left -= 1
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        log.info("[MISSION] Countdown cancelado.")


async def handle_mission_command(action: str, cmd: dict):
    global mission, session

    if action == "mission_start":
        if mission.state not in (MissionState.IDLE, MissionState.SCRUBBED):
            return
        if not mission.all_go():
            not_go = [i["label"] for i in mission.gonogo if not i["go"]]
            log.warning(f"[MISSION] Início BLOQUEADO — NO-GO: {', '.join(not_go)}")
            await broadcast({"type": "mission_blocked",
                             "reason": f"NO-GO: {', '.join(not_go)}",
                             **mission.snapshot()})
            return
        t = cmd.get("t_seconds")
        if isinstance(t, int) and 5 <= t <= 3600:
            mission.t_seconds = t
        mission.reset()
        mission.state        = MissionState.COUNTING
        mission.seconds_left = mission.t_seconds
        mission.countdown_task = asyncio.create_task(mission_countdown_task())
        log.info(f"[MISSION] Contagem iniciada T-{mission.t_seconds}s")
        await broadcast({"type": "mission_start", **mission.snapshot()})

    elif action == "mission_hold":
        if mission.state != MissionState.COUNTING:
            return
        mission.state       = MissionState.HOLD
        mission.hold_reason = cmd.get("reason", "Hold solicitado pelo operador")
        log.warning(f"[MISSION] HOLD — {mission.hold_reason}")
        await broadcast({"type": "mission_hold", **mission.snapshot()})

    elif action == "mission_resume":
        if mission.state != MissionState.HOLD:
            return
        mission.state       = MissionState.COUNTING
        mission.hold_reason = ""
        log.info("[MISSION] Contagem retomada.")
        await broadcast({"type": "mission_resume", **mission.snapshot()})

    elif action == "mission_scrub":
        if mission.state == MissionState.IDLE and not mission.t0_reached:
            return
        if mission.countdown_task and not mission.countdown_task.done():
            mission.countdown_task.cancel()
        mission.state        = MissionState.SCRUBBED
        mission.scrub_reason = cmd.get("reason", "Scrub solicitado pelo operador")
        log.warning(f"[MISSION] SCRUB — {mission.scrub_reason}")
        if session.state == State.RUNNING:
            # finalize_session faz a transição RUNNING→COMPLETE, fecha CSV e gera gráfico
            asyncio.create_task(finalize_session("scrub"))
            await send_esp_led("idle")
            log.info("[MISSION] Sessão encerrada por Scrub.")
        await broadcast({"type": "mission_scrub", **mission.snapshot(),
                         **session.snapshot(), "esp": esp_connected})

    elif action == "mission_reset":
        mission.reset()
        log.info("[MISSION] Missão resetada.")
        await broadcast({"type": "mission_reset", **mission.snapshot()})

    elif action == "mission_set_t":
        t = cmd.get("t_seconds")
        if isinstance(t, int) and 5 <= t <= 3600:
            mission.t_seconds    = t
            if mission.state == MissionState.IDLE:
                mission.seconds_left = t
            log.info(f"[MISSION] T configurado: {t}s")
            await broadcast({"type": "mission_config", **mission.snapshot()})

    elif action == "gonogo_set":
        item_id = cmd.get("id")
        go_val  = cmd.get("go")
        if item_id is None or go_val is None:
            return
        for item in mission.gonogo:
            if item["id"] == item_id and not item.get("auto"):
                item["go"] = bool(go_val)
                break
        log.info(f"[MISSION] GO/NO-GO: {item_id} = {'GO' if go_val else 'NO-GO'}")
        await broadcast({"type": "gonogo_update", **mission.snapshot()})


async def handle_command(cmd: Dict):
    action = cmd.get("action", "")
    if not action:
        return
    log.info(f"[CMD] {action}")

    if action.startswith("mission_") or action == "gonogo_set":
        await handle_mission_command(action, cmd)
        return

    if action == "start":
        if session.transition(State.RUNNING):
            session.reset_data()
            path = session.open_log()
            log.info(f"[SESSÃO] Iniciada. Log: {path}")
            await send_esp_led("armed")          # LED amarelo fixo — sessao armada
            await broadcast_state({"event": "session_start"})

    elif action == "stop":
        if session.state == State.RUNNING:
            asyncio.create_task(finalize_session("manual"))
        else:
            log.warning("[STOP] Nenhuma sessão ativa.")

    elif action == "reset":
        if session.full_reset():
            await send_esp_led("idle")           # Garante LEDs voltam ao idle
            await broadcast_state({"event": "reset"})

    elif action == "tare":
        log.info("[TARE] Solicitado.")
        if esp_websocket is not None:
            try:
                await esp_websocket.send(json.dumps({"tare": True}))
                log.info("[TARE] Comando enviado ao ESP32.")
            except Exception as e:
                log.warning(f"[TARE] Falha ao enviar para ESP32: {e}")
        else:
            log.warning("[TARE] ESP32 não conectado — tare ignorado.")
        await broadcast({"type": "tare_ack", "esp_reached": esp_websocket is not None})

    elif action == "plot":
        if session.samples:
            loop = asyncio.get_running_loop()  
            try:
                path = await loop.run_in_executor(
                    None, generate_plot, session.session_name, list(session.samples)
                )
                if path:
                    await broadcast({
                        "type": "plot_ready",
                        "path": path,
                        "name": Path(path).name,
                    })
            except Exception as e:
                log.error(f"[PLOT] Erro ao processar comando manual de plot: {e}")
        else:
            log.warning("[PLOT] Sem amostras.")

    elif action == "get_logs":
        logs = sorted(LOG_DIR.glob("*.csv"), reverse=True) if LOG_DIR.exists() else []
        await broadcast({"type": "log_list", "logs": [p.name for p in logs[:20]]})

    elif action == "get_plots":
        plots = sorted(PLOT_DIR.glob("*.png"), reverse=True) if PLOT_DIR.exists() else []
        await broadcast({"type": "plot_list", "plots": [p.name for p in plots[:20]]})

    elif action == "overlay_ctrl":
        target = cmd.get("target", "")
        show   = bool(cmd.get("show", True))
        if target:
            await broadcast({"type": "overlay_ctrl", "target": target, "show": show})

    elif action == "overlay_visibility":
        show = bool(cmd.get("show", True))
        await broadcast({"type": "overlay_visibility", "show": show})


async def finalize_session(reason: str = "manual"):
    """Encerra sessão, transita para complete, fecha CSV e gera gráfico."""
    if session.state != State.RUNNING:
        return

    await asyncio.sleep(0.3)

    if not session.transition(State.COMPLETE):
        return

    # Garante LED em idle ao finalizar sessao (se nao for cooldown)
    # Cooldown ja foi enviado no burn_end; aqui so garante reset em stop manual
    if reason == "manual":
        await send_esp_led("idle")

    session.close_log()

    samples_snap = list(session.samples)
    sname        = session.session_name
    peak         = round(session.peak_force, 2)
    impulse      = round(session.impulse, 4)
    burn_dur     = round(session.burn_duration, 3)

    log.info(f"[SESSÃO] Finalizando ({reason}). {len(samples_snap)} amostras.")

    # ✅ get_running_loop() — correto dentro de coroutine (Python 3.10+)
    loop = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(None, generate_plot, sname, samples_snap)
    except Exception as e:
        log.error(f"[PLOT] Erro ao gerar gráfico: {e}")
        path = ""
    plot_name = Path(path).name if path else ""

    await broadcast_state({
        "event":        "session_end",
        "reason":       reason,
        "plot":         plot_name,
        "sample_count": len(samples_snap),
    })
    log.info(f"[SESSÃO] Finalizada. Pico={peak}N Impulso={impulse}N·s Dur={burn_dur}s")


# ════
#  HTTP SERVER
# ════
async def serve_file(request):
    fname = request.match_info.get("filename", "dashboard.html")
    if ".." in fname or fname.startswith("/"):
        raise web.HTTPForbidden()
    for base in [Path("."), Path(__file__).parent]:
        fpath = base / fname
        if fpath.is_file():
            return web.FileResponse(fpath)
    raise web.HTTPNotFound()

async def serve_plot(request):
    fname = request.match_info["filename"]
    if ".." in fname:
        raise web.HTTPForbidden()
    fpath = PLOT_DIR / fname
    if fpath.is_file():
        return web.FileResponse(fpath)
    raise web.HTTPNotFound()

async def serve_log(request):
    fname = request.match_info["filename"]
    if ".." in fname:
        raise web.HTTPForbidden()
    fpath = LOG_DIR / fname
    if fpath.is_file():
        return web.FileResponse(
            fpath,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'}
        )
    raise web.HTTPNotFound()

async def api_status(request):
    return web.json_response({
        **session.snapshot(),
        "esp":     esp_connected,
        "clients": len(client_connections),
    })

async def api_health(request):
    """Endpoint de saúde — retorna 200 OK sempre que o servidor está rodando."""
    return web.json_response({
        "status":  "ok",
        "state":   session.state.value,
        "esp":     esp_connected,
        "clients": len(client_connections),
        "uptime":  round(time.time() - _start_time, 1),
    })


async def esp_heartbeat_monitor():
    """Task que monitora se o ESP32 parou de enviar dados."""
    global esp_connected, esp_websocket
    while True:
        await asyncio.sleep(1.0)
        if esp_connected and (time.time() - esp_last_seen) > ESP_HEARTBEAT_TIMEOUT:
            log.warning("[ESP32] Heartbeat timeout — marcando como desconectado.")
            esp_connected = False
            esp_websocket = None
            await broadcast_state()

async def resync_led_on_connect():
    """Após reconexao do ESP, reenvia o estado LED corrente."""
    await asyncio.sleep(1.0)   # aguarda handshake
    if session.state == State.RUNNING:
        if session.burning:
            await send_esp_led("burning")
        else:
            await send_esp_led("armed")
    else:
        await send_esp_led("idle")


# ════
#  ENTRY POINT
# ════
def disable_quickedit():
    """Desabilita QuickEdit no Windows (evita travar o event loop)."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        k32  = ctypes.windll.kernel32
        hdl  = k32.GetStdHandle(-10)
        mode = ctypes.c_ulong(0)
        k32.GetConsoleMode(hdl, ctypes.byref(mode))
        k32.SetConsoleMode(hdl, ctypes.c_ulong(mode.value & ~0x0040 & ~0x0020))
        log.info("QuickEdit desabilitado (Windows).")
    except Exception as e:
        log.debug(f"QuickEdit: {e}")


async def main():
    global shutdown_event
    disable_quickedit()

    # ✅ Event para graceful shutdown
    shutdown_event = asyncio.Event()

    log.info("=" * 58)
    log.info("  TELEMETRIA DE EMPUXO — Servidor Python  v4.0 (Layer Control)")
    log.info("=" * 58)
    log.info(f"  Estados: idle → running → complete (reset → idle)")
    log.info(f"  WebSocket ESP32   : ws://0.0.0.0:{ESP32_PORT}")
    log.info(f"  WebSocket Clientes: ws://0.0.0.0:{CLIENT_PORT}")
    log.info(f"  Dashboard HTTP    : http://localhost:{HTTP_PORT}/dashboard.html")
    log.info(f"  Overlay HTTP      : http://localhost:{HTTP_PORT}/overlay.html")
    log.info(f"  Layer Control     : http://localhost:{HTTP_PORT}/layer_control.html")
    log.info(f"  API Status        : http://localhost:{HTTP_PORT}/api/status")
    log.info("=" * 58)

    esp_server = await serve(
        handle_esp32, ESP32_HOST, ESP32_PORT,
        ping_interval=None, ping_timeout=None,
        close_timeout=10, max_size=2**16,
    )

    client_server = await serve(
        handle_client, "0.0.0.0", CLIENT_PORT,
        ping_interval=20, ping_timeout=10,
    )

    app = web.Application()
    app.router.add_get("/",                 lambda r: web.HTTPFound("/dashboard.html"))
    app.router.add_get("/{filename}",       serve_file)
    app.router.add_get("/plots/{filename}", serve_plot)
    app.router.add_get("/logs/{filename}",  serve_log)
    app.router.add_get("/api/status",       api_status)
    app.router.add_get("/api/health",       api_health)  # ✅ endpoint de saúde

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()

    log.info("Servidor rodando. Ctrl+C para parar.\n")

    # ✅ Inicia task de monitoramento de heartbeat do ESP
    asyncio.create_task(esp_heartbeat_monitor())

    # ✅ Aguarda sinal de shutdown
    try:
        await shutdown_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("\nEncerando servidor...")
        esp_server.close()
        client_server.close()
        await runner.cleanup()
        log.info("Servidor finalizado.")


def handle_signal(signum, frame):
    """Handler para SIGINT/SIGTERM."""
    if shutdown_event:
        shutdown_event.set()


if __name__ == "__main__":
    try:
        # Registra signal handlers
        signal.signal(signal.SIGINT,  handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("\nServidor interrompido pelo usuário.")