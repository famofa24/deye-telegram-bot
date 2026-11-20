#!/usr/bin/env bash
# Мастер установки Telegram-бота для инвертора Deye.
#   Скачать и поставить:  bash <(curl -fsSL https://raw.githubusercontent.com/famofa24/deye-telegram-bot/main/install.sh)
#   Из папки бота:        ./install.sh        (повторный запуск — изменить настройки; старые значения подставятся)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/famofa24/deye-telegram-bot.git}"
DIR="${DIR:-$HOME/deye-tg}"
B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
step() { echo; echo "${C}${B}━━ $* ━━${N}"; }
ok()   { echo "  ${G}✔${N} $*"; }
warn() { echo "  ${Y}⚠${N} $*"; }
die()  { echo "  ${R}✘ $*${N}"; exit 1; }
ask()  { local p="$1" d="${2:-}" a; read -r -p "  $p${d:+ [$d]}: " a </dev/tty; echo "${a:-$d}"; }
askyn(){ local a; read -r -p "  $1 [${2:-n}]: " a </dev/tty; a="${a:-${2:-n}}"; [[ "$a" =~ ^[yYдД] ]]; }

echo "${B}⚡ Установка Telegram-бота для инвертора Deye${N}"
echo "  Займёт 5–10 минут. Enter — оставить значение в [скобках]."

# ---------- 1. Система и файлы ----------
step "1/7 Система и файлы"
command -v apt-get >/dev/null || die "Нужен Debian / Raspberry Pi OS (apt)."
need=(); for p in git python3 python3-venv curl; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
if [ ${#need[@]} -gt 0 ]; then
  echo "  Ставлю: ${need[*]} (нужен пароль sudo)"; sudo apt-get update -qq && sudo apt-get install -y -qq "${need[@]}"
fi
ok "Python $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/deye_tg.py" ]; then DIR="$SELF_DIR"
elif [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q && ok "Обновил $DIR"
else git clone -q "$REPO_URL" "$DIR" && ok "Скачал в $DIR"; fi
cd "$DIR"
[ -d .venv ] || python3 -m venv .venv
echo "  Ставлю библиотеки Python (на Pi это пара минут)…"
.venv/bin/pip install -q --upgrade pip >/dev/null && .venv/bin/pip install -q -r requirements.txt
ok "Библиотеки установлены"
PY=.venv/bin/python
[ -f .env ] || { cp .env.example .env; chmod 600 .env; }
getv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/^"//; s/"$//'; }
setv() { $PY - "$1" "$2" <<'EOF'
import re, sys
k, v = sys.argv[1], sys.argv[2]
if " " in v: v = f'"{v}"'
s = open(".env").read()
s = re.sub(rf"(?m)^#?{k}=.*$", lambda m: f"{k}={v}", s, count=1) if re.search(rf"(?m)^#?{k}=", s) else s.rstrip("\n") + f"\n{k}={v}\n"
open(".env", "w").write(s)
EOF
}
# если бот уже запущен — остановить, иначе логгер и Telegram будут заняты
systemctl --user stop deye-tg 2>/dev/null && warn "Остановил работающего бота на время настройки" || true

# ---------- 2. Telegram ----------
step "2/7 Telegram"
TOKEN="$(getv TG_TOKEN)"
if [[ -z "$TOKEN" || "$TOKEN" == *your-token* ]] || ! askyn "Токен бота уже есть. Оставить? (y/n)" y; then
  echo "  Создайте бота у @BotFather → /newbot и вставьте токен (ввод скрыт)."
  read -r -s -p "  Токен: " TOKEN </dev/tty; echo
fi
BOT="$($PY -c "import json,sys,urllib.request; print(json.load(urllib.request.urlopen('https://api.telegram.org/bot'+sys.argv[1]+'/getMe',timeout=10))['result']['username'])" "$TOKEN" 2>/dev/null)" \
  || die "Telegram не принял токен. Проверьте его и интернет."
setv TG_TOKEN "$TOKEN"; ok "Бот @$BOT"
CHAT="$(getv TG_CHAT_ID)"
if [[ -z "$CHAT" || "$CHAT" == 123456789 ]] || ! askyn "Чат $CHAT. Оставить? (y/n)" y; then
  echo "  👉 Откройте ${B}t.me/$BOT${N} и нажмите ${B}Start${N} (или напишите любое сообщение). Жду до 3 минут…"
  CHAT="$($PY - "$TOKEN" <<'EOF'
import json, sys, time, urllib.request
api = f"https://api.telegram.org/bot{sys.argv[1]}/"
off = 0
try:
    ups = json.load(urllib.request.urlopen(api + "getUpdates?offset=-1", timeout=10))["result"]
    off = ups[-1]["update_id"] + 1 if ups else 0
except Exception: pass
end = time.time() + 180
while time.time() < end:
    ups = json.load(urllib.request.urlopen(api + f"getUpdates?timeout=20&offset={off}", timeout=30))["result"]
    for u in ups:
        off = u["update_id"] + 1
        m = u.get("message") or {}
        if "chat" in m: print(m["chat"]["id"]); sys.exit(0)
sys.exit(1)
EOF
)" || die "Не дождался сообщения. Запустите мастер ещё раз."
  setv TG_CHAT_ID "$CHAT"
fi
ok "Чат $CHAT"

# ---------- 3. Инвертор ----------
step "3/7 Инвертор (Wi-Fi-стик Solarman)"
echo "  Ищу стик в сети…"
FOUND="$($PY - <<'EOF'
import socket, time, ipaddress
out = {}
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1); s.settimeout(0.5)
for _ in range(3):                                       # стики Solarman отвечают «IP,MAC,серийник»
    try: s.sendto(b"WIFIKIT-214028-READ", ("255.255.255.255", 48899))
    except OSError: pass
    end = time.time() + 1.5
    while time.time() < end:
        try:
            data, _ = s.recvfrom(1024); p = data.decode(errors="ignore").strip().split(",")
            if len(p) >= 3 and p[2].isdigit(): out[p[0]] = p[2]
        except socket.timeout: pass
if not out:                                              # стик молчит — ищем открытый порт 8899 в своей сети /24
    from concurrent.futures import ThreadPoolExecutor
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.connect(("1.1.1.1", 80)); me = u.getsockname()[0]
        net = ipaddress.ip_network(me + "/24", strict=False)
        def probe(h):
            try:
                with socket.create_connection((str(h), 8899), timeout=0.6): return str(h)
            except OSError: return None
        with ThreadPoolExecutor(64) as ex:
            for ip in ex.map(probe, net.hosts()):
                if ip: out[ip] = ""
    except OSError: pass
for ip, sn in out.items(): print(ip, sn)
EOF
)"
IP="$(getv LOGGER_IP)"; SN="$(getv LOGGER_SN)"
if [ -n "$FOUND" ]; then
  echo "$FOUND" | while read -r i s; do ok "Найден стик: $i${s:+, серийник $s}"; done
  IP="$(echo "$FOUND" | head -1 | cut -d' ' -f1)"; S2="$(echo "$FOUND" | head -1 | cut -s -d' ' -f2)"; SN="${S2:-$SN}"
  [ -z "$S2" ] && warn "Серийник стик не сообщил — он на наклейке стика (10 цифр)"
else
  warn "Автопоиск не нашёл стик — введите вручную (IP — в роутере, серийник — на наклейке стика)."
fi
IP="$(ask "IP стика" "$IP")"; SN="$(ask "Серийник стика" "$SN")"
[ -n "$IP" ] && [ -n "$SN" ] || die "Нужны IP и серийник стика."
setv LOGGER_IP "$IP"; setv LOGGER_SN "$SN"
echo "  Читаю инвертор…"
if TEST="$(set -a; . ./.env; set +a; timeout 40 $PY deye_tg.py --test 2>/tmp/deye-test.err)"; then
  echo "$TEST" | $PY -c "import json,sys; d=json.load(sys.stdin); print(f\"  \033[32m✔\033[0m Сеть {d['grid_v']} В · батарея {d['soc']}% · нагрузка {d['load_w']} Вт · батарея {d['batt_w']} Вт\")"
  warn "Если сейчас батарея ЗАРЯЖАЕТСЯ, а число батареи выше положительное — включу BATT_INVERT."
  askyn "Батарея сейчас заряжается, а число положительное? (y/n)" n && setv BATT_INVERT 1 || true
else
  tail -3 /tmp/deye-test.err | sed 's/^/    /'
  warn "Не удалось прочитать инвертор: проверьте IP, серийник и что порт 8899 открыт. Продолжаю — исправите потом в .env."
fi

# ---------- 4. Батарея и тариф ----------
step "4/7 Батарея и тариф"
setv BATTERY_KWH "$(ask "Ёмкость батареи, кВт·ч" "$(getv BATTERY_KWH)")"
setv MIN_SOC "$(ask "Shutdown SOC из настроек инвертора, %" "$(getv MIN_SOC)")"
setv INVERTER_W "$(ask "Мощность инвертора, Вт" "$(getv INVERTER_W)")"
setv TARIFF_DAY "$(ask "Тариф днём, грн/кВт·ч" "$(getv TARIFF_DAY)")"
setv TARIFF_NIGHT "$(ask "Тариф ночью, грн/кВт·ч (однозонный — то же число)" "$(getv TARIFF_NIGHT)")"
setv MAIN_BREAKER_A "$(ask "Вводной автомат квартиры, А (пусто — не показывать)" "$(getv MAIN_BREAKER_A)")"

# ---------- 5. График ЯСНО ----------
step "5/7 График отключений ЯСНО (Киев)"
G_="$(ask "Ваша черга с yasno.ua, например 3.1 (пусто — без графика)" "$(getv YASNO_GROUP)")"
setv YASNO_GROUP "$G_"
if [ -n "$G_" ]; then
  if OUT="$(set -a; . ./.env; set +a; timeout 30 $PY deye_tg.py --yasno 2>&1)"; then
    echo "$OUT" | sed 's/^/    /' | tail -6; ok "График получен"
  else warn "График не получен — проверьте номер черги"; fi
fi

# ---------- 6. Tuya: бойлер и датчики ----------
step "6/7 Бойлер и датчики Tuya / Smart Life (необязательно)"
if askyn "Подключить розетку бойлера и/или датчики температуры? (y/n)" n; then
  echo "  Smart Life → Я → ⚙️ Настройки → Аккаунт и безопасность → User Code"
  UC="$(ask "User Code")"
  echo "  QR-код придёт в Telegram: отсканируйте его в Smart Life (вкладка «Я» → сканер вверху)."
  (set -a; . ./.env; set +a; $PY tuya_login.py "$UC") || warn "Вход в Tuya не удался"
  if [ -f devices.json ] && askyn "Есть розетка бойлера с измерением мощности? (y/n)" y; then
    echo "  Ищу розетки в сети (20 сек)…"
    $PY /dev/fd/3 3<<'EOF' || warn "Розетку настроить не удалось — см. README, раздел про Tuya"
import json, re, tinytuya
devs = [d for d in json.load(open("devices.json")) if d.get("category") in ("cz", "pc", "kg", "tdq")]
scan = tinytuya.deviceScan(verbose=False, maxretry=20, color=False, poll=False)
by_id = {v.get("gwId") or v.get("id"): v for v in scan.values()}
local = [(d, by_id[d["id"]]) for d in devs if d["id"] in by_id]
if not local: raise SystemExit("в локальной сети розеток не найдено")
for i, (d, s) in enumerate(local, 1): print(f"    {i}. {d['name']} — {s['ip']}, протокол {s['version']}")
n = int(input("  Номер розетки бойлера: ") or 1) if len(local) > 1 else 1
d, s = local[n - 1]
dev = tinytuya.OutletDevice(d["id"], s["ip"], d["key"], version=float(s["version"]))
dps = (dev.status() or {}).get("dps", {})
volt = lambda k: isinstance(dps.get(k), int) and 1800 <= dps[k] <= 2600       # напряжение в 0,1 В
power_dp = "22" if volt("23") else "19"
env = open(".env").read()
for k, v in (("BOILER_ID", d["id"]), ("BOILER_IP", s["ip"]), ("BOILER_VERSION", s["version"]), ("BOILER_POWER_DP", power_dp)):
    env = re.sub(rf"(?m)^{k}=.*$", f"{k}={v}", env) if re.search(rf"(?m)^{k}=", env) else env + f"\n{k}={v}\n"
open(".env", "w").write(env)
print(f"  \033[32m✔\033[0m Бойлер: {d['name']} ({s['ip']}), поле мощности {power_dp}")
print("  ⚠ Закрепите этот IP за розеткой в роутере (DHCP-резерв).")
EOF
  fi
fi < /dev/tty

# ---------- 7. Служба ----------
step "7/7 Запуск"
mkdir -p ~/.config/systemd/user
sed "s|%h/deye-tg|$DIR|g" deye-tg.service > ~/.config/systemd/user/deye-tg.service
systemctl --user daemon-reload
systemctl --user enable -q deye-tg
systemctl --user restart deye-tg
loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes || {
  echo "  Включаю автозапуск без входа в систему (нужен sudo)…"; sudo loginctl enable-linger "$USER"; }
sleep 5
if systemctl --user is-active -q deye-tg; then
  ok "Бот работает — в Telegram придёт «🤖 Мониторинг запущен»"
else
  warn "Бот не запустился. Лог: journalctl --user-unit deye-tg -n 30 --no-pager -o cat"
fi
echo
echo "${B}Готово 🎉${N}  Команды в боте: /status /finance /boiler /climate /report /graph"
echo "  Изменить настройки: ${B}$DIR/install.sh${N} или ${B}nano $DIR/.env${N} + ${B}systemctl --user restart deye-tg${N}"
echo "  Все виды сообщений на выдуманных данных: ${B}cd $DIR && set -a && . ./.env && set +a && .venv/bin/python deye_tg.py --demo${N}"
