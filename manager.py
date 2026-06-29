import os, time, json, subprocess, tempfile
import redis

REDIS_URL = os.environ["REDIS_URL"]
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
REDIS_USERS_KEY = "users:data"

def log(msg):
    print(f"[MANAGER] {msg}", flush=True)

try:
    r = redis.from_url(REDIS_URL, max_connections=2)
    r.ping()
    log("✅ Connected to Redis.")
except Exception as e:
    log(f"❌ Redis error: {e}")
    r = None

def get_all_users():
    if not r:
        return {}
    try:
        raw = r.hgetall(REDIS_USERS_KEY)
        users = {}
        for email, data_json in raw.items():
            email = email.decode() if isinstance(email, bytes) else email
            data = json.loads(data_json.decode() if isinstance(data_json, bytes) else data_json)
            users[email] = data
        return users
    except Exception as e:
        log(f"❌ Redis read error: {e}")
        return {}

def save_users_batch(users_dict):
    if not r or not users_dict:
        return
    try:
        mapping = {email: json.dumps(data) for email, data in users_dict.items()}
        r.hset(REDIS_USERS_KEY, mapping=mapping)
    except Exception as e:
        log(f"❌ Redis save error: {e}")

def build_xray_config(users):
    config = {
        "log": {"access": "/var/log/xray/access.log", "error": "/var/log/xray/error.log", "loglevel": "warning"},
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True
                }
            }
        },
        "inbounds": [
            {
                "port": 5000, "listen": "127.0.0.1", "protocol": "vless",
                "tag": "vless-inbound",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "ws", "security": "none",
                                   "wsSettings": {"path": "/@nkka404"}}
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
        if data.get("quota_bytes", 0) > 0:
            config["inbounds"][0]["settings"]["clients"].append({"id": data["uuid"], "email": email})
    os.makedirs(os.path.dirname(XRAY_CONFIG_PATH), exist_ok=True)
    with open(XRAY_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    subprocess.run(["pkill", "-f", "xray"], stderr=subprocess.DEVNULL)
    subprocess.Popen(["/usr/local/bin/xray", "run", "-config", XRAY_CONFIG_PATH])
    log("Xray started with Stats API & User policy.")

def get_user_traffic():
    try:
        output = subprocess.check_output(
            ["/usr/local/bin/xray", "api", "statsquery",
             "--server=127.0.0.1:10085", "-pattern", "user"],
            stderr=subprocess.STDOUT
        ).decode()
        data = json.loads(output)
        traffic = {}
        for item in data.get("stat", []):
            name = item["name"]
            value = int(item["value"])
            if "user>>>" in name and ">>>traffic>>>" in name:
                parts = name.split(">>>")
                email = parts[1]
                traffic[email] = traffic.get(email, 0) + value
        return traffic
    except Exception as e:
        log(f"❌ Statsquery error: {e}")
        return {}

os.makedirs("/var/log/xray", exist_ok=True)
open("/var/log/xray/access.log", "a").close()
users = get_all_users()
build_xray_config(users)

last_stats = get_user_traffic()
log("Initial stats captured.")

while True:
    time.sleep(20)
    now = time.time()

    # 1. مزامنة المستخدمين
    try:
        users = get_all_users()
        for email, data in users.items():
            if data.get("quota_bytes", 0) > 0:
                add_json = {"inboundTag": "vless-inbound", "user": {"id": data["uuid"], "email": email}}
                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                    json.dump(add_json, f)
                    tmp_path = f.name
                result = subprocess.run(
                    f"/usr/local/bin/xray api adu --server=127.0.0.1:10085 {tmp_path}",
                    shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                os.unlink(tmp_path)
                if result.returncode == 0:
                    log(f"✅ Added: {email}")
                else:
                    log(f"❌ Add failed {email}: {result.stderr.decode().strip()}")

                # تشخيص: افحص حالة المستخدم بعد الإضافة
                check = subprocess.run(
                    f"/usr/local/bin/xray api inbounduser --server=127.0.0.1:10085 -tag=vless-inbound -email={email}",
                    shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                log(f"USER_CHECK: {check.stdout.decode().strip()}")

    except Exception as e:
        log(f"❌ Sync error: {e}")

    # 2. حساب الاستهلاك
    current_stats = get_user_traffic()
    batch_updates = {}

    if not current_stats:
        continue

    for email, data in users.items():
        cur = current_stats.get(email, 0)
        prev = last_stats.get(email, 0)
        used = cur - prev
        if used < 0:
            used = cur
        if used > 0:
            old = data.get("quota_bytes", 0)
            new = max(old - used, 0)
            if new != old:
                data["quota_bytes"] = new
                batch_updates[email] = data
                log(f"📉 {email}: -{used} bytes, remaining {new} bytes")
            if new <= 0:
                subprocess.run(
                    f"/usr/local/bin/xray api rmu --server=127.0.0.1:10085 -tag=\"vless-inbound\" \"{email}\"",
                    shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                data["banned_until"] = None
                batch_updates[email] = data
                log(f"🚫 Quota finished: {email}")

    if batch_updates:
        save_users_batch(batch_updates)

    last_stats = current_stats
