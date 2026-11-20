#!/usr/bin/env python3
"""Вход в облако Tuya по QR-коду из приложения Smart Life (как в Home Assistant), без аккаунта разработчика.

  python tuya_login.py <User Code>   — QR-код уходит в Telegram; после скана сохраняется токен
                                       (data/tuya_token.json) и ключи устройств (devices.json).
User Code: Smart Life → Я → Настройки → Аккаунт и безопасность → User Code.
"""
import json, os, sys, time
from tuya_sharing import LoginControl, Manager

CID, SCHEMA = "HA_3y9q4ak7g4ephrvke", "haauthorize"
TOKEN_FILE = os.path.join(os.path.dirname(os.environ.get("STATE_FILE", "data/state.json")) or ".", "tuya_token.json")

def write_private(path, obj):
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)

def main():
    uc = sys.argv[1]
    lc = LoginControl()
    r = lc.qr_code(CID, SCHEMA, uc)
    if not r.get("success"): sys.exit(f"Tuya: {r.get('msg')} ({r.get('code')})")
    tok = r["result"]["qrcode"]
    import deye_tg as bot, qrcode
    from qrcode.image.pure import PyPNGImage
    import io
    buf = io.BytesIO(); qrcode.make(f"tuyaSmart--qrLogin?token={tok}", image_factory=PyPNGImage, box_size=12).save(buf)
    bot.tg_photo(buf.getvalue(), "🔑 <b>Вход Tuya</b>\nОтсканируй в Smart Life: «Я» → сканер вверху → подтвердить. Код действует ~5 мин.", bot.KB_NONE)
    print("QR отправлен в Telegram, жду скан…")
    for _ in range(100):
        ok, info = lc.login_result(tok, CID, uc)
        if ok: break
        time.sleep(3)
    else: sys.exit("не дождался скана")
    token = {k: info[k] for k in ("t", "uid", "expire_time", "access_token", "refresh_token")}
    write_private(TOKEN_FILE, {"user_code": uc, "terminal_id": info["terminal_id"], "endpoint": info["endpoint"], "token": token})
    m = Manager(CID, uc, info["terminal_id"], info["endpoint"], token)
    m.update_device_cache()
    devs = [{"id": d.id, "key": d.local_key, "name": d.name, "product_name": d.product_name, "category": d.category}
            for d in m.device_map.values()]
    write_private("devices.json", devs)
    print(f"Готово: токен сохранён, устройств: {len(devs)}")

if __name__ == "__main__":
    main()
