import os, time, json, subprocess
import redis

REDIS_URL = os.environ["REDIS_URL"]
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_LOG_PATH = "/var/log/xray/access.log"
REDIS_USERS_KEY = "users:data"

# اتصالان لكل حاوية (8 حاويات × 2 = 16 اتصالاً، آمن تحت حد الـ 30)
r = redis.from_url(REDIS_URL, max_connections=2)

def log(msg):
    print(f"[MANAGER] {msg}", flush=True)

def get_all_users():
    raw = r.hgetall(REDIS_USERS_KEY)
    users = {}
    for email, data_json in raw.items():
        try:
            email = email.decode() if isinstance(email, bytes) else email
            data = json.loads(data_json.decode() if isinstance(data_json, bytes) else data_json)
            users[email] = data
        except:
            pass
    return users

def save_users_batch(users_dict):
    """تجميع كل التحديثات في أمر HSET واحد"""
    mapping = {email: json.dumps(data) for email, data in users_dict.items()}
    if mapping:
        r.hset(REDIS_USERS_KEY, mapping=mapping)

def build_xray_config(users):
    config = {
        "log": {"access": XRAY_LOG_PATH, "error": "/var/log/xray/error.log", "loglevel": "warning"},
        "api": {"tag": "api", "services": ["HandlerService"]},
        "inbounds": [
            {
                "port": 5000, "listen": "127.0.0.1", "protocol": "vless",
                "tag": "vless-inbound",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": "/@nkka404"}
                    # تم حذف acceptProxyProtocol لأنه غير مطلوب مع Cloud Run
                }
            },
            {
                "listen": "127.0.0.1", "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"}, "tag": "api-inbound"
            }
        ],
        "routing": {"rules": [{"inboundTag": ["api-inbound"], "outboundTag": "api", "type": "field"}]},
        "outbounds": [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "api"}]
    }
    for email, data in users.items():
        if data.get("banned_until") is None and data.get("quota_bytes", 0) > 0:
            config["inbounds"][0]["settings"]["clients"].append({"id": data["uuid"], "email": email})
    os.makedirs(os.path.dirname(XRAY_CONFIG_PATH), exist_ok=True)
    with open(XRAY_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    subprocess.run(["pkill", "-f", "xray"], stderr=subprocess.DEVNULL)
    subprocess.Popen(["/usr/local/bin/xray", "run", "-config", XRAY_CONFIG_PATH])
    log("Xray started.")

# الإقلاع
os.makedirs("/var/log/xray", exist_ok=True)
open(XRAY_LOG_PATH, "a").close()
users = get_all_users()
build_xray_config(users)
last_sync = time.time()

while True:
    time.sleep(15)
    now = time.time()

    # 1. مزامنة كل 60 ثانية مع Redis
    if now - last_sync > 60:
        users = get_all_users()
        last_sync = now
        # أضف المستخدمين المؤهلين
        for email, data in users.items():
            if data.get("banned_until") is None and data.get("quota_bytes", 0) > 0:
                client_json = json.dumps({"id": data["uuid"], "email": email})
                subprocess.run(
                    f"/usr/local/bin/xray api handler --server=127.0.0.1:10085 AddClient "
                    f"--inbound-tag=vless-inbound --client='{client_json}'",
                    shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # فك الحظر تلقائياً
        for email, data in users.items():
            if data.get("banned_until") and now >= data["banned_until"]:
                if data.get("quota_bytes", 0) > 0:
                    client_json = json.dumps({"id": data["uuid"], "email": email})
                    subprocess.run(
                        f"/usr/local/bin/xray api handler --server=127.0.0.1:10085 AddClient "
                        f"--inbound-tag=vless-inbound --client='{client_json}'",
                        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                data["banned_until"] = None
                save_users_batch({email: data})
                log(f"Unbanned {email}")

    # 2. قراءة السجلات واحتساب الاستهلاك
    if not os.path.exists(XRAY_LOG_PATH):
        continue
    with open(XRAY_LOG_PATH, "r") as f:
        lines = f.readlines()[-300:]
    consumption = {}
    for line in lines:
        if "accepted" in line and "email:" in line:
            parts = line.split()
            try:
                email_idx = parts.index("email:")
                email = parts[email_idx + 1].strip()
                if "sent=" in line and "received=" in line:
                    sent = int(parts[-2].split("=")[1])
                    received = int(parts[-1].split("=")[1])
                    consumption[email] = consumption.get(email, 0) + sent + received
            except:
                continue

    # 3. خصم الرصيد والطرد الفوري (تجميعي)
    batch_updates = {}
    for email, used in consumption.items():
        if email not in users:
            continue
        old = users[email].get("quota_bytes", 0)
        new = max(old - used, 0)
        if new != old:
            users[email]["quota_bytes"] = new
            batch_updates[email] = users[email]
        if new <= 0 and users[email].get("banned_until") is None:
            subprocess.run(
                f"/usr/local/bin/xray api handler --server=127.0.0.1:10085 DelClient "
                f"--inbound-tag=vless-inbound --email={email}",
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            users[email]["banned_until"] = now + 300
            batch_updates[email] = users[email]
            log(f"Quota exhausted: {email}")

    if batch_updates:
        save_users_batch(batch_updates)
