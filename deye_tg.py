#!/usr/bin/env python3
"""Deye SUN-xK-SG0xLP1 -> Telegram: свет вкл/выкл, SOC, прогноз заряда + график ЯСНО.

Инвертор опрашивается локально через Wi-Fi логгер Solarman (порт 8899, протокол V5).
Проверка регистров:  python deye_tg.py --test
Проверка графика:    python deye_tg.py --yasno
Демо всех сообщений: python deye_tg.py --demo
"""
import calendar, html, io, json, logging, os, re, signal, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from contextlib import closing
from collections import deque
from datetime import datetime, timedelta, time as dtime
from statistics import mean
from zoneinfo import ZoneInfo

from pysolarmanv5 import PySolarmanV5

E = os.environ.get
LOGGER_IP    = E("LOGGER_IP")
LOGGER_SN    = int(E("LOGGER_SN") or 0)
TG_TOKEN     = E("TG_TOKEN")
TG_CHAT      = str(E("TG_CHAT_ID"))
BATT_WH      = float(E("BATTERY_KWH", "5.12")) * 1000
MIN_SOC      = int(E("MIN_SOC", "20"))           # SOC, при котором инвертор отключает нагрузку
POLL         = int(E("POLL_SEC", "10"))
GRID_V_MIN   = float(E("GRID_V_MIN", "170"))     # ниже — считаем, что сети нет
CONFIRM      = int(E("CONFIRM", "3"))            # опросов подряд для смены состояния
SOC_ALERTS   = sorted({int(x) for x in E("SOC_ALERTS", "50,30,20").split(",")}, reverse=True)
LINK_TIMEOUT = int(E("LINK_TIMEOUT_SEC", "300"))
BATT_SIGN    = -1 if E("BATT_INVERT", "0") == "1" else 1  # Deye: + разряд, - заряд
STATE_FILE   = E("STATE_FILE", "data/state.json")  # относительно рабочей папки службы
TZ           = ZoneInfo(E("TZ_NAME", "Europe/Kyiv"))
# --- ЯСНО ---
YASNO_URL    = E("YASNO_URL", "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/25/dsos/902/planned-outages")
YASNO_GROUP  = E("YASNO_GROUP", "")              # твоя черга, напр. 3.1 (пусто — без графика)
YASNO_EVERY  = int(E("YASNO_REFRESH_SEC", "900"))
MARGIN_MIN   = int(E("CHARGE_MARGIN_MIN", "15")) # запас меньше этого — «впритык»
YASNO_CACHE  = os.path.join(os.path.dirname(STATE_FILE) or ".", "yasno.json")
YASNO_LINK   = E("YASNO_LINK", "https://yasno.ua")
# --- оформление ---
SHOW_TIMELINE = E("SHOW_TIMELINE", "1") == "1"   # полоска графика на сутки (<pre>)
SHOW_DETAILS  = E("SHOW_DETAILS", "1") == "1"    # свёрнутый блок «Подробнее»
EFFECTS       = E("EFFECTS", "1") == "1"         # анимация 🎉/👍 (только личный чат)
EFF_PARTY, EFF_LIKE = "5046509860389126442", "5107584321108051014"
# --- сторож и ошибки ---
HEARTBEAT_URL = E("HEARTBEAT_URL", "")             # пинг внешнему сторожу (healthchecks.io и т.п.)
HEARTBEAT_SEC = int(E("HEARTBEAT_SEC", "60"))
FAULT_EVERY   = int(E("FAULT_EVERY", "3"))         # проверять ошибки каждые N опросов (0 — выкл.)
FAULT_IGNORE  = {x.strip().upper() for x in E("FAULT_IGNORE", "F13").split(",") if x.strip()}
# --- нагрузка и история ---
INVERTER_W    = int(E("INVERTER_W", "5000"))
OVERLOAD_W    = int(E("OVERLOAD_W", "4000"))         # нагрузка от батареи, при которой предупреждать
MAIN_A        = float(E("MAIN_BREAKER_A") or 0)      # вводной автомат квартиры, А: «из 25» в сумме (0 — не показывать)
HISTORY_DB    = os.path.join(os.path.dirname(STATE_FILE) or ".", "history.db")
# --- тариф, грн/кВт·ч ---
TARIFF_DAY    = float(E("TARIFF_DAY", "4.32"))
TARIFF_NIGHT  = float(E("TARIFF_NIGHT", "2.16"))
NIGHT_FROM, NIGHT_TO = int(E("NIGHT_FROM", "23")), int(E("NIGHT_TO", "7"))
CALIB_DAYS    = int(E("CALIB_DAYS", "14"))           # в «Батарея»: «пора калибровать», если 100% не было дольше
# --- бойлер на умной розетке Tuya (пусто — выключено) ---
BOILER_ID      = E("BOILER_ID", "")                  # id розетки из devices.json мастера tinytuya
BOILER_IP      = E("BOILER_IP", "") or None          # пусто — найти в сети автоматически
BOILER_VERSION = float(E("BOILER_VERSION", "3.3"))
BOILER_KEYFILE = E("BOILER_KEYFILE", "devices.json")  # локальный ключ берётся отсюда, в .env его нет
BOILER_CONTROL = E("BOILER_CONTROL", "1") == "1"     # 0 — только учёт, бот сам розетку не переключает
BOILER_POWER_DP = E("BOILER_POWER_DP", "19")         # поле мощности (0,1 Вт): обычно 19, у T34-Smart Plug+ — 22

logging.Formatter.converter = lambda *a: datetime.now(TZ).timetuple()   # время в логах — киевское
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deye-tg")
LOG_BUF = deque(maxlen=300)                      # запасной источник для /logs, если journalctl недоступен

class _BufHandler(logging.Handler):
    def emit(self, r): LOG_BUF.append(self.format(r))
_bh = _BufHandler(); _bh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_bh)
RESTART_FLAG = os.path.join(os.path.dirname(STATE_FILE) or ".", "restart.flag")

def git_version():
    """Короткий хеш и дата текущего коммита; «изменён», если есть незакоммиченные правки."""
    d = os.path.dirname(os.path.abspath(__file__))
    try:
        run = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, timeout=5).stdout.strip()
        h, ts = run("log", "-1", "--format=%h %ct").split()
        dirty = run("status", "--porcelain", "--untracked-files=no")
        return f"{h} · {datetime.fromtimestamp(int(ts), TZ):%d.%m}" + (" · изменён" if dirty else "")
    except Exception:
        return None
VERSION = git_version()

# ================= инвертор =================
inv = None
inv_lock = threading.Lock()                       # read() зовут main-loop и обработчик кнопок
live = {"d": None, "t": 0, "avg": 0.0}            # последнее чтение для кнопок

def s16(x): return x - 0x10000 if x & 0x8000 else x

def read():
    """Один запрос: регистры 150..194 (карта Deye SG0*LP1)."""
    global inv
    for attempt in (1, 2):                        # при сбое — переподключиться и повторить сразу
        try:
            if inv is None:
                inv = PySolarmanV5(LOGGER_IP, LOGGER_SN, port=8899, mb_slave_id=1, socket_timeout=6)
            r = inv.read_holding_registers(150, 45)
            break
        except Exception:
            try: inv and inv.disconnect()
            except Exception: pass
            inv = None
            if attempt == 2: raise
    if not (0 <= r[34] <= 100 and 4000 <= r[33] <= 6200 and r[0] <= 3000):
        raise ValueError(f"неправдоподобное чтение: soc={r[34]} batt_v={r[33] / 100} grid_v={r[0] / 10}")
    return {
        "grid_v": r[0] / 10,                  # 150 напряжение сети
        "load_w": s16(r[28]),                 # 178 нагрузка (иногда отдаёт 0)
        "grid_w": s16(r[19]),                 # 169 мощность из сети через инвертор (+ — берём из сети)
        "batt_v": r[33] / 100,                # 183
        "soc":    r[34],                      # 184
        "pv_w":   r[36] + r[37],              # 186, 187
        "batt_w": BATT_SIGN * s16(r[40]),     # 190: >0 разряд, <0 заряд
        "batt_a": BATT_SIGN * s16(r[41]) / 100,  # 191 ток
        "batt_t": (r[32] - 1000) / 10,        # 182 температура
    }

def read_locked():
    with inv_lock:
        return read()

def close_inverter(*_):
    """Закрыть соединение с логгером: он держит одно подключение, брошенное зависает на минуты."""
    try:
        if inv: inv.disconnect()
    except Exception:
        pass

def _on_sigterm(*_):
    close_inverter(); os._exit(0)

# ================= бойлер (Tuya) =================
_boiler = None
boiler_lock = threading.Lock()

def _boiler_dev():
    global _boiler
    if _boiler is None:
        import tinytuya
        with open(BOILER_KEYFILE) as f:
            key = next(d["key"] for d in json.load(f) if d.get("id") == BOILER_ID)
        _boiler = tinytuya.OutletDevice(BOILER_ID, BOILER_IP, key, version=BOILER_VERSION,
                                        connection_timeout=3, connection_retry_limit=2, connection_retry_delay=1)
    return _boiler

_cloud_at = 0                                         # когда последний раз обновляли устройства из облака

def boiler_cloud(on=None):
    """Запасной путь через облако Tuya, если розетка не отвечает в локальной сети (другая точка Wi-Fi и т. п.).
    on=None — прочитать (включён, Вт); True/False — переключить. None — облака нет."""
    global _cloud_at
    if not os.path.exists(TUYA_TOKEN): return None
    try:
        with tuya_lock:
            m = tuya_mgr()
            if on is not None:
                m.send_commands(BOILER_ID, [{"code": "switch_1", "value": bool(on)}])
                _cloud_at = 0; return None
            if BOILER_ID not in m.device_map or time.time() - _cloud_at > (300 if m.mq else 60):
                m.update_device_cache(); _cloud_at = time.time()     # между обновлениями статус свежий по MQTT
            dev = m.device_map[BOILER_ID]
            if not dev.online: return None                            # без питания облако отдаёт старое состояние
            st = dev.status
        p = st.get("cur_power")
        return bool(st.get("switch_1")), (p / 10 if isinstance(p, (int, float)) else None)
    except Exception as e:
        log.warning("boiler cloud: %s %s", type(e).__name__, e)
        return None

def boiler_state():
    """(включён, мощность Вт | None) или None, если розетка не отвечает ни локально, ни через облако."""
    if not BOILER_ID: return None
    global _boiler
    with boiler_lock:
        try:
            st = _boiler_dev().status()
            dps = st.get("dps") or {}
            if "Error" in st or "1" not in dps: raise RuntimeError(st.get("Error", "нет dps"))
            p = dps.get(BOILER_POWER_DP)                              # мощность в 0,1 Вт
            if live.get("boiler_src") == "cloud": log.info("бойлер снова отвечает в локальной сети")
            live["boiler_src"] = "local"
            return bool(dps["1"]), (p / 10 if isinstance(p, (int, float)) else None)
        except Exception as e:
            if live.get("boiler_src", "local") == "local": log.warning("boiler: %s %s", type(e).__name__, e)
            _boiler = None
    c = boiler_cloud()
    if c is not None:
        if live.get("boiler_src") != "cloud": log.warning("бойлер: локально не отвечает, работаю через облако Tuya")
        live["boiler_src"] = "cloud"
    else:
        if live.get("boiler_src") is not None: log.warning("бойлер: розетка не отвечает (нет питания или Wi-Fi)")
        live["boiler_src"] = None
    return c

def boiler_set(on):
    """Включить/выключить бойлер с проверкой. True — розетка подтвердила."""
    if not BOILER_ID: return None
    global _boiler
    for i in range(3):
        with boiler_lock:
            try:
                dev = _boiler_dev()
                r = dev.turn_on() if on else dev.turn_off()
                if isinstance(r, dict) and "Error" in r: raise RuntimeError(r["Error"])
            except Exception as e:
                log.warning("boiler: %s %s", type(e).__name__, e); _boiler = None
                boiler_cloud(on)                                      # локально не вышло — через облако
                time.sleep(2)
        s = boiler_state()
        if s and s[0] == on:
            log.info("бойлер %s", "включён" if on else "выключен"); live["boiler"] = s
            live.pop("boiler_want", None)
            return True
        if s is None and i == 0 and not on:
            return "nopower"                                          # света нет — розетка обесточена, это и есть «выкл»
        time.sleep(1)
    if s is None:                                                     # розетка ещё загружается — включит boiler_loop
        live["boiler_want"] = on
        return "pending"
    return False

def boiler_off_text():
    """Как назвать молчащую розетку: света нет — значит, обесточена (бойлер подключён мимо инвертора)."""
    d = live.get("d")
    return "обесточен — света нет" if d and d["grid_v"] < GRID_V_MIN else "нет связи с розеткой"

def boiler_line(ok, on):
    if ok is None: return []
    if ok == "nopower": return ["🔥 Бойлер обесточен вместе со светом"]
    if ok == "pending": return [f"🔥 Бойлер {'включится' if on else 'выключится'}, когда розетка выйдет на связь"]
    if ok: return [f"🔥 Бойлер {'включён' if on else 'выключен'}"]
    return [f"⚠️ <b>Бойлер не {'включился' if on else 'отключился'}</b> — проверь розетку"]

STATES = {0: "Ожидание", 1: "Самотест", 2: "Норма", 3: "Предупреждение", 4: "Ошибка"}
# код: (что значит, что делать) — по таблице ошибок мануала Deye SUN-3-6K-SG06LP1-EU-CM2 (05.2026)
RESTART = "выключить DC и AC на минуту и включить снова"
FAULT_NAMES = {
    1:  ("обратная полярность PV", "проверь полярность подключения панелей"),
    7:  ("сбой мягкого старта DC/DC", f"обычно уходит сам; если нет — {RESTART}"),
    8:  ("сбой реле GFDI", "проверь заземление N на выходе backup"),
    10: ("сбой вспомогательного питания", RESTART),
    13: ("смена режима работы", "обычно уходит сам"),
    18: ("аппаратная перегрузка по току AC", "проверь, что нагрузка в пределах мощности инвертора"),
    19: ("аппаратная защита (Tz_Integ)", RESTART),
    20: ("аппаратная перегрузка по току DC", "мощный прибор при пуске от батареи — уменьши нагрузку"),
    22: ("аварийная остановка", "обратись к установщику"),
    23: ("утечка тока AC", "проверь заземление; если не уходит — перезапуск 2–3 раза"),
    24: ("низкое сопротивление изоляции PV", "проверь кабели панелей и заземление PE"),
    26: ("разбаланс DC-шины", "подожди; если не уходит — перезапуск"),
    29: ("сбой связи параллельной работы", "проверь кабель параллели и адреса инверторов"),
    34: ("перегрузка по току AC", "уменьши нагрузку на выходе backup"),
    35: ("нет сети AC", "свет пропал или выключен автомат сети"),
    41: ("остановка параллельной системы", "один из инверторов в параллели выключен"),
    42: ("низкое напряжение сети", "напряжение сети вне нормы или плохой контакт кабеля AC"),
    47: ("частота сети выше нормы", "проблема сети или плохой контакт кабеля AC"),
    48: ("частота сети ниже нормы", "проблема сети или плохой контакт кабеля AC"),
    56: ("низкое напряжение DC-шины — батарея разряжена", "заряди от сети; обычно уходит сам, когда пошёл заряд"),
    58: ("нет связи с BMS батареи", "проверь кабель связи инвертор ↔ батарея"),
    63: ("дуговой пробой (ARC)", "проверь кабели панелей"),
    64: ("перегрев радиатора", "проверь вентиляцию; выключить на 10 мин"),
}
ALARM_NAMES = {1: ("сбой вентилятора", "проверь, крутится ли вентилятор и не забит ли он пылью"),
               2: ("ошибка фазы сети", "проверь подключение сети"),
               3: ("нет связи со счётчиком", "проверь кабель счётчика"),
               30: ("потеря связи с батареей", "проверь кабель связи инвертор ↔ батарея"),
               31: ("связь параллельной работы", "проверь кабель параллели")}

def read_faults():
    """Состояние, ошибки F01–F64 (рег. 103–106), предупреждения (101–102), BMS (322–323), t° инвертора (90–91)."""
    with inv_lock:
        if inv is None: return None
        a = inv.read_holding_registers(59, 48)                     # 59..106
        try: b = inv.read_holding_registers(322, 2)                # 322..323
        except Exception: b = [0, 0]
    alarm = a[42] | a[43] << 16
    fault = a[44] | a[45] << 16 | a[46] << 32 | a[47] << 48
    items = [f"F{i + 1:02d}" for i in range(64) if fault >> i & 1 and f"F{i + 1:02d}" not in FAULT_IGNORE]
    items += [f"W{i}" for i in range(32) if alarm >> i & 1]
    if b[0]: items.append(f"BA:{b[0]:#06x}")
    if b[1]: items.append(f"BF:{b[1]:#06x}")
    energy = {"chg": a[11] / 10, "dis": a[12] / 10, "imp": a[17] / 10, "load": a[25] / 10,   # рег. 70, 71, 76, 84
              "tdis": (a[15] | a[16] << 16) / 10}                                         # рег. 74–75: за всё время
    return a[0], items, ((a[31] - 1000) / 10, (a[32] - 1000) / 10), energy

# ================= время / формат =================
def now(): return datetime.fromtimestamp(time.time(), TZ)

def hm(t):
    dt, today = datetime.fromtimestamp(t, TZ), now().date()
    if dt.date() == today: return f"{dt:%H:%M}"
    if dt.date() == today + timedelta(days=1): return f"завтра {dt:%H:%M}"
    return f"{dt:%d.%m %H:%M}"

def dur(hours):
    m = max(0, int(round(hours * 60)))
    return f"{m // 60} ч {m % 60:02d} мин" if m >= 60 else f"{m} мин"

def tt(t, fmt=None):
    """<tg-time>: клиент покажет время в своём формате, внутри — fallback для старых клиентов."""
    if fmt is None:
        fmt = "t" if datetime.fromtimestamp(t, TZ).date() == now().date() else "wt"
    return f'<tg-time unix="{int(t)}" format="{fmt}">{hm(t)}</tg-time>'

def rel(t):
    """Относительное время («через 2 часа») — считает клиент Telegram."""
    return f'<tg-time unix="{int(t)}" format="r">через {dur((t - time.time()) / 3600)}</tg-time>'

def tts(t):
    return f'<tg-time unix="{int(t)}" format="T">{datetime.fromtimestamp(t, TZ):%H:%M:%S}</tg-time>'

def bar(soc, n=10):
    """Шкала заряда цветными квадратами: 🟩 от 60%, 🟨 30–59%, 🟥 ниже 30%."""
    k = max(0, min(n, round(soc / 100 * n)))
    fill = "🟩" if soc >= 60 else "🟨" if soc >= 30 else "🟥"
    return fill * k + "⬜" * (n - k)

def quote(lines): return "<blockquote>" + "\n".join(lines) + "</blockquote>"
def fold(lines): return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"

def num(x, d=1): return f"{x:.{d}f}".replace(".", ",")
def pw(w): return f"{w:.0f} Вт" if abs(w) < 1000 else f"{num(w / 1000).removesuffix(',0')} кВт"
def volts(v): return f"{num(v)} В"

def batt_str(w):
    return f"заряд {pw(-w)}" if w < -30 else f"разряд {pw(w)}" if w > 30 else "простой"

def batt_line(d):
    return f"🔋 <b>{d['soc']}%</b> {bar(d['soc'])}"

def amps(d): return f"{num(abs(d['batt_a']))} А · " if "batt_a" in d else ""

def flow_line(d):
    w, v = d["batt_w"], volts(d["batt_v"])
    if w < -30: return f"🔌 Заряд <b>{pw(-w)}</b> · {amps(d)}{v}"
    if w > 30: return f"🔻 Разряд <b>{pw(w)}</b> · {amps(d)}{v}"
    return f"⏸ Батарея в простое · {v}"

# ================= ЯСНО =================
sched = {"events": [], "status": None, "ok_at": 0}   # events: [[start_ts, end_ts], ...]
yasno_next_try = 0

def yasno_get():
    req = urllib.request.Request(YASNO_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)

def at_min(day, minutes):
    d, m = divmod(int(minutes), 1440)
    return datetime.combine(day + timedelta(days=d), dtime(m // 60, m % 60), tzinfo=TZ).timestamp()

def merge(slots):
    out = []
    for a, b in sorted(slots):
        if out and a <= out[-1][1]: out[-1][1] = max(b, out[-1][1])
        else: out.append([a, b])
    return out

def yasno_refresh(force=False):
    """Обновляет график. Возвращает (было, стало) в минутах, если изменился график на сегодня."""
    global yasno_next_try
    if not YASNO_GROUP or (not force and time.time() < yasno_next_try): return None
    yasno_next_try = time.time() + YASNO_EVERY
    try:
        data = yasno_get()
        if YASNO_GROUP not in data:
            log.error("yasno: группы %s нет, есть: %s", YASNO_GROUP, ", ".join(sorted(data))); return
        g, ev = data[YASNO_GROUP], []
        for key in ("today", "tomorrow"):
            day = g.get(key) or {}
            if "date" not in day: continue
            base = datetime.fromisoformat(day["date"]).date()
            ev += [(at_min(base, s["start"]), at_min(base, s["end"]))
                   for s in day.get("slots", []) if s.get("type") == "Definite"]
        merged = []
        for a, b in sorted(ev):                       # склеиваем 22–24 + 00–02 в одно окно
            if merged and a <= merged[-1][1]: merged[-1][1] = max(b, merged[-1][1])
            else: merged.append([a, b])
        today = g.get("today") or {}
        slots = merge([s["start"], s["end"]] for s in today.get("slots", []) if s.get("type") == "Definite")
        prev, changed = sched.get("today"), None
        if prev and prev.get("date") == today.get("date") and prev.get("slots") != slots:
            changed = (prev.get("slots") or [], slots)
        sched.update(events=merged, status=today.get("status"), ok_at=time.time(),
                     today={"date": today.get("date"), "slots": slots})
        with open(YASNO_CACHE, "w") as f: json.dump(sched, f)
        return changed
    except Exception as e:
        log.warning("yasno: %s", e)
        yasno_next_try = time.time() + 120
    return None

def next_outage(t):
    return next((e for e in sched["events"] if e[0] > t), None)

def current_outage(t, tol=1800):                       # ±30 мин: отключают не секунда в секунду
    return next((e for e in sched["events"] if e[0] - tol <= t < e[1]), None)

def notes():
    out = []
    if sched["status"] == "EmergencyShutdowns":
        out.append("<i>⚠️ ЯСНО: аварийные отключения — график может не действовать</i>")
    if YASNO_GROUP and time.time() - sched["ok_at"] > 6 * 3600:
        out.append("<i>⚠️ График не обновлялся больше 6 ч</i>")
    return out

def verdict(ok, title, note=None):
    icon = "✅" if ok is True else "⚠️" if ok is None else "❌"
    return [f"{icon} <b>{title}</b>" + (f" · <i>{note}</i>" if note else "")]

def cal(): return f'<a href="{YASNO_LINK}">📅</a>'    # 📅 — ссылка на график ЯСНО

def next_line(t):
    n = next_outage(t)
    if not n: return None, f"{cal()} <i>По графику отключений больше нет</i>"
    return n, f"{cal()} Отключение <b>{tt(n[0])}–{tt(n[1], 't')}</b> · {rel(n[0])}"

def charge_forecast(soc, charge_w):
    """(строки прогноза, вердикт): время до 100% и успеет ли до отключения."""
    t = time.time()
    h = BATT_WH * (100 - soc) / 100 / charge_w
    t_full = t + h * 3600
    fc, verd = [f"⏱ До 100%: <b>{dur(h)}</b> · к {tt(t_full)}"], []
    if not YASNO_GROUP: return fc, verd
    n, line = next_line(t)
    fc.append(line)
    if n:
        margin = (n[0] - t_full) / 60
        if margin >= MARGIN_MIN:
            verd = verdict(True, "Успеет зарядиться", f"запас <u>{dur(margin / 60)}</u>")
        elif margin >= 0:
            verd = verdict(None, "Впритык", "последние % заряжаются медленнее")
        else:
            soc_at = min(100, soc + charge_w * (n[0] - t) / 3600 / BATT_WH * 100)
            verd = verdict(False, "Не успеет", f"к {tt(n[0])} будет ≈ <u>{soc_at:.0f}%</u>")
    return fc, verd + notes()

def outage_forecast(soc, disch_w):
    """(строки прогноза, вердикт): на сколько хватит и дотянет ли до включения."""
    t, fc, verd = time.time(), [], []
    if disch_w >= 50:
        h = BATT_WH * max(soc - MIN_SOC, 0) / 100 / disch_w
        fc.append(f"⏳ Хватит на <b>{dur(h)}</b> · до {MIN_SOC}% при {pw(disch_w)}")
    if not YASNO_GROUP: return fc, verd
    cur = current_outage(t)
    if not cur: return fc + [f"{cal()} <i>Отключение не по графику</i>"], notes()
    fc.append(f"{cal()} Свет по графику в <b>{tt(cur[1])}</b> · {rel(cur[1])}")
    if disch_w >= 50:
        t_empty = t + h * 3600
        verd = (verdict(True, "Батареи хватит") if t_empty >= cur[1]
                else verdict(False, "Не хватит", f"до {MIN_SOC}% сядет ≈ в <u>{tt(t_empty)}</u>"))
    return fc, verd + notes()

def day_windows(day):
    """Окна отключений, пересекающие сутки day, как [(start_ts, end_ts)]."""
    a = datetime.combine(day, dtime(0), tzinfo=TZ).timestamp()
    b = datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=TZ).timestamp()
    return [(max(s, a), min(e, b)) for s, e in sched["events"] if s < b and e > a]

def timeline():
    """Окна отключений текстом: прошедшие зачёркнуты, текущее или ближайшее — жирным."""
    if not (SHOW_TIMELINE and YASNO_GROUP and sched["events"]): return []
    t, out = time.time(), []
    nxt = next((e for e in sched["events"] if e[1] > t), None)
    for d, label in ((now().date(), "Сегодня"), (now().date() + timedelta(days=1), "Завтра")):
        parts = []
        for s, e in day_windows(d):
            txt = f"{datetime.fromtimestamp(s, TZ):%H:%M}–{datetime.fromtimestamp(e, TZ):%H:%M}"
            if e <= t: txt = f"<s>{txt}</s>"
            elif nxt and s < nxt[1] and e > nxt[0]: txt = f"<b>{txt}</b>"
            parts.append(txt)
        if parts: out.append(f"🗓 {label} без света: " + ", ".join(parts))
    return out

def kw(w): return f"{num(w / 1000)} кВт"

def grid_draw(d):
    """(заряд, бойлер, квартира, всего Вт, всего А) — сколько берётся из сети через вводной автомат.
    Через инвертор — по мощности сети (рег. 169): она точнее нагрузки и включает сам инвертор."""
    b = live.get("boiler") if BOILER_ID else None
    bw = b[1] if b and b[0] and b[1] and b[1] >= 30 else 0
    chg = -d["batt_w"] if d["batt_w"] < -30 else 0
    if d.get("grid_w", 0) > 0:
        inv_w = d["grid_w"]
        apt = max(0, inv_w - chg)
    else:                                                       # старые данные / сеть не через инвертор
        apt = max(0, d["load_w"] - (d.get("pv_w") or 0))
        inv_w = apt + chg
    total = inv_w + bw
    return chg, bw, apt, total, total / max(d["grid_v"], 1)

def power_lines(d):
    """Бойлер (мимо инвертора, мощность с розетки) и сумма из сети: заряд + бойлер + квартира, кВт и А."""
    out = []
    if BOILER_ID:
        b = live.get("boiler")
        if not b: out.append(f"🔥 Бойлер: {boiler_off_text()}")
        elif not b[0]: out.append("🔥 Бойлер выключен")
        elif b[1] is None: out.append("🔥 Бойлер включён")
        elif b[1] < 30: out.append("🔥 Бойлер включён · не греет")
        else: out.append(f"🔥 Бойлер <b>{pw(b[1])}</b>")
    chg, bw, apt, total, amps = grid_draw(d)
    b = live.get("boiler")
    bpart = None if not BOILER_ID else "бойлер ?" if not b else f"бойлер {kw(bw)}" if bw else None
    parts = ([f"заряд {kw(chg)}"] if chg else []) + ([bpart] if bpart else []) + [f"квартира {kw(apt)}"]
    out.append("🧮 " + " + ".join(parts) + f" = <b>{kw(total)} · {amps:.1f} А</b>".replace(".", ",") +
               (f" из {MAIN_A:.0f}" if MAIN_A else ""))
    return out

def pi_line():
    """Raspberry Pi: температура процессора и свободное место на карте."""
    parts = []
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f: t = int(f.read()) / 1000
        parts.append(f"{num(t)} °C" + (" 🔥" if t >= 75 else ""))
    except Exception: pass
    try:
        import shutil
        u = shutil.disk_usage("/")
        free = u.free / 2**30
        parts.append(f"свободно {num(free, 0 if free >= 10 else 1)} из {u.total / 2**30:.0f} ГБ" + (" ⚠️" if u.free / u.total < 0.1 else ""))
    except Exception: pass
    return "🍓 Pi: " + " · ".join(parts) if parts else None

def details(d, load=False, pv=False, boiler=True):
    """Свёрнутый блок «Подробнее» (раскрывается по тапу)."""
    if not SHOW_DETAILS: return []
    lines = ["ℹ️ <b>Подробнее</b>"]
    if load: lines.append(f"🏠 Нагрузка {pw(d['load_w'])}")
    if pv and d.get("pv_w"): lines.append(f"☀️ PV {pw(d['pv_w'])}")
    if "batt_t" in d:
        valid = [t for t in live.get("temps") or () if -20 < t < 120]    # у SG06LP1 датчик DC отдаёт мусор
        inv_t = max(valid) if valid else None
        lines.append(f"🌡 Батарея {num(d['batt_t'])} °C" + (f" · инвертор {num(inv_t)} °C" if inv_t is not None else ""))
    if BOILER_ID and boiler:
        b = live.get("boiler")
        lines.append(f"🔥 Бойлер: {boiler_off_text()}" if not b else
                     f"🔥 Бойлер: {'вкл' if b[0] else 'выкл'}" + (f" · {pw(b[1])}" if b[0] and b[1] else ""))
    pi = pi_line()
    if pi: lines.append(pi)
    if VERSION: lines.append(f"🏷 Версия {VERSION}")
    lines.append(f"🕓 Данные на {tts(time.time())}")
    return [fold(lines)]

def assemble(head, card, fc, verd, d, **det):
    return "\n".join(head + [quote(card)] + fc + verd + timeline() + details(d, **det))

# ================= история =================
def db():
    c = sqlite3.connect(HISTORY_DB, timeout=10)
    c.execute("create table if not exists samples (ts integer primary key, soc integer, grid integer, "
              "load_w integer, batt_w integer, chg real, dis real, imp real, eload real)")
    c.execute("create table if not exists boiler (ts integer primary key, is_on integer, w real)")
    c.execute("create table if not exists climate (ts integer, name text, t real, h integer, primary key (ts, name))")
    return c

def boiler_add(b):
    """Отметка бойлера (каждые ~30 с): включена ли розетка и мощность, Вт."""
    with closing(db()) as c, c:
        c.execute("insert or replace into boiler values (?,?,?)", (int(time.time()), int(b[0]), b[1] or 0))

def history_add(d, grid):
    e = live.get("energy") or {}
    with closing(db()) as c, c:
        c.execute("insert or replace into samples values (?,?,?,?,?,?,?,?,?)",
                  (int(time.time()), d["soc"], int(grid), d["load_w"], d["batt_w"],
                   e.get("chg"), e.get("dis"), e.get("imp"), e.get("load")))

def kwh(x): return f"{num(x)} кВт·ч"
def grn(x): return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " грн"

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MON = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

def is_night(ts):
    h = datetime.fromtimestamp(ts, TZ).hour
    return h >= NIGHT_FROM or h < NIGHT_TO

def grid_cost(rows):
    """(кВт·ч днём, кВт·ч ночью) по приращениям дневного счётчика «из сети» (обнуляется в полночь)."""
    day = night = 0.0; prev = None
    for ts, v in rows:
        if v is None: continue
        if prev is not None:
            dv = v - prev if v >= prev else v
            if is_night(ts): night += dv
            else: day += dv
        prev = v
    return day, night

def report(period):
    """Отчёт: period = "day" | "week" | "month". Из истории и дневных счётчиков Deye."""
    today = now().date()
    start_day = {"day": today, "week": today - timedelta(days=6), "month": today.replace(day=1)}[period]
    start = datetime.combine(start_day, dtime(0), tzinfo=TZ).timestamp()
    with closing(db()) as c:
        rows = c.execute("select ts, soc, grid, chg, dis, imp, eload from samples where ts >= ? order by ts",
                         (start,)).fetchall()
    title = {"day": "сегодня", "week": f"7 дней · {start_day:%d.%m}–{today:%d.%m}",
             "month": f"{MONTHS[today.month - 1]} {today.year}"}[period]
    if not rows: return f"📈 <b>Отчёт за {title}</b>\n<i>Данных пока нет</i>"
    off_min = sum(1 for r in rows if r[2] == 0)
    outages = sum(1 for p, r in zip(rows, rows[1:]) if p[2] == 1 and r[2] == 0)
    per_day = {}
    for ts, _, _, *vals in rows:
        p = per_day.setdefault(datetime.fromtimestamp(ts, TZ).date(), [0.0] * 4)
        for i, v in enumerate(vals):
            if v is not None: p[i] = max(p[i], v)
    chg, dis, imp, eload = (sum(p[i] for p in per_day.values()) for i in range(4))
    kd, kn = grid_cost([(r[0], r[5]) for r in rows])
    total = kd * TARIFF_DAY + kn * TARIFF_NIGHT
    card = [f"🔴 Без света: <b>{dur(off_min / 60)}</b> · отключений: {outages}",
            f"🪫 Минимальный заряд: <b>{min(r[1] for r in rows)}%</b>",
            f"⚡ Взято из сети: <b>{kwh(imp)}</b>",
            f"🔋 Батарея: заряд <b>{kwh(chg)}</b> · разряд <b>{kwh(dis)}</b>",
            f"🏠 Потребление: <b>{kwh(eload)}</b>"]
    money = [f"💸 Стоимость: <b>{grn(total)}</b> · день {grn(kd * TARIFF_DAY)} · ночь {grn(kn * TARIFF_NIGHT)}"]
    if period != "day":
        avg = total / max(1, len(per_day))
        money.append(f"📊 В среднем <b>{grn(avg)}</b> в день")
        if period == "month":
            money.append(f"📆 Прогноз на месяц: <b>≈ {grn(avg * calendar.monthrange(today.year, today.month)[1])}</b>")
    out = [f"📈 <b>Отчёт за {title}</b>", quote(card), quote(money)]
    if rows[0][0] > start + 3600:
        out.append(f"<i>Данные есть с {datetime.fromtimestamp(rows[0][0], TZ):%H:%M %d.%m}</i>")
    out.append("<i>Учтена только энергия, прошедшая через инвертор</i>")
    return "\n".join(out)

def discharge_sessions(rows):
    """Отключения без дыр в данных: SOC в начале и конце и энергия, отданная батареей (рег. 71)."""
    out, cur, prev_ts = [], None, None
    for ts, soc, g, dis in rows:
        if cur and (g != 0 or ts - prev_ts > 300):
            out.append(cur); cur = None
        if g == 0:
            if cur is None:
                cur = {"t": ts, "soc0": soc, "soc1": soc, "e": 0.0, "last": dis}
            else:
                if dis is not None and cur["last"] is not None:
                    cur["e"] += dis - cur["last"] if dis >= cur["last"] else dis
                if dis is not None: cur["last"] = dis
                cur["soc1"] = soc
        prev_ts = ts
    return out + ([cur] if cur else [])

def last_charge():
    """Последний заряд от сети: (с какого времени и %, до 100% или до отключения, за сколько)."""
    with closing(db()) as c:
        rows = c.execute("select ts, soc, grid from samples where ts >= ? order by ts",
                         (time.time() - 14 * 86400,)).fetchall()
    sessions, cur = [], None
    for ts, soc, g in rows:
        if g == 1:
            if cur is None: cur = {"t0": ts, "s0": soc, "full": None, "end": None}
            cur["t1"], cur["s1"] = ts, soc
            if soc >= 100 and cur["full"] is None: cur["full"] = ts
        elif cur:
            cur["end"] = ts; sessions.append(cur); cur = None
    if cur: sessions.append(cur)
    s = next((x for x in reversed(sessions) if x["s0"] < 99), None)   # периоды, где батарея была полной, — не заряд
    if not s: return None
    start = f"с {hm(s['t0'])} с {s['s0']}%"
    if s["full"]:
        return f"⚡ Зарядилась {start} до <b>100%</b> в {hm(s['full'])} · за <b>{dur((s['full'] - s['t0']) / 3600)}</b>"
    if s["end"]:
        return (f"⚡ Заряжалась {start} до <b>{s['s1']}%</b> · свет пропал в {hm(s['end'])}"
                f" · за <b>{dur((s['end'] - s['t0']) / 3600)}</b>")
    return f"⚡ Заряжается {start}, сейчас <b>{s['s1']}%</b> · уже {dur((s['t1'] - s['t0']) / 3600)}"

def battery_text():
    """Здоровье батареи: циклы, реальная ёмкость по разрядам ≥ 20%, динамика по месяцам."""
    nominal = BATT_WH / 1000
    with closing(db()) as c:
        rows = c.execute("select ts, soc, grid, dis from samples where ts >= ? order by ts",
                         (time.time() - 366 * 86400,)).fetchall()
    est = [(s["t"], s["e"] / (s["soc0"] - s["soc1"]) * 100) for s in discharge_sessions(rows)
           if s["soc0"] - s["soc1"] >= 20 and s["e"] > 0]
    e, d = live.get("energy") or {}, live.get("d") or {}
    if e.get("tdis"):
        cyc = f"🔄 Циклов: <b>{num(e['tdis'] / nominal)}</b> · отдано всего {kwh(e['tdis'])}"
    else:
        per_day = {}
        for ts, _, _, v in rows:
            if v is not None:
                k = datetime.fromtimestamp(ts, TZ).date(); per_day[k] = max(per_day.get(k, 0), v)
        cyc = f"🔄 Циклов: <b>{num(sum(per_day.values()) / nominal)}</b> · по истории бота"
    card = [cyc]
    try:
        lc = last_charge()
        if lc: card.insert(0, lc)
    except Exception as ex:
        log.warning("last_charge: %s", ex)
    if est:
        recent = [x for t, x in est if t > time.time() - 30 * 86400] or [x for _, x in est[-5:]]
        cap = mean(recent)
        card.append(f"📏 Реальная ёмкость: <b>{num(cap, 2)} кВт·ч</b> · {cap / nominal * 100:.0f}% от {num(nominal, 2)}")
    else:
        card.append("📏 Реальная ёмкость: <i>появится после отключения с разрядом от 20%</i>")
    if "batt_t" in d: card.append(f"🌡 Температура: {num(d['batt_t'])} °C")
    late = live.get("full_at") and CALIB_DAYS and time.time() - live["full_at"] > CALIB_DAYS * 86400
    if live.get("full_at"):
        card.append(f"🎯 Полный заряд: {ago(live['full_at'])}" + (" · <b>пора калибровать</b>" if late else ""))
    out = [f"🔋 <b>Батарея</b> · {tts(time.time())}", quote(card)]
    if late:
        out.append("<i>Зарядите до 100% и дайте постоять час-другой: BMS уточнит процент заряда</i>")
    months = {}
    for t, x in est:
        dt = datetime.fromtimestamp(t, TZ); months.setdefault((dt.year, dt.month), []).append(x)
    if len(months) > 1:
        out.append("📆 По месяцам: " + " · ".join(f"{MON[m - 1]} {num(mean(v), 2)}" for (y, m), v in sorted(months.items())[-6:]))
    if est: out.append(f"<i>Ёмкость считается по разрядам от 20% во время отключений · замеров: {len(est)}</i>")
    return "\n".join(out)

def grid_segments(rows):
    """[(start_ts, end_ts, grid)] — непрерывные участки; grid=None — дыра в данных (> 5 мин)."""
    segs, (s0, g0), prev = [], (rows[0][0], rows[0][2]), rows[0][0]
    for ts, _, g, _ in rows[1:]:
        if ts - prev > 300:
            segs += [(s0, prev, g0), (prev, ts, None)]; s0, g0 = ts, g
        elif g != g0:
            segs.append((s0, ts, g0)); s0, g0 = ts, g
        prev = ts
    return segs + [(s0, prev, g0)]

def make_graph(hours):
    """PNG: полоса «Сеть» (есть/нет света), заряд батареи и нагрузка. Возвращает (png | None, подпись)."""
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt, matplotlib.dates as mdates
        from matplotlib.patches import Patch
    except ImportError:
        return None, "⚠️ Для графиков на Pi нужна библиотека matplotlib"
    with closing(db()) as c:
        rows = c.execute("select ts, soc, grid, load_w from samples where ts >= ? order by ts",
                         (time.time() - hours * 3600,)).fetchall()
    if len(rows) < 2: return None, "📈 <i>Данных для графика пока мало</i>"
    dtt = lambda t: datetime.fromtimestamp(t, TZ)
    ts = [dtt(r[0]) for r in rows]
    soc, load = [r[1] for r in rows], [r[3] / 1000 for r in rows]
    bg, fg, gc = "#17212b", "#e6e9ed", "#2b3946"
    on_c, off_c, gap_c = "#4caf50", "#e5534b", "#3a4652"
    plt.rcParams.update({"font.size": 10, "text.color": fg, "axes.labelcolor": fg, "xtick.color": fg, "ytick.color": fg})
    fig, (a0, a1, a2) = plt.subplots(3, 1, figsize=(8, 5.2), dpi=160, sharex=True,
                                     gridspec_kw={"height_ratios": [0.55, 3, 1.3]})
    fig.patch.set_facecolor(bg)
    for ax in (a0, a1, a2):
        ax.set_facecolor(bg)
        for s in ax.spines.values(): s.set_visible(False)
    for ax in (a1, a2): ax.grid(color=gc, lw=0.6)
    span = mdates.date2num(ts[-1]) - mdates.date2num(ts[0])
    segs = grid_segments(rows)
    for s, e, g in segs:                                      # полоса «Сеть»
        x0, w = mdates.date2num(dtt(s)), mdates.date2num(dtt(e)) - mdates.date2num(dtt(s))
        a0.broken_barh([(x0, w)], (0, 1), color=on_c if g == 1 else off_c if g == 0 else gap_c)
        if g == 0:
            for ax in (a1, a2): ax.axvspan(dtt(s), dtt(e), color=off_c, alpha=0.16, lw=0)
            if hours <= 48 and w > span * 0.07:               # подпись времени отключения
                a0.text(x0 + w / 2, 0.5, f"{dtt(s):%H:%M}–{dtt(e):%H:%M}", ha="center", va="center",
                        fontsize=8, color="white", fontweight="bold")
    a0.set_ylim(0, 1); a0.set_yticks([]); a0.set_ylabel("Сеть", rotation=0, ha="right", va="center")
    a1.plot(ts, soc, color="#5fb15f", lw=2)
    a1.axhline(MIN_SOC, color="#e5a54b", lw=1, ls="--")
    a1.set_ylim(0, 100); a1.set_ylabel("Заряд, %")
    a2.fill_between(ts, load, color="#4a90d9", alpha=0.35, lw=0); a2.plot(ts, load, color="#4a90d9", lw=1)
    a2.set_ylim(0, max(1, max(load) * 1.2)); a2.set_ylabel("Нагрузка, кВт")
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if hours <= 48 else "%d.%m", tz=TZ))
    leg = [Patch(color=on_c, label="свет есть"), Patch(color=off_c, label="света нет")]
    if any(g is None for _, _, g in segs): leg.append(Patch(color=gap_c, label="нет данных"))
    leg.append(Patch(color="#e5a54b", label=f"порог {MIN_SOC}%"))
    fig.legend(handles=leg, loc="lower center", ncol=len(leg), frameon=False, fontsize=9)
    fig.subplots_adjust(left=0.11, right=0.98, top=0.98, bottom=0.13, hspace=0.12)
    buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=bg); plt.close(fig)
    off = sum(1 for r in rows if r[2] == 0)
    outs = sum(1 for p, r in zip(rows, rows[1:]) if p[2] == 1 and r[2] == 0)
    title = "Сутки" if hours <= 24 else f"{hours // 24} дней"
    cap = (f"📈 <b>{title}</b> · без света <b>{dur(off / 60)}</b> · отключений: {outs}"
           f" · мин. заряд <b>{min(soc)}%</b>")
    return buf.getvalue(), cap

# ================= бойлер: статистика и график =================
def boiler_rows(since, until=None):
    with closing(db()) as c:
        return c.execute("select ts, is_on, w from boiler where ts >= ? and ts < ? order by ts",
                         (since, until or 2**40)).fetchall()

def boiler_intervals(rows):
    """Интервалы между отметками (дыры > 5 мин пропускаем): (начало, сек, Вт)."""
    for (t0, _, w0), (t1, _, w1) in zip(rows, rows[1:]):
        if t1 - t0 <= 300:
            yield t0, t1 - t0, w0

def boiler_stats(rows):
    """(кВт·ч, грн, нагревов, часов нагрева)."""
    e = cost = heat = 0.0
    for t0, dt, w in boiler_intervals(rows):
        k = w * dt / 3600 / 1000
        e += k; cost += k * (TARIFF_NIGHT if is_night(t0) else TARIFF_DAY)
        if w >= 30: heat += dt
    starts, last_hot = 0, None                     # нагрев: пауза перед ним ≥ 3 мин, иначе это тот же нагрев
    for ts, _, w in rows:
        if w >= 30:
            if last_hot is None or ts - last_hot >= 180: starts += 1
            last_hot = ts
    return e, cost, starts, heat / 3600

def boiler_caption(title):
    today = now().date()
    periods = (("📅 Сегодня", today), ("🗓 7 дней", today - timedelta(days=6)), ("📆 Месяц", today.replace(day=1)))
    card = []
    for label, day in periods:
        rows = boiler_rows(datetime.combine(day, dtime(0), tzinfo=TZ).timestamp())
        e, cost, n, h = boiler_stats(rows)
        card.append(f"{label}: <b>{kwh(e)}</b> · {grn(cost)} · нагревов {n} · грел {dur(h)}")
    with closing(db()) as c: first = c.execute("select min(ts) from boiler").fetchone()[0]
    b = live.get("boiler")
    now_line = ("нет связи с розеткой" if not b else "выключен" if not b[0] else
                f"греет <b>{pw(b[1])}</b>" if b[1] and b[1] >= 30 else "включён, вода нагрета")
    out = [f"🔥 <b>Бойлер</b> · {title} · {tts(time.time())}", f"Сейчас: {now_line}", quote(card)]
    if first and first > time.time() - 31 * 86400:
        out.append(f"<i>Учёт ведётся с {datetime.fromtimestamp(first, TZ):%d.%m %H:%M}</i>")
    return "\n".join(out)

def boiler_buckets(start, n, step):
    """Расход по корзинам [start + i*step): (кВт·ч ночью, кВт·ч днём, грн) на каждую."""
    out = [[0.0, 0.0, 0.0] for _ in range(n)]
    for t0, dt, w in boiler_intervals(boiler_rows(start)):
        i = int((t0 - start) // step)
        if not 0 <= i < n: continue
        k, nt = w * dt / 3600 / 1000, is_night(t0)
        out[i][0 if nt else 1] += k
        out[i][2] += k * (TARIFF_NIGHT if nt else TARIFF_DAY)
    return out

def make_boiler_graph(period):
    """period: "d" — по часам за сегодня, "w"/"m" — по дням за 7 дней / месяц. (png | None, подпись)."""
    today = now().date()
    start_day = {"d": today, "w": today - timedelta(days=6), "m": today.replace(day=1)}[period]
    start = datetime.combine(start_day, dtime(0), tzinfo=TZ).timestamp()
    title = {"d": f"сегодня, {today.day} {MONTHS_GEN[today.month - 1]}",
             "w": f"7 дней · {start_day:%d.%m}–{today:%d.%m}", "m": f"{MONTHS[today.month - 1]} {today.year}"}[period]
    cap = boiler_caption(title)
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch, Patch
        from matplotlib.lines import Line2D
    except ImportError:
        return None, cap
    rows = boiler_rows(start)
    if len(rows) < 2: return None, cap + "\n<i>Для графика данных пока мало</i>"
    e, cost, starts, heat = boiler_stats(rows)
    BG, CARD, FG, SUB, MUT, GRID = "#17212b", "#202c38", "#eef1f4", "#9aa7b4", "#5f6d7a", "#26323e"
    NIGHT_C, DAY_C, OFF_C = "#6c9cf0", "#f59a4a", "#e5534b"
    plt.rcParams.update({"font.size": 10, "text.color": FG, "axes.labelcolor": SUB, "xtick.color": SUB, "ytick.color": MUT})
    fig = plt.figure(figsize=(8, 5.2), dpi=160); fig.patch.set_facecolor(BG)
    fig.text(0.035, 0.955, "Бойлер", fontsize=15, fontweight="bold", va="top")
    fig.text(0.152, 0.950, f"· {title}", fontsize=11.5, color=SUB, va="top")
    # плитки с итогами
    n_days = 1 if period == "y" else (today - start_day).days + 1
    tiles = [(grn(cost), "потрачено"), (kwh(e), "расход"), (str(starts), "нагревов"), (dur(heat), "грел")]
    if period != "d": tiles[3] = (grn(cost / n_days), "в среднем за день")
    for i, (v, lab) in enumerate(tiles):
        x = 0.035 + i * 0.2375
        fig.patches.append(FancyBboxPatch((x, 0.735), 0.2225, 0.135, boxstyle="round,pad=0,rounding_size=0.012",
                                          transform=fig.transFigure, fc=CARD, ec="none"))
        fig.text(x + 0.016, 0.835, v, fontsize=13.5 if i else 15, fontweight="bold", va="top",
                 color=DAY_C if i == 0 else FG)
        fig.text(x + 0.016, 0.762, lab, fontsize=9, color=SUB, va="bottom")
    ax = fig.add_axes([0.075, 0.16, 0.905, 0.52]); ax.set_facecolor(BG)
    for s in ax.spines.values(): s.set_visible(False)
    ax.grid(color=GRID, lw=0.8, axis="y"); ax.set_axisbelow(True); ax.tick_params(length=0)
    if period == "d":
        b = boiler_buckets(start, 24, 3600)
        nk, dk, money = [x[0] for x in b], [x[1] for x in b], [x[2] for x in b]
        tot = [a + c for a, c in zip(nk, dk)]
        top = max(0.5, max(tot) * 1.32)
        cur_h = now().hour
        for h0, h1 in ((0, NIGHT_TO), (NIGHT_FROM, 24)):                 # ночная зона
            ax.axvspan(h0 - 0.5, h1 - 0.5, color=NIGHT_C, alpha=0.07, lw=0)
        ax.text((NIGHT_TO - 1) / 2, top * 0.985, f"ночь · {num(TARIFF_NIGHT, 2)} грн", ha="center", va="top", fontsize=8, color=NIGHT_C)
        ax.text((NIGHT_TO + NIGHT_FROM - 1) / 2, top * 0.985, f"день · {num(TARIFF_DAY, 2)} грн за кВт·ч", ha="center", va="top", fontsize=8, color=DAY_C)
        with closing(db()) as c:                                         # отключения света
            srows = c.execute("select ts, soc, grid, load_w from samples where ts >= ? order by ts", (start,)).fetchall()
        if len(srows) > 1:
            for s, e2, g in grid_segments(srows):
                if g == 0 and e2 - s >= 300:
                    x0, x1 = (s - start) / 3600 - 0.5, (e2 - start) / 3600 - 0.5
                    ax.axvspan(x0, x1, color=OFF_C, alpha=0.13, lw=0)
                    ax.text((x0 + x1) / 2, top * 0.03, f"нет света\n{datetime.fromtimestamp(s, TZ):%H:%M}–{datetime.fromtimestamp(e2, TZ):%H:%M}",
                            ha="center", va="bottom", fontsize=7.5, color=OFF_C)
        colors = [NIGHT_C if is_night(start + h * 3600) else DAY_C for h in range(24)]
        alpha = [1 if h <= cur_h else 0.25 for h in range(24)]
        for h in range(24):
            if tot[h] > 0:
                ax.bar(h, tot[h], width=0.78, color=colors[h], alpha=alpha[h], lw=1.5, edgecolor=BG)
                if tot[h] >= 0.05:
                    ax.text(h, tot[h] + top * 0.015, num(money[h], 1), ha="center", va="bottom", fontsize=7.5, fontweight="bold", color=FG)
        ax.axvline(cur_h + 0.5, color=MUT, lw=1, ls=(0, (3, 3)))
        ax.text(cur_h + 0.55, top * 0.9, "сейчас", fontsize=7.5, color=SUB, va="top")
        ax.set_xlim(-0.6, 23.6); ax.set_ylim(0, top)
        ax.set_xticks(range(0, 24, 2)); ax.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 2)], fontsize=8)
        ax.set_ylabel("кВт·ч за час", fontsize=9)
        n_k, d_k = sum(nk), sum(dk)
        leg = [Patch(color=NIGHT_C, label=f"ночь {kwh(n_k)} = {grn(n_k * TARIFF_NIGHT)}"),
               Patch(color=DAY_C, label=f"день {kwh(d_k)} = {grn(d_k * TARIFF_DAY)}")]
        note = "над столбиком — грн за этот час"
    else:
        b = boiler_buckets(start, n_days, 86400)                         # сутки в DST-дни ±1 ч — некритично
        nk, dk, money = [x[0] for x in b], [x[1] for x in b], [x[2] for x in b]
        tot = [a + c for a, c in zip(nk, dk)]
        top = max(1.0, max(tot) * 1.3)
        x = range(n_days)
        ax.bar(x, nk, width=0.72, color=NIGHT_C, lw=1.5, edgecolor=BG)
        ax.bar(x, dk, bottom=nk, width=0.72, color=DAY_C, lw=1.5, edgecolor=BG)
        avg = sum(tot) / n_days
        ax.axhline(avg, color=MUT, lw=1, ls=(0, (3, 3)))
        for i in x:
            if tot[i] > 0:
                ax.text(i, tot[i] + top * 0.015, num(money[i], 0 if n_days > 10 else 1), ha="center", va="bottom",
                        fontsize=7 if n_days > 10 else 8.5, fontweight="bold", color=FG)
        days = [start_day + timedelta(days=i) for i in x]
        WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        ax.set_xticks(list(x))
        ax.set_xticklabels([f"{WD[d.weekday()]}\n{d:%d.%m}" if n_days <= 10 else f"{d.day}" for d in days], fontsize=8)
        ax.set_xlim(-0.6, n_days - 0.4); ax.set_ylim(0, top); ax.set_ylabel("кВт·ч за день", fontsize=9)
        n_k, d_k = sum(nk), sum(dk)
        leg = [Patch(color=NIGHT_C, label=f"ночью {kwh(n_k)} = {grn(n_k * TARIFF_NIGHT)}"),
               Patch(color=DAY_C, label=f"днём {kwh(d_k)} = {grn(d_k * TARIFF_DAY)}"),
               Line2D([], [], color=MUT, lw=1, ls=(0, (3, 3)), label=f"в среднем {kwh(avg)}")]
        note = "над столбиком — грн за день"
    fig.legend(handles=leg, loc="lower left", bbox_to_anchor=(0.03, 0.0), ncol=len(leg), frameon=False, fontsize=9,
               handlelength=1.1, handleheight=1.1, labelcolor=FG)
    fig.text(0.975, 0.95, note, fontsize=8, color=MUT, ha="right", va="top")
    buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=BG); plt.close(fig)
    return buf.getvalue(), cap

def boiler_heats(rows):
    """Отдельные нагревы (паузы < 3 мин склеиваем): [(начало, конец, кВт·ч, грн, ночью ли)]."""
    out, cur = [], None
    for t0, dt, w in boiler_intervals(rows):
        if w < 30: continue
        k = w * dt / 3600 / 1000
        c = k * (TARIFF_NIGHT if is_night(t0) else TARIFF_DAY)
        if cur and t0 - cur[1] < 180:
            cur[1] = t0 + dt; cur[2] += k; cur[3] += c
        else:
            if cur: out.append(cur)
            cur = [t0, t0 + dt, k, c, is_night(t0)]
    return out + ([cur] if cur else [])

def spark(v, vmax, n=8):
    """Полоска ▰▱ длиной пропорционально v."""
    k = 0 if vmax <= 0 else max(1 if v > 0 else 0, round(v / vmax * n))
    return "▰" * k + "▱" * (n - k)

def boiler_text(period):
    """/boiler текстом: period "d" — сегодня, "y" — вчера (по нагревам), "w"/"m" — по дням."""
    today = now().date()
    start_day = {"d": today, "y": today - timedelta(days=1), "w": today - timedelta(days=6), "m": today.replace(day=1)}[period]
    start = datetime.combine(start_day, dtime(0), tzinfo=TZ).timestamp()
    end = datetime.combine(today, dtime(0), tzinfo=TZ).timestamp() if period == "y" else None
    rows = boiler_rows(start, end)
    e, cost, starts, heat = boiler_stats(rows)
    title = {"d": "сегодня", "y": f"вчера, {start_day.day} {MONTHS_GEN[start_day.month - 1]}", "w": f"7 дней · {start_day:%d.%m}–{today:%d.%m}",
             "m": f"{MONTHS[today.month - 1]} {today.year}"}[period]
    b = live.get("boiler")
    now_line = (("🔌 " if "обесточен" in boiler_off_text() else "⚠️ ") + boiler_off_text() if not b else "⏸ розетка выключена" if not b[0] else
                f"🔥 греет <b>{pw(b[1])}</b>" if b[1] and b[1] >= 30 else "✅ вода нагрета, не греет")
    if b and live.get("boiler_src") == "cloud": now_line += " · <i>через облако</i>"
    n_days = 1 if period == "y" else (today - start_day).days + 1
    split = boiler_buckets(start, n_days, 86400)
    nk, dk = sum(x[0] for x in split), sum(x[1] for x in split)
    span = {"d": "за сегодня", "y": f"за вчера, {start_day:%d.%m}", "w": f"за 7 дней, {start_day:%d.%m}–{today:%d.%m}",
            "m": f"за {MONTHS[today.month - 1]}, {start_day:%d.%m}–{today:%d.%m}"}[period]
    card = [f"💸 Потрачено {span}: <b>{grn(cost)}</b> · {kwh(e)}",
            f"🌙 Ночь: {kwh(nk)} × {num(TARIFF_NIGHT, 2)} = <b>{grn(nk * TARIFF_NIGHT)}</b>",
            f"☀️ День: {kwh(dk)} × {num(TARIFF_DAY, 2)} = <b>{grn(dk * TARIFF_DAY)}</b>",
            f"🔁 Нагревов: <b>{starts}</b> · грел {dur(heat)}"]
    if period in ("w", "m"):
        card.append(f"📊 В среднем: <b>{grn(cost / n_days)}</b> · {kwh(e / n_days)} в день")
    out = [f"🔥 <b>Бойлер</b> · {title} · {tts(time.time())}", f"Сейчас: {now_line}", quote(card)]
    if period in ("d", "y"):
        hs = boiler_heats(rows)
        when = "сегодня" if period == "d" else "вчера"
        if hs:
            mx = max(h[2] for h in hs)
            lines = [f"{'🌙' if h[4] else '☀️'} {datetime.fromtimestamp(h[0], TZ):%H:%M}–{datetime.fromtimestamp(h[1], TZ):%H:%M}"
                     f" {spark(h[2], mx, 6)} {num(h[2])} кВт·ч · <b>{num(h[3], 2)} грн</b>" for h in hs]
            out.append(fold([f"🕒 <b>Нагревы {when}</b>"] + lines))
        else:
            out.append("<i>Сегодня бойлер ещё не грел</i>" if period == "d" else "<i>Вчера бойлер не грел</i>")
    else:
        WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        days = [start_day + timedelta(days=i) for i in range(n_days)]
        mx = max((x[0] + x[1] for x in split), default=0)
        lines = [f"{WD[d.weekday()]} {d:%d.%m} {spark(x[0] + x[1], mx)} {num(x[0] + x[1])} кВт·ч · <b>{num(x[2], 2)} грн</b>"
                 for d, x in zip(days, split)]
        out.append(fold(["📅 <b>По дням</b>"] + lines[::-1]))             # свежие сверху
    with closing(db()) as c: first = c.execute("select min(ts) from boiler").fetchone()[0]
    if first and first > start + 3600:
        out.append(f"<i>Учёт ведётся с {datetime.fromtimestamp(first, TZ):%d.%m %H:%M}</i>")
    return "\n".join(out)

# ================= климат: датчики Tuya через облако =================
TUYA_TOKEN = os.path.join(os.path.dirname(STATE_FILE) or ".", "tuya_token.json")   # создаёт tuya_login.py
HUM_HIGH, HUM_LOW = int(E("HUM_HIGH", "65")), int(E("HUM_LOW", "30"))
CLIMATE_HIDE = {x.strip() for x in E("CLIMATE_HIDE", "").split(",") if x.strip()}   # датчики, которые не показывать
_tuya, tuya_lock = None, threading.Lock()
CLIMATE_CACHE = os.path.join(os.path.dirname(STATE_FILE) or ".", "climate.json")
RAW_DPS = {"27": "t", "46": "h", "101": "bat"}       # датчики без описания в облаке (Балкон, Ванная): поля MQTT
climate_raw = {}                                      # dev_id -> {"t": °C, "h": %, "bat": ..., "at": ts}

class _TokenSaver:
    """Tuya обновляет токен сам — сохраняем новый, чтобы вход переживал перезапуски."""
    def update_token(self, token_info):
        try:
            with open(TUYA_TOKEN) as f: t = json.load(f)
            t["token"] = {k: token_info.get(k, t["token"].get(k)) for k in ("t", "uid", "expire_time", "access_token", "refresh_token")}
            with open(os.open(TUYA_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: json.dump(t, f)
        except Exception as e:
            log.warning("tuya token: %s", e)

def tuya_mgr():
    """Один на весь бот менеджер облака Tuya (под tuya_lock)."""
    global _tuya
    if _tuya is None:
        from tuya_sharing import Manager
        with open(TUYA_TOKEN) as f: t = json.load(f)
        _tuya = Manager("HA_3y9q4ak7g4ephrvke", t["user_code"], t["terminal_id"], t["endpoint"], t["token"], _TokenSaver())
    return _tuya

def _on_mq(msg):
    """Сырые показания из MQTT: запоминаем у датчиков, которые облако не описывает."""
    try:
        data = msg.get("data") or {}
        dev, upd = data.get("devId"), {}
        for item in data.get("status") or []:
            k = RAW_DPS.get(str(item.get("dpId")))
            if k: upd[k] = item.get("value")
        if not dev or not upd: return
        if "t" in upd: upd["t"] = upd["t"] / 10
        climate_raw.setdefault(dev, {}).update(upd, at=time.time())
        with open(CLIMATE_CACHE, "w") as f: json.dump(climate_raw, f)
    except Exception as e:
        log.warning("tuya mq: %s", e)

def climate_loop():
    """Подписка на сообщения облака Tuya (MQTT): Балкон и Ванная приходят только так, раз в 2–10 мин."""
    climate_raw.update(load_json(CLIMATE_CACHE, {}))
    while True:
        try:
            with tuya_lock:
                m = tuya_mgr()
                m.update_device_cache()
                for d in m.device_map.values(): d.set_up = True
                m.refresh_mq()
                m.mq.add_message_listener(_on_mq)
            log.info("tuya: подписка на датчики включена")
            break                                       # SharingMQ сам переподключается
        except Exception as e:
            log.warning("tuya mq start: %s %s", type(e).__name__, e)
            time.sleep(300)
    while True:                                         # история для стрелок и графиков — раз в 10 мин
        try:
            climate_record(climate_read())
        except Exception as e:
            log.warning("climate history: %s", e)
        time.sleep(600)

def climate_record(data):
    if not data: return
    now_ = int(time.time())
    rows = [(now_, name, t, h) for name, t, h, _, _, at in data
            if not (at and now_ - at > 3 * 3600)]       # датчики шлют только изменения: старше 3 ч — не пишем
    with closing(db()) as c, c:
        c.executemany("insert or replace into climate values (?,?,?,?)", rows)

def climate_ago(name, sec=3600):
    """(°C, %) у датчика примерно sec секунд назад (±15 мин) или None."""
    with closing(db()) as c:
        return c.execute("select t, h from climate where name = ? and ts between ? and ? order by abs(ts - ?) limit 1",
                         (name, time.time() - sec - 900, time.time() - sec + 900, time.time() - sec)).fetchone()

def trend(cur, old, step, unit, d=1):
    """« ↗ +0,4°» / « ↘ −3%» / «» — изменение, если оно не меньше step."""
    if old is None or cur is None or abs(cur - old) < step: return ""
    return f" {'↗' if cur > old else '↘'} {'+' if cur > old else '−'}{num(abs(cur - old), d)}{unit}"

def climate_read():
    """[(имя, °C, %влажности | None, батарейка | None, онлайн, когда обновлялось | None)]. None — облака нет."""
    global _tuya
    if not os.path.exists(TUYA_TOKEN): return None
    with tuya_lock:
        try:
            m = tuya_mgr()
            m.update_device_cache()
            devs = list(m.device_map.values())
        except Exception as e:
            log.warning("tuya cloud: %s %s", type(e).__name__, e)
            return None
    out = []
    for d in devs:
        if d.name.strip() in CLIMATE_HIDE: continue
        st = d.status or {}
        if "va_temperature" in st:
            try: scale = json.loads(d.status_range["va_temperature"].values).get("scale", 1)
            except Exception: scale = 1
            bat = st.get("battery_percentage", st.get("battery_state"))
            out.append((d.name, st["va_temperature"] / 10 ** scale, st.get("va_humidity"), bat, d.online, None))
        elif d.id in climate_raw and "t" in climate_raw[d.id]:
            r = climate_raw[d.id]
            out.append((d.name, r["t"], r.get("h"), r.get("bat"), d.online, r.get("at")))
    return sorted(out, key=lambda x: x[0])

def climate_text():
    data = climate_read()
    head = f"🌡 <b>Климат</b> · {tts(time.time())}"
    if data is None:
        return head + ("\n<i>Нет связи с облаком Tuya — попробуй позже</i>" if os.path.exists(TUYA_TOKEN)
                       else "\n<i>Вход в Tuya не настроен: запусти tuya_login.py</i>")
    if not data: return head + "\n<i>Датчиков температуры не нашлось</i>"
    card, notes_, arrows = [], [], False
    for name, t, h, bat, online, at in data:
        old = climate_ago(name)
        tt_ = trend(t, old and old[0], 0.2, "°")
        line = f"{'🌡' if online else '📴'} {html.escape(name)}: <b>{num(t)} °C</b>{tt_}"
        if h is not None:
            th = trend(h, old and old[1], 2, "%", 0)
            arrows = arrows or bool(tt_ or th)
            line += f" · 💧 {h}%{th}" + (" <i>влажно</i>" if h >= HUM_HIGH else " <i>сухо</i>" if h <= HUM_LOW else "")
        if at and time.time() - at > 1800: line += f" · <i>{dur((time.time() - at) / 3600)} назад</i>"
        if isinstance(bat, int) and bat <= 20 or bat == "low":
            line += " · 🪫"; notes_.append(f"🪫 {html.escape(name)}: пора сменить батарейку")
        if not online: line += " · <i>не на связи</i>"
        card.append(line)
    wet = [n for n, _, h, *_ in data if h is not None and h >= HUM_HIGH]
    if wet: notes_.insert(0, f"💧 Влажность выше {HUM_HIGH}% — стоит проветрить")
    if arrows: notes_.append("↗↘ — изменение за последний час")
    return "\n".join([head, quote(card)] + [f"<i>{x}</i>" for x in notes_])

# ================= финансы: квартира + бойлер =================
def apt_energy(start, end):
    """Через инвертор за [start, end): (квартира днём, ночью, заряд батареи днём, ночью — кВт·ч, сек без данных).
    Из сети (рег. 76) = квартира + заряд батареи (рег. 70); заряд — от сети, солнечных панелей нет."""
    with closing(db()) as c:
        rows = c.execute("select ts, imp, chg from samples where ts >= ? and ts < ? order by ts", (start, end)).fetchall()
    kd, kn = grid_cost([(r[0], r[1]) for r in rows])
    cd, cn = grid_cost([(r[0], r[2]) for r in rows])
    cd, cn = min(cd, kd), min(cn, kn)                                  # заряд не больше взятого из сети
    kd, kn = kd - cd, kn - cn
    with closing(db()) as c: first = c.execute("select min(ts) from samples").fetchone()[0] or end
    start, stop = max(start, first), min(end, time.time())            # до начала истории — не «дыра»
    if stop <= start: return kd, kn, cd, cn, 0
    if not rows: return kd, kn, cd, cn, stop - start
    gap = max(0, rows[0][0] - start - 120) + max(0, stop - rows[-1][0] - 120)
    gap += sum(b[0] - a[0] for a, b in zip(rows, rows[1:]) if b[0] - a[0] > 300)
    return kd, kn, cd, cn, gap

def finance_text(period):
    """/finance: деньги за свет — квартира (через инвертор) + бойлер (розетка). period: d / y / w / m."""
    today = now().date()
    start_day = {"d": today, "y": today - timedelta(days=1), "w": today - timedelta(days=6), "m": today.replace(day=1)}[period]
    last_day = start_day if period == "y" else today
    days = [start_day + timedelta(days=i) for i in range((last_day - start_day).days + 1)]
    ts = lambda d: datetime.combine(d, dtime(0), tzinfo=TZ).timestamp()
    cost = lambda d_, n_: d_ * TARIFF_DAY + n_ * TARIFF_NIGHT
    per = []                                                    # по дням: (день, квартира ₴, бойлер ₴, заряд ₴)
    a_d = a_n = c_d = c_n = b_d = b_n = 0.0; gap = 0
    for d in days:
        s, e = ts(d), ts(d + timedelta(days=1))
        kd, kn, cd, cn, g = apt_energy(s, e)
        bb = boiler_buckets(s, 1, 86400)[0] if BOILER_ID else [0.0, 0.0, 0.0]
        a_d += kd; a_n += kn; c_d += cd; c_n += cn; b_n += bb[0]; b_d += bb[1]; gap += g
        per.append((d, cost(kd, kn), bb[2], cost(cd, cn)))
    apt_c, chg_c, boil_c = cost(a_d, a_n), cost(c_d, c_n), cost(b_d, b_n)
    total_c, total_k = apt_c + chg_c + boil_c, a_d + a_n + c_d + c_n + b_d + b_n
    nk, dk = a_n + c_n + b_n, a_d + c_d + b_d
    title = {"d": "сегодня", "y": f"вчера, {start_day.day} {MONTHS_GEN[start_day.month - 1]}",
             "w": f"7 дней · {start_day:%d.%m}–{today:%d.%m}", "m": f"{MONTHS[today.month - 1]} {today.year}"}[period]
    span = {"d": "за сегодня", "y": f"за вчера, {start_day:%d.%m}", "w": "за 7 дней", "m": f"за {MONTHS[today.month - 1]}"}[period]
    card = [f"💸 Всего {span}: <b>{grn(total_c)}</b> · {kwh(total_k)}",
            f"🏠 Квартира: <b>{grn(apt_c)}</b> · {kwh(a_d + a_n)}",
            f"🔋 Заряд батареи: <b>{grn(chg_c)}</b> · {kwh(c_d + c_n)}" if c_d + c_n >= 0.05 else None,
            f"🔥 Бойлер: <b>{grn(boil_c)}</b> · {kwh(b_d + b_n)}" if BOILER_ID else None]
    tariff = [f"🌙 Ночь: {kwh(nk)} × {num(TARIFF_NIGHT, 2)} = <b>{grn(nk * TARIFF_NIGHT)}</b>",
              f"☀️ День: {kwh(dk)} × {num(TARIFF_DAY, 2)} = <b>{grn(dk * TARIFF_DAY)}</b>"]
    out = [f"💰 <b>Финансы</b> · {title} · {tts(time.time())}", quote([x for x in card if x]), quote(tariff)]
    if period in ("w", "m"):
        with closing(db()) as c: first = c.execute("select min(ts) from samples").fetchone()[0]
        if first:                                               # дни до начала истории не показываем
            per = [x for x in per if x[0] >= datetime.fromtimestamp(first, TZ).date()] or per
        n = len(per)
        avg = [f"📊 В среднем <b>{grn(total_c / n)}</b> в день"]
        if period == "m":
            avg.append(f"📆 Прогноз на месяц: <b>≈ {grn(total_c / n * calendar.monthrange(today.year, today.month)[1])}</b>")
        out += avg
        mx = max((a + b + c for _, a, b, c in per), default=0)
        WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        lines = [f"{WD[d.weekday()]} {d:%d.%m} {spark(a + b + c, mx)} <b>{num(a + b + c, 2)} грн</b> · 🏠 {num(a, 2)}"
                 + (f" · 🔋 {num(c, 2)}" if c >= 0.01 else "") + (f" · 🔥 {num(b, 2)}" if BOILER_ID else "")
                 for d, a, b, c in per]
        out.append(fold(["📅 <b>По дням</b>"] + lines[::-1]))
    extra = []
    with closing(db()) as c: first = c.execute("select min(ts) from samples").fetchone()[0]
    if first and first > ts(start_day) + 3600:
        extra.append(f"Данные квартиры есть с {datetime.fromtimestamp(first, TZ):%d.%m %H:%M}")
    if gap > 1800:
        extra.append(f"⚠️ Инвертор не отвечал {dur(gap / 3600)} — расход квартиры за это время не учтён")
    extra.append(f"Тариф: день {num(TARIFF_DAY, 2)}, ночь {num(TARIFF_NIGHT, 2)} грн/кВт·ч ({NIGHT_FROM:02d}:00–{NIGHT_TO:02d}:00)")
    out.append("\n".join(f"<i>{x}</i>" for x in extra))
    return "\n".join(out)

def kb_finance(p):
    mark = lambda k, t: ("• " + t) if k == p else t
    return json.dumps({"inline_keyboard": [
        [{"text": mark("y", "⏪ Вчера"), "callback_data": "fy"}, {"text": mark("w", "🗓 7 дней"), "callback_data": "fw"},
         {"text": mark("m", "📆 Месяц"), "callback_data": "fm"}],
        [{"text": "🔄 Сегодня", "callback_data": "fd"}]]})

def make_climate_graph(hours):
    """PNG: температура и влажность по датчикам (два графика друг под другом). (png | None, подпись)."""
    with closing(db()) as c:
        rows = c.execute("select ts, name, t, h from climate where ts >= ? order by ts", (time.time() - hours * 3600,)).fetchall()
    title = "сутки" if hours <= 24 else f"{hours // 24} дней"
    names = sorted({r[1] for r in rows})
    if not rows or max(sum(1 for r in rows if r[1] == n) for n in names) < 3:
        return None, "🌡 <i>Графики появятся, когда накопится история: показания пишутся раз в 10 минут</i>"
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt, matplotlib.dates as mdates
    except ImportError:
        return None, "⚠️ Для графиков нужна библиотека matplotlib"
    BG, FG, SUB, MUT, GRID = "#17212b", "#eef1f4", "#9aa7b4", "#5f6d7a", "#26323e"
    COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9"]   # фиксированный порядок
    plt.rcParams.update({"font.size": 10, "text.color": FG, "axes.labelcolor": SUB, "xtick.color": SUB, "ytick.color": MUT})
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(8, 6), dpi=160, sharex=True)
    fig.patch.set_facecolor(BG)
    dtt = lambda t: datetime.fromtimestamp(t, TZ)
    cap_lines, ends = [], {a1: [], a2: []}
    for i, n in enumerate(names):
        pts = [r for r in rows if r[1] == n]
        col = COLORS[i % len(COLORS)]
        xs = [dtt(r[0]) for r in pts]
        ts_, hs = [r[2] for r in pts], [r[3] for r in pts]
        for ax, ys, unit in ((a1, ts_, "°"), (a2, hs, "%")):
            seg_x, seg_y = [], []                                         # разрыв линии на дырах > 3 ч
            for j, (x, y) in enumerate(zip(xs, ys)):
                if j and (pts[j][0] - pts[j - 1][0]) > 3 * 3600:
                    seg_x.append(xs[j - 1] + timedelta(seconds=1)); seg_y.append(float("nan"))   # NaN — разрыв линии
                seg_x.append(x); seg_y.append(float("nan") if y is None else y)
            ax.plot(seg_x, seg_y, color=col, lw=2, label=n if ax is a1 else None)
            if ys and ys[-1] is not None:
                ends[ax].append([ys[-1], f"  {num(ys[-1], 1 if unit == '°' else 0)}{unit}", xs[-1]])
        hv = [h for h in hs if h is not None]
        cap_lines.append(f"{html.escape(n)}: {num(min(ts_))}…{num(max(ts_))} °C" + (f" · 💧 {min(hv)}…{max(hv)}%" if hv else ""))
    for ax, lab in ((a1, "Температура, °C"), (a2, "Влажность, %")):
        ax.set_facecolor(BG); ax.grid(color=GRID, lw=0.8); ax.set_axisbelow(True); ax.tick_params(length=0)
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.set_ylabel(lab, fontsize=9)
    for ax, vals in ((a1, [r[2] for r in rows]), (a2, [r[3] for r in rows if r[3] is not None])):
        lo, hi = min(vals), max(vals); pad = max(1.0, (hi - lo) * 0.12)
        ax.set_ylim(lo - pad, hi + pad)
        gap = (hi - lo + 2 * pad) * 0.055                                  # подписи в конце линий не налезают
        prev = None
        for e in sorted(ends[ax], key=lambda e: e[0]):
            y = e[0] if prev is None else max(e[0], prev + gap)
            ax.text(e[2], y, e[1], color=FG, fontsize=8, va="center"); prev = y
    y0, y1 = a2.get_ylim()
    if HUM_HIGH < y1: a2.axhspan(max(HUM_HIGH, y0), y1, color="#3987e5", alpha=0.07, lw=0)
    a2.set_ylim(y0, y1)
    if y0 < HUM_HIGH < y1: a2.text(0.005, HUM_HIGH + 0.5, f"влажно > {HUM_HIGH}%", transform=a2.get_yaxis_transform(), fontsize=7.5, color=SUB, va="bottom")
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if hours <= 48 else "%d.%m", tz=TZ))
    fig.legend(loc="lower center", ncol=min(4, len(names)), frameon=False, fontsize=9, labelcolor=FG)
    fig.text(0.02, 0.975, "Климат", fontsize=14, fontweight="bold", va="top")
    fig.text(0.13, 0.97, f"· {title}", fontsize=11, color=SUB, va="top")
    fig.subplots_adjust(left=0.09, right=0.93, top=0.92, bottom=0.11, hspace=0.12)
    buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=BG); plt.close(fig)
    return buf.getvalue(), f"🌡 <b>Климат</b> · {title}\n" + quote(cap_lines)

# ================= telegram =================
outbox = []  # (ts, text, kb) — копится, если нет интернета

def kb(text, data): return json.dumps({"inline_keyboard": [[{"text": text, "callback_data": data}]]})
def kbs(*btns): return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in btns]]})
KB_NOW = kbs(("📊 Показать сейчас", "now"), ("🔋 Батарея", "bat"))   # под событиями
KB_REFRESH = kbs(("🔄 Обновить", "rf"), ("🔋 Батарея", "bat"))       # под статусом: обновляет это же сообщение
KB_BATT = kbs(("🔄 Обновить", "batrf"))
KB_REPORT = kbs(("📅 Сегодня", "r1"), ("🗓 7 дней", "r7"), ("📆 Месяц", "rm"))
KB_LOGS = kbs(("🔄 Обновить", "lg"), ("⚠️ Только ошибки", "lge"))
KB_LOGS_ERR = kbs(("🔄 Обновить", "lge"), ("📜 Все строки", "lg"))
KB_RESTART = kbs(("♻️ Перезапустить", "rsy"), ("Отмена", "rsn"))
KB_BOILER = kbs(("📈 Сутки", "b1"), ("🗓 7 дней", "b7"), ("📆 Месяц", "bm"))      # под графиком
def kb_boiler_text(p):                                                          # под текстом: период + график
    mark = lambda k, t: ("• " + t) if k == p else t
    return json.dumps({"inline_keyboard": [
        [{"text": mark("y", "⏪ Вчера"), "callback_data": "bty"}, {"text": mark("w", "🗓 7 дней"), "callback_data": "btw"},
         {"text": mark("m", "📆 Месяц"), "callback_data": "btm"}],
        [{"text": "🔄 Сегодня", "callback_data": "btd"}]
        + ([{"text": "📈 График", "callback_data": "b" + {"d": "1", "w": "7", "m": "m"}[p]}] if p != "y" else [])]})
KB_CLIMATE = kbs(("🔄 Обновить", "cl"), ("📈 Графики", "clg24"))
KB_CLIMATE_G = kbs(("📈 Сутки", "clg24"), ("🗓 7 дней", "clg168"))
KB_CLEAN = json.dumps({"inline_keyboard": [[{"text": "🧹 Запросы и ответы", "callback_data": "clq"},
                                              {"text": "🗑 Всё", "callback_data": "cla"}],
                                             [{"text": "Отмена", "callback_data": "cln"}]]})
KB_INV_DOWN = kbs(("🔌 Это я выключил", "invoff"), ("📊 Статус", "now"))
KB_NONE = json.dumps({"inline_keyboard": []})
KB_GRAPH = json.dumps({"inline_keyboard": [[{"text": "📈 Сутки", "callback_data": "g24"},
                                             {"text": "📈 7 дней", "callback_data": "g168"}]]})

def tg(method, _timeout=10, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data, timeout=_timeout) as r:
        return json.load(r)["result"]

def plain(text): return html.unescape(re.sub(r"<[^>]+>", "", text))

NO_PREVIEW = json.dumps({"is_disabled": True})
PRE_LANG = re.compile(r'<pre><code class="[^"]*">')

# --- учёт отправленных сообщений для /clean (Telegram даёт удалять только моложе 48 ч) ---
MSGS_FILE = os.path.join(os.path.dirname(STATE_FILE) or ".", "msgs.json")
msgs_lock = threading.Lock()

def remember(mid, kind="query"):
    """kind: "event" — уведомление (свет, ошибки…), "query" — ответ на запрос или ваша команда."""
    if not mid: return
    with msgs_lock:
        lst = [x for x in load_json(MSGS_FILE, []) if time.time() - x[1] < 49 * 3600]
        lst.append([int(mid), int(time.time()), kind])
        try:
            with open(MSGS_FILE, "w") as f: json.dump(lst, f)
        except Exception as e: log.warning("msgs: %s", e)

def clean_chat(everything):
    """Удалить сообщения моложе 48 ч: только запросы или всё. Возвращает, сколько удалено."""
    with msgs_lock:
        lst = load_json(MSGS_FILE, [])
        fresh = [x for x in lst if time.time() - x[1] < 47.5 * 3600]
        kill = [x[0] for x in fresh if everything or x[2] != "event"]
        keep = [x for x in fresh if x[0] not in set(kill)]
        with open(MSGS_FILE, "w") as f: json.dump(keep, f)
    done = 0
    for i in range(0, len(kill), 100):
        part = kill[i:i + 100]
        try:
            tg("deleteMessages", chat_id=TG_CHAT, message_ids=json.dumps(part)); done += len(part)
        except Exception:
            for mid in part:                                     # по одному: часть могла уже исчезнуть
                try: tg("deleteMessage", chat_id=TG_CHAT, message_id=mid); done += 1
                except Exception: pass
    log.info("/clean: удалено %s сообщений (%s)", done, "всё" if everything else "запросы")
    return done

def tg_send(text, keyboard=KB_NOW, effect=None, silent=False, kind="query"):
    """sendMessage с HTML. Если Telegram что-то отверг — упрощаем: без эффекта/языка у <pre>, потом текстом."""
    base = dict(chat_id=TG_CHAT, reply_markup=keyboard, link_preview_options=NO_PREVIEW)
    if silent: base["disable_notification"] = "true"
    tries = [dict(text=text, parse_mode="HTML", **({"message_effect_id": effect} if effect and EFFECTS else {})),
             dict(text=PRE_LANG.sub("<pre><code>", text), parse_mode="HTML"),
             dict(text=plain(text))]
    for i, extra in enumerate(tries):
        try:
            mid = tg("sendMessage", **base, **extra)["message_id"]
            remember(mid, kind)
            return mid
        except urllib.error.HTTPError as e:
            if e.code != 400 or i == len(tries) - 1: raise
            log.error("telegram 400 (%s), упрощаю", e.read()[:200])

def tg_photo(png, caption, keyboard=KB_GRAPH):
    b = uuid.uuid4().hex
    fields = {"chat_id": TG_CHAT, "caption": caption, "parse_mode": "HTML", "reply_markup": keyboard,
              "disable_notification": "true"}
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                    for k, v in fields.items())
    body += (f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="graph.png"\r\n'
             f'Content-Type: image/png\r\n\r\n').encode() + png + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=30) as r: res = json.load(r)["result"]
    remember(res.get("message_id"))
    return res

def flush():
    mid = None
    while outbox:
        ts, text, keyboard, effect, silent = outbox[0]
        if time.time() - ts > 90:
            text = f"<i>⏱ событие в {tt(ts)}, доставлено с задержкой</i>\n{text}"
        try:
            mid = tg_send(text, keyboard, effect, silent, kind="event")
        except urllib.error.HTTPError as e:
            if e.code == 400:
                log.error("telegram: сообщение отброшено"); outbox.pop(0); continue
            log.warning("telegram: %s", e); return None
        except Exception as e:
            log.warning("telegram: %s", e); return None
        outbox.pop(0)
    return mid

def send(text, keyboard=KB_NOW, effect=None, silent=False):
    log.info("-> %s", plain(text).replace("\n", " | "))
    outbox.append((time.time(), text, keyboard, effect, silent))
    return flush()

def edit(mid, text, keyboard=KB_NOW):
    for t in (text, PRE_LANG.sub("<pre><code>", text)):
        try:
            tg("editMessageText", chat_id=TG_CHAT, message_id=mid, text=t, parse_mode="HTML",
               reply_markup=keyboard, link_preview_options=NO_PREVIEW)
            return True
        except urllib.error.HTTPError as e:
            if b"not modified" in e.read(): return True
            if e.code != 400: return False
        except Exception:
            return False
    return False

# ================= сообщения =================
def fault_line(c):
    """Код + что значит + совет (курсивом на второй строке)."""
    if c[0] in "FW":
        icon, table = ("❌", FAULT_NAMES) if c[0] == "F" else ("⚠️", ALARM_NAMES)
        name, hint = table.get(int(c[1:]), ("нет в таблице мануала", "смотри код в мануале или у установщика"))
        return f"{icon} <code>{c}</code> {name}\n💡 <i>{hint}</i>"
    kind = "авария батареи" if c.startswith("BF") else "предупреждение батареи"
    return f"🔋 <code>BMS {c[3:]}</code> {kind}\n💡 <i>посмотри индикацию и экран батареи</i>"

def fault_text(prev, cur, state):
    new, gone = [c for c in cur if c not in prev], [c for c in prev if c not in cur]
    if new:
        bad = any(c[0] == "F" or c.startswith("BF") for c in new)
        head = "🚨 <b>Ошибка инвертора</b>" if bad else "⚠️ <b>Предупреждение инвертора</b>"
    else:
        head = "✅ <b>Ошибки сняты</b>"
    out = [f"{head} · {tt(time.time())}"]
    if new: out.append(quote([fault_line(c) for c in new]))
    if gone: out.append("✅ <i>Ушло:</i> " + ", ".join(f"<code>{c}</code> {fault_line(c).split(chr(10))[0].split('</code> ', 1)[1]}" for c in gone))
    still = [c for c in cur if c in prev]
    if still: out.append(f"<i>Ещё активно:</i> <code>{', '.join(still)}</code>")
    out.append(f"<i>Состояние инвертора: {STATES.get(state, state)}</i>")
    return "\n".join(out)

def hhmm(m): return f"{m // 60 % 24:02d}:{m % 60:02d}"

def sched_change_text(old, new):
    fmt = lambda sl: ", ".join(f"{hhmm(a)}–{hhmm(b)}" for a, b in sl) or "отключений нет"
    out = [f"📅 <b>График на сегодня изменился</b> · {tt(time.time())}",
           quote([f"Было: <s>{fmt(old)}</s>", f"Стало: <b>{fmt(new)}</b>"])]
    n, line = next_line(time.time())
    return "\n".join(out + ([line] if n else []) + notes())

def overload_text(d, disch_w):
    fc, _ = outage_forecast(d["soc"], disch_w)
    card = [f"🏠 Нагрузка <b>{pw(d['load_w'])}</b> из {pw(INVERTER_W)}", batt_line(d)]
    return "\n".join([f"⚠️ <b>Большая нагрузка</b> · {tt(time.time())}", quote(card)] + fc[:1] +
                     ["<i>Выключи чайник, обогреватель или другие мощные приборы</i>"])

def ago(t):
    days = (now().date() - datetime.fromtimestamp(t, TZ).date()).days
    return "сегодня" if days == 0 else "вчера" if days == 1 else f"{days} дн. назад ({datetime.fromtimestamp(t, TZ):%d.%m})"

PENDING = "⏳ <i>Считаю время зарядки…</i>"
NO_CHARGE = verdict(None, "Батарея не заряжается", "сеть есть 5 мин — проверь Grid Charge / TOU")
NEAR_FULL = verdict(True, "Батарея почти полная", "последние проценты инвертор не дозаряжает — это нормально")

def status(d, grid, avg_w, title="📊 <b>Статус</b>"):
    since = live.get("since") if live.get("grid") == grid else None
    ago_ = f" · с {tt(since)} <i>({dur((time.time() - since) / 3600)})</i>" if since else ""
    card = [(f"🟢 Сеть <b>{d['grid_v']:.0f} В</b>" if grid else "🔴 <b>Сети нет</b>") + ago_,
            batt_line(d), flow_line(d), f"🏠 Нагрузка <b>{pw(d['load_w'])}</b>"]
    if d.get("pv_w"): card.append(f"☀️ PV <b>{pw(d['pv_w'])}</b>")
    if grid: card += power_lines(d)
    if live.get("faults"): card.append(f"🚨 Активно: <code>{', '.join(live['faults'])}</code>")
    if not grid:
        fc, verd = outage_forecast(d["soc"], avg_w)
    elif -avg_w >= 50 and -d["batt_w"] >= 50 and d["soc"] < 100:
        fc, verd = charge_forecast(d["soc"], -avg_w)
    elif YASNO_GROUP:
        fc, verd = [next_line(time.time())[1]], notes()
    else:
        fc, verd = [], []
    return assemble([f"{title} · {tts(time.time())}"], card, fc, verd, d, boiler=not grid)

def on_text(d, was, t_on, charge_w=None, fc=None, verd=None, extra=()):
    """fc=None — первое сообщение (ждём заряд), иначе финальное с прогнозом."""
    head = [f"🟢 <b>Свет появился</b> · {tt(t_on)}", f"<i>не было {was}</i>", *extra]
    card = [f"⚡ Сеть <b>{d['grid_v']:.0f} В</b>", batt_line(d),
            f"🔌 Заряд <b>{pw(charge_w)}</b> · {amps(d)}{volts(d['batt_v'])}" if charge_w else f"🔌 Батарея {volts(d['batt_v'])}"]
    if fc is None:
        return "\n".join(head + [quote(card), PENDING])
    return assemble(head, card + power_lines(d), fc, verd, d, load=True, pv=True, boiler=False)

def off_text(d, was, t_off, disch_w, extra=()):
    head = [f"🔴 <b>Свет пропал</b> · {tt(t_off)}", f"<i>был {was}</i>", *extra]
    card = [batt_line(d), f"🏠 Нагрузка <b>{pw(d['load_w'])}</b> · {volts(d['batt_v'])}"]
    fc, verd = outage_forecast(d["soc"], disch_w)
    return assemble(head, card, fc, verd, d, pv=True)

def soc_text(d, disch_w):
    fc, verd = outage_forecast(d["soc"], disch_w)
    return "\n".join([f"🪫 <b>Батарея {d['soc']}%</b> {bar(d['soc'])}"] + fc + verd)

def full_text(t):
    return f"✅ <b>Батарея заряжена</b> · {tt(t)}\n🔋 <b>100%</b> {bar(100)}"

def demo():
    """Шлёт в чат все варианты сообщений на выдуманных данных."""
    global YASNO_GROUP
    YASNO_GROUP = YASNO_GROUP or "demo"
    t, H = time.time(), 3600
    def sch(*ev, st="ScheduleApplies"):
        sched.update(events=[[t + a * H, t + b * H] for a, b in ev], status=st, ok_at=t)
    def d(soc, load=650, v=52.1, w=-2400):
        return {"grid_v": 229, "load_w": load, "batt_v": v, "soc": soc, "pv_w": 0,
                "batt_w": w, "batt_a": w / v, "batt_t": 24.5}
    was = "3 ч 58 мин"
    send("🧪 <b>Демо сообщений</b>\n<i>данные выдуманные</i>")
    sch((4, 8), (20, 22));  send(on_text(d(41), was, t), effect=EFF_PARTY)
    send(on_text(d(41), was, t, 2400, *charge_forecast(41, 2400)))
    sch((1.85, 6));         send(on_text(d(41), was, t, 1700, *charge_forecast(41, 1700)))
    sch((1, 5));            send(on_text(d(41), was, t, 2400, *charge_forecast(41, 2400)))
    sch((4, 8));            send(on_text(d(100), was, t, None, [next_line(t)[1]], verdict(True, "Батарея уже полная")))
    send(on_text(d(41), was, t, None, [], NO_CHARGE))
    send(full_text(t), effect=EFF_LIKE)
    sch((0, 4), (12, 16));  send(off_text(d(100, w=650), "4 ч 00 мин", t, 650))
    send(off_text(d(60, 1500, w=1500), "4 ч 00 мин", t, 1500))
    sch((-0.5, 2));         send(soc_text(d(30, 800, 50.9, 800), 800))
    sch((5, 9));            send(off_text(d(90, w=650), "4 ч 00 мин", t, 650))
    sch((0, 4), st="EmergencyShutdowns"); send(off_text(d(100, w=650), "4 ч 00 мин", t, 650))
    send(sched_change_text([[120, 330], [840, 1050]], [[120, 330], [900, 1110]]))
    send(overload_text(d(55, 4300, 51.8, 4300), 4300))
    send(fault_text([], ["F18", "W1"], 4))
    send(fault_text(["F18", "W1"], ["W1"], 3))
    sch((4, 8));            send(status(d(41), True, -2400), KB_REFRESH, silent=True)

# ================= состояние =================
def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception:
        return default

def save(st):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f: json.dump(st, f)

# ================= кнопки и команды (отдельный поток, long polling) =================
def inv_down_text():
    """Ответ на /status, когда инвертор не отвечает и свежих данных нет."""
    if live.get("inv_off"):
        out = [f"⏸ <b>Инвертор выключен</b> · с {tt(live['inv_off'])}"]
    else:
        out = ["⚠️ <b>Нет связи с инвертором</b>"]
    b = live.get("boiler") if BOILER_ID else None
    if b: out.append(f"🔥 Бойлер {'греет <b>' + pw(b[1]) + '</b>' if b[0] and b[1] and b[1] >= 30 else 'включён, не греет' if b[0] else 'выключен'}")
    out.append("<i>Данные появятся, когда инвертор снова будет на связи</i>")
    return "\n".join(out)

def live_status():
    """Свежее чтение инвертора; если связи нет — последние данные с пометкой."""
    try:
        d, note = read_locked(), ""
    except Exception:
        d = live["d"]
        if not d: return None
        note = (f"\n<i>⏸ Инвертор выключен с {tt(live['inv_off'])} — данные на {tts(live['t'])}</i>" if live.get("inv_off")
                else f"\n<i>⚠️ Нет связи с инвертором, данные на {tts(live['t'])}</i>")
    grid = d["grid_v"] >= GRID_V_MIN
    if BOILER_ID and grid: live["boiler"] = boiler_state()       # свежая мощность бойлера для суммы
    avg = live["avg"] if live["d"] else d["batt_w"]
    return d, grid, status(d, grid, avg, "📊 <b>Сейчас</b>") + note

def toast(d, grid):
    return f"🔋 {d['soc']}% · {batt_str(d['batt_w'])} · " + (f"⚡ {d['grid_v']:.0f} В" if grid else "сети нет")

def read_logs(n):
    """Последние строки лога службы: journalctl (переживает перезапуски), иначе буфер в памяти."""
    for cmd in (["journalctl", "--user", "-u", "deye-tg"], ["journalctl", "--user-unit", "deye-tg"]):
        try:
            out = subprocess.run(cmd + ["-n", str(n), "--no-pager", "-o", "cat"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if out and "No entries" not in out: return out.splitlines()
        except Exception:
            pass
    return list(LOG_BUF)[-n:]

def logs_text(errors=False, n=20):
    lines = read_logs(400 if errors else n)
    if errors: lines = [l for l in lines if re.search(r"WARNING|ERROR|Traceback|Error", l)]
    lines = [re.sub(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+ ", r"\1 ", l) for l in lines[-n:]]
    body = "\n".join(l if len(l) <= 160 else l[:157] + "…" for l in lines) or "пусто"
    for secret in (TG_TOKEN, HEARTBEAT_URL):
        if secret: body = body.replace(secret, "***")
    body = html.escape(body)
    while len(body) > 3500 and "\n" in body: body = body.split("\n", 1)[1]
    what = "только ошибки и предупреждения" if errors else f"последние {len(lines)} строк"
    return f"🧾 <b>Логи</b> · {what} · {tts(time.time())}\n<pre>{body}</pre>"

def restart_now():
    with open(RESTART_FLAG, "w") as f: f.write(str(time.time()))
    log.info("перезапуск по команде из Telegram")
    threading.Timer(1.5, lambda: (close_inverter(), os._exit(0))).start()   # systemd поднимет бота снова

def send_boiler_text(period="d", mid=None):
    if not BOILER_ID:
        tg_send("🔥 <i>Розетка бойлера не настроена</i>", KB_NONE, silent=True); return
    text = boiler_text(period)
    if not (mid and edit(mid, text, kb_boiler_text(period))): tg_send(text, kb_boiler_text(period), silent=True)

def send_boiler(period):
    if not BOILER_ID:
        tg_send("🔥 <i>Розетка бойлера не настроена</i>", KB_NONE, silent=True); return
    png, cap = make_boiler_graph(period)
    if png: tg_photo(png, cap, KB_BOILER)
    else: tg_send(cap, KB_BOILER, silent=True)

def send_graph(hours):
    png, cap = make_graph(hours)
    if png: tg_photo(png, cap)
    else: tg_send(cap, KB_NOW, silent=True)

def handle_update(u):
    if "callback_query" in u:
        cq = u["callback_query"]
        msg, data = cq.get("message") or {}, cq.get("data", "")
        if str(msg.get("chat", {}).get("id")) != TG_CHAT:
            tg("answerCallbackQuery", callback_query_id=cq["id"]); return
        if data in ("r1", "r7", "rm"):
            tg("answerCallbackQuery", callback_query_id=cq["id"])
            edit(msg["message_id"], report({"r1": "day", "r7": "week", "rm": "month"}[data]), KB_REPORT); return
        if data in ("bat", "batrf"):
            tg("answerCallbackQuery", callback_query_id=cq["id"])
            if data == "bat": tg_send(battery_text(), KB_BATT, silent=True)
            else: edit(msg["message_id"], battery_text(), KB_BATT)
            return
        if data in ("lg", "lge"):
            tg("answerCallbackQuery", callback_query_id=cq["id"])
            edit(msg["message_id"], logs_text(data == "lge"), KB_LOGS_ERR if data == "lge" else KB_LOGS); return
        if data in ("rsy", "rsn"):
            tg("answerCallbackQuery", callback_query_id=cq["id"])
            if data == "rsn":
                edit(msg["message_id"], "♻️ <i>Перезапуск отменён</i>", KB_NONE); return
            edit(msg["message_id"], f"♻️ <b>Перезапускаю бота</b> · {tts(time.time())}\n<i>Вернусь примерно через 20 секунд</i>", KB_NONE)
            restart_now(); return
        if data in ("clg24", "clg168"):
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Рисую график…")
            png, cap = make_climate_graph(int(data[3:]))
            if png: tg_photo(png, cap, KB_CLIMATE_G)
            else: tg_send(cap, KB_NONE, silent=True)
            return
        if data == "cl":
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Спрашиваю датчики…")
            edit(msg["message_id"], climate_text(), KB_CLIMATE); return
        if data == "invoff":
            live["inv_off"] = time.time()
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Понял, не беспокою")
            edit(msg["message_id"], f"⏸ <b>Инвертор выключен вами</b> · {tts(time.time())}\n"
                 "<i>Не беспокою, пока он не включится. Бойлер и климат считаются как обычно.</i>", KB_NOW)
            return
        if data in ("clq", "cla", "cln"):
            if data == "cln":
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="Отменено")
                for mid in (msg["message_id"],):
                    try: tg("deleteMessage", chat_id=TG_CHAT, message_id=mid)
                    except Exception: pass
                return
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Удаляю…")
            n = clean_chat(data == "cla")
            if n == 0: tg_send("🧹 <i>Удалять нечего: сообщений моложе 48 часов нет</i>", KB_NONE, silent=True)
            return
        if data in ("fd", "fy", "fw", "fm"):
            tg("answerCallbackQuery", callback_query_id=cq["id"])
            if not edit(msg["message_id"], finance_text(data[1]), kb_finance(data[1])):
                tg_send(finance_text(data[1]), kb_finance(data[1]), silent=True)
            return
        if data in ("btd", "bty", "btw", "btm"):
            tg("answerCallbackQuery", callback_query_id=cq["id"])
            send_boiler_text(data[2], msg["message_id"]); return
        if data in ("b1", "b7", "bm"):
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Рисую график…")
            send_boiler({"b1": "d", "b7": "w", "bm": "m"}[data]); return
        if data in ("g24", "g168"):
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Рисую график…")
            send_graph(int(data[1:])); return
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Читаю инвертор…")   # сразу, иначе нажатие «протухнет»
        r = live_status()
        if not r:
            tg_send(inv_down_text(), KB_REFRESH if live.get("inv_off") else KB_INV_DOWN, silent=True); return
        d, grid, text = r
        if data == "rf":
            edit(msg["message_id"], text, KB_REFRESH)
        else:
            tg_send(text, KB_REFRESH, silent=True)
        return
    m = u.get("message") or {}
    if str(m.get("chat", {}).get("id")) != TG_CHAT: return
    remember(m.get("message_id"))                                  # ваши команды тоже чистятся /clean
    cmd = m.get("text", "").split("@")[0].split()[0] if m.get("text") else ""
    if cmd == "/status":
        r = live_status()
        tg_send(r[2] if r else inv_down_text(), KB_REFRESH if r or live.get("inv_off") else KB_INV_DOWN, silent=True)
    elif cmd == "/report":
        tg_send(report("day"), KB_REPORT, silent=True)
    elif cmd == "/clean":
        lst = [x for x in load_json(MSGS_FILE, []) if time.time() - x[1] < 47.5 * 3600]
        nq, ne = sum(1 for x in lst if x[2] != "event"), sum(1 for x in lst if x[2] == "event")
        tg_send(f"🧹 <b>Очистить чат?</b>\n<i>Запросы и ответы: {nq} · уведомления: {ne}</i>\n"
                "<i>Telegram позволяет удалять только сообщения моложе 48 часов</i>", KB_CLEAN, silent=True)
    elif cmd == "/finance":
        tg_send(finance_text("d"), kb_finance("d"), silent=True)
    elif cmd == "/climate":
        tg_send(climate_text(), KB_CLIMATE, silent=True)
    elif cmd == "/boiler":
        send_boiler_text("d")
    elif cmd == "/graph":
        send_graph(24)
    elif cmd == "/logs":
        tg_send(logs_text(), KB_LOGS, silent=True)
    elif cmd == "/restart":
        tg_send("♻️ <b>Перезапустить бота?</b>\n<i>Это займёт около 20 секунд</i>", KB_RESTART, silent=True)

def updates_loop():
    offset = 0
    try:                                              # пропустить старые нажатия, повесить меню /status
        ups = tg("getUpdates", offset=-1, timeout=0)
        if ups: offset = ups[-1]["update_id"] + 1
        tg("setMyCommands", commands=json.dumps([{"command": "status", "description": "Состояние сейчас"},
                                                  {"command": "graph", "description": "График заряда и сети"},
                                                  {"command": "boiler", "description": "Бойлер: расход и деньги"},
                                                  {"command": "climate", "description": "Температура и влажность"},
                                                  {"command": "finance", "description": "Деньги за свет: квартира и бойлер"},
                                                  {"command": "clean", "description": "Очистить чат"},
                                                  {"command": "report", "description": "Отчёт за день или неделю"},
                                                  {"command": "logs", "description": "Последние строки лога"},
                                                  {"command": "restart", "description": "Перезапустить бота"}]))
    except Exception as e:
        log.warning("updates init: %s", e)
    while True:
        try:
            ups = tg("getUpdates", _timeout=35, offset=offset, timeout=25,
                     allowed_updates=json.dumps(["message", "callback_query"]))
        except Exception as e:
            log.warning("updates: %s", e); time.sleep(5); continue
        for u in ups:
            offset = u["update_id"] + 1
            try: handle_update(u)
            except Exception as e: log.warning("update: %s", e)

def boiler_loop():
    """Опрос розетки бойлера каждые 30 с и запись в историю — независимо от связи с инвертором."""
    while True:
        b = boiler_state()
        live["boiler"] = b
        want = live.get("boiler_want")
        if b and want is not None and b[0] != want:                   # розетка загрузилась после появления света
            if boiler_set(want) is True:
                send(f"🔥 Бойлер {'включён' if want else 'выключен'} · розетка вышла на связь", KB_NOW, silent=True)
            b = live.get("boiler")
        elif b and want is not None:
            live.pop("boiler_want", None)
        if b:
            try: boiler_add(b)
            except Exception as e: log.warning("boiler history: %s", e)
        time.sleep(30)

def heartbeat_loop():
    """Пинг внешнему сторожу: перестал приходить — значит, дом не на связи."""
    while True:
        try: urllib.request.urlopen(HEARTBEAT_URL, timeout=10).read()
        except Exception as e: log.warning("heartbeat: %s", e)
        time.sleep(HEARTBEAT_SEC)

# ================= main =================
def main():
    if "--test" in sys.argv:
        print(json.dumps(read(), indent=2)); return
    if "--demo" in sys.argv:
        demo(); return
    if "--yasno" in sys.argv:
        yasno_refresh(force=True)
        for a, b in sched["events"]: print(f"{hm(a)} – {hm(b)}")
        print("status:", sched["status"]); return

    log.info("старт, версия %s", VERSION or "неизвестна")
    signal.signal(signal.SIGTERM, _on_sigterm)
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    st = load_json(STATE_FILE, {"grid": None, "since": time.time(), "alerted": [], "full_sent": True, "offset": 0})
    sched.update(load_json(YASNO_CACHE, {}))
    samples = deque(maxlen=6)     # мощность батареи для сглаживания
    chg = deque(maxlen=6)         # ≈1 мин стабильного заряда после появления сети
    eta = None                    # ждём прогноз после появления сети
    cnt, d, fail_since = 0, None, None
    link_alert = st.get("link_alert", False)                      # помним и после перезапуска бота
    live["inv_off"] = st.get("inv_off")                            # «инвертор выключен вами» — не беспокоить
    polls, fpend, fcnt = 0, None, 0
    hist_at, over_n, over_at = 0, 0, 0
    spike, full_n = None, 0                       # фильтр скачков SOC и антидребезг «заряжена»
    boiler_alert = False
    live["faults"] = st.get("faults", [])
    if "full_at" not in st: st["full_at"] = time.time(); save(st)  # отсчёт калибровки — с первого запуска
    live["full_at"] = st["full_at"]
    live.update(grid=st.get("grid"), since=st.get("since"))         # «свет есть / нет с …» в статусе
    if os.path.exists(RESTART_FLAG):                             # бот поднялся после /restart
        try: took = int(time.time() - float(open(RESTART_FLAG).read()))
        except Exception: took = 0
        os.remove(RESTART_FLAG)
        send(f"✅ <b>Бот перезапущен</b> · {tt(time.time())}" + (f" · <i>за {took} с</i>" if took else ""), KB_NOW, silent=True)
    threading.Thread(target=updates_loop, daemon=True).start()
    if HEARTBEAT_URL: threading.Thread(target=heartbeat_loop, daemon=True).start()
    if BOILER_ID: threading.Thread(target=boiler_loop, daemon=True).start()
    if os.path.exists(TUYA_TOKEN): threading.Thread(target=climate_loop, daemon=True).start()

    while True:
        t0 = time.time()
        ch = yasno_refresh()
        if ch: send(sched_change_text(*ch))
        if live.get("inv_off") != st.get("inv_off"):              # кнопка «Это я выключил» (поток кнопок)
            st["inv_off"] = live.get("inv_off"); save(st)
        try:
            d = read_locked()
        except Exception as e:
            if not live.get("inv_off") or fail_since is None:
                log.warning("inverter: %s %s", type(e).__name__, e)
            fail_since = fail_since or t0
            if not link_alert and not live.get("inv_off") and t0 - fail_since > LINK_TIMEOUT:
                send(f"⚠️ <b>Нет связи с инвертором</b> · {tt(fail_since)}\n<i>больше 5 мин: выключен / логгер / Wi-Fi</i>\n"
                     "<i>Выключили сами — нажмите «Это я выключил», и я не буду беспокоить</i>", KB_INV_DOWN)
                link_alert = True; st["link_alert"] = True; save(st)
            flush()
            time.sleep(POLL); continue

        fail_since = None
        prev = live.get("d")
        if prev and abs(d["soc"] - prev["soc"]) > 10:              # скачок SOC — ждём подтверждения
            if spike is None or abs(d["soc"] - spike) > 3:
                log.warning("скачок SOC %s → %s, жду подтверждения", prev["soc"], d["soc"])
                spike = d["soc"]; time.sleep(POLL); continue
        spike = None
        samples.append(d["batt_w"])
        avg = mean(samples)
        live.update(d=d, t=t0, avg=avg)
        grid = d["grid_v"] >= GRID_V_MIN

        if link_alert or live.get("inv_off"):
            if live.get("inv_off"):
                send(f"✅ <b>Инвертор снова на связи</b> · {tt(t0)}\n<i>был выключен {dur((t0 - live['inv_off']) / 3600)}</i>",
                     KB_NOW, silent=True)
            else:
                send(status(d, grid, avg, "✅ <b>Связь с инвертором есть</b>"), KB_REFRESH, silent=True)
            link_alert = False; live["inv_off"] = None
            st.update(link_alert=False, inv_off=None); save(st)

        if st["grid"] is None:                                   # первый запуск
            st.update(grid=grid, since=t0); save(st); live.update(grid=grid, since=t0)
            send(status(d, grid, avg, "🤖 <b>Мониторинг запущен</b>"), KB_REFRESH, silent=True)
        elif grid != st["grid"]:
            cnt += 1
            if cnt >= CONFIRM:
                cnt = 0
                was = dur((t0 - st["since"]) / 3600)
                st.update(grid=grid, since=t0); live.update(grid=grid, since=t0)
                if grid:
                    bl = boiler_line(boiler_set(True), True) if BOILER_CONTROL else []   # свет есть — бойлер включаем
                    ch = yasno_refresh(force=True)                # график мог обновиться
                    if ch: send(sched_change_text(*ch))
                    mid = send(on_text(d, was, t0, extra=bl), effect=EFF_PARTY)
                    eta = {"mid": mid, "t0": t0, "was": was, "bl": bl}
                    chg.clear(); st["full_sent"] = False
                else:
                    eta = None
                    st["alerted"] = [x for x in SOC_ALERTS if d["soc"] <= x]
                    bl = boiler_line(boiler_set(False), False) if BOILER_CONTROL else []  # света нет — выключаем
                    recent = mean(list(samples)[-CONFIRM:])
                    send(off_text(d, was, t0, recent, bl))
                save(st)
        else:
            cnt = 0

        # прогноз заряда: ждём ~1 мин стабильного заряда (у Deye задержка подключения к сети)
        if st["grid"] and eta:
            chg.append(-d["batt_w"])
            fc, verd, cw = None, None, None
            if d["soc"] >= 99:
                fc = [next_line(t0)[1]] if YASNO_GROUP else []
                verd = verdict(True, "Батарея уже полная") + notes()
                st["full_sent"] = True
            elif len(chg) == chg.maxlen and min(chg) > 50:
                cw = mean(chg); fc, verd = charge_forecast(d["soc"], cw)
            elif t0 - eta["t0"] > 300:
                fc, verd = [], (NEAR_FULL if d["soc"] >= 95 else NO_CHARGE)
            if fc is not None:
                text = on_text(d, eta["was"], eta["t0"], cw, fc, verd, eta.get("bl", ()))
                if not (eta["mid"] and edit(eta["mid"], text)): send(text)
                eta = None

        full_n = full_n + 1 if d["soc"] >= 100 else 0
        if st["grid"] and not st.get("full_sent", True) and full_n >= 3:   # 100% три опроса подряд
            send(full_text(t0), effect=EFF_LIKE); st["full_sent"] = True; save(st)

        if BOILER_ID and BOILER_CONTROL and polls % 3 == 0:       # бойлер: страховка (опрос — в boiler_loop)
            b = live.get("boiler")
            if st["grid"] is False and b and b[0]:
                log.warning("бойлер включён при отключении — выключаю")
                if not boiler_set(False) and not boiler_alert:
                    send("⚠️ <b>Бойлер не отключается</b>\n<i>Света нет, а розетка включена — выключи бойлер вручную</i>")
                    boiler_alert = True
            if st["grid"]: boiler_alert = False

        polls += 1                                                # ошибки и предупреждения
        if FAULT_EVERY and polls % FAULT_EVERY == 0:
            try:
                f = read_faults()
            except Exception as e:
                f = None; log.warning("faults: %s", e)
            if f:
                state, cur, live["temps"], live["energy"] = f
                fcnt = fcnt + 1 if cur == fpend else 1
                fpend = cur
                if fcnt == 2 and cur != st.get("faults", []):     # два чтения подряд — не случайность
                    send(fault_text(st.get("faults", []), cur, state))
                    st["faults"] = cur; live["faults"] = cur; save(st)

        if t0 - hist_at >= 60:                                    # история для /graph и /report
            try: history_add(d, grid); hist_at = t0
            except Exception as e: log.warning("history: %s", e)

        if full_n >= 3 and t0 - st["full_at"] > 600:               # отметка полного заряда (для «Батарея»)
            st["full_at"] = live["full_at"] = t0; save(st)

        over_n = over_n + 1 if st["grid"] is False and d["load_w"] >= OVERLOAD_W else 0
        if over_n == 2 and t0 - over_at > 600:                    # перегрузка от батареи
            send(overload_text(d, avg)); over_at = t0

        if st["grid"] is False:                                   # пороги SOC при отключении
            hit = [x for x in SOC_ALERTS if d["soc"] <= x and x not in st["alerted"]]
            if hit:
                st["alerted"] += hit; save(st)
                send(soc_text(d, avg))

        flush()
        time.sleep(max(1, POLL - (time.time() - t0)))

if __name__ == "__main__":
    main()
