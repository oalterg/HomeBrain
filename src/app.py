import os
import re
import time
import shutil
import secrets
import string
import subprocess
import threading
import json
import hashlib
import hmac
import logging
import shlex
import tempfile
import platform
import requests
import fcntl
from datetime import timedelta
from flask import Flask, render_template, jsonify, request, Response, session, abort, stream_with_context
import migration
import integrations
import recovery
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sock import Sock

app = Flask(__name__)

# --- GPU Detection ---
def has_gpu() -> bool:
    """Check if an AI-capable compute GPU is available.

    Reads HAS_GPU from env when set explicitly. Otherwise probes
    /sys/class/drm/renderD*/device/driver and only accepts compute-capable
    drivers (amdgpu / nvidia / i915 / xe). Display-only GPUs like the
    Raspberry Pi VideoCore (v3d/vc4) are rejected — they expose a render
    node but cannot run llama.cpp inference, and treating them as a GPU
    leaves the dashboard's AI cards stuck in skeleton state.
    """
    val = os.environ.get("HAS_GPU", "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    import glob
    ai_drivers = {"amdgpu", "nvidia", "i915", "xe"}
    for path in glob.glob("/sys/class/drm/renderD*/device/driver"):
        try:
            if os.path.basename(os.readlink(path)) in ai_drivers:
                return True
        except OSError:
            continue
    return False

# Cache for delta-based GPU compute utilisation (avoids relying on gpu_busy_percent,
# which reports spurious 92-100 % on GFX12/RDNA4/Navi44 hardware regardless of load).
_gpu_fdinfo_cache: dict = {"ts": 0.0, "ns": 0, "util_pct": 0}

def _amdgpu_compute_util() -> int:
    """Return GPU compute utilisation % derived from drm-engine-compute delta (fdinfo).

    On GFX12/RDNA4 (e.g. RX 9060 XT / Navi44) the sysfs gpu_busy_percent attribute is
    stuck at ~92-100 % regardless of actual shader load.  Instead we read the cumulative
    drm-engine-compute nanoseconds from /proc/*/fdinfo for every amdgpu client, deduplicate
    by drm-client-id, and compare with the previous sample to derive a true utilisation %.
    Returns 0 on the first call (no prior baseline), then accurate deltas thereafter.
    """
    import glob as _glob
    global _gpu_fdinfo_cache
    seen: set = set()
    total_ns: int = 0
    for fdinfo_dir in _glob.glob("/proc/[0-9]*/fdinfo"):
        try:
            for fd_path in _glob.glob(f"{fdinfo_dir}/*"):
                try:
                    data = open(fd_path).read()
                    if "amdgpu" not in data:
                        continue
                    client_id = compute_ns = None
                    for line in data.splitlines():
                        if line.startswith("drm-client-id:"):
                            client_id = line.split(":", 1)[1].strip()
                        elif line.startswith("drm-engine-compute:"):
                            compute_ns = int(line.split(":", 1)[1].strip().split()[0])
                    if client_id and compute_ns is not None and client_id not in seen:
                        seen.add(client_id)
                        total_ns += compute_ns
                except Exception:
                    pass
        except Exception:
            pass
    now = time.monotonic()
    prev_ts = _gpu_fdinfo_cache["ts"]
    prev_ns = _gpu_fdinfo_cache["ns"]
    if prev_ts > 0:
        dt = now - prev_ts
        dns = total_ns - prev_ns
        if dt > 0 and dns >= 0:
            _gpu_fdinfo_cache["util_pct"] = min(100, round(dns / (dt * 1e9) * 100))
    _gpu_fdinfo_cache["ts"] = now
    _gpu_fdinfo_cache["ns"] = total_ns
    return _gpu_fdinfo_cache["util_pct"]

def get_gpu_stats() -> dict:
    """Read GPU stats from sysfs — AMD amdgpu driver (no rocm-smi dependency)."""
    import glob as _glob
    import re as _re
    result = {"available": False}
    try:
        # /sys/class/drm/card* also matches connector subdirs like card0-HDMI-A-1
        # which expose a `device` symlink but lack the amdgpu mem_info_* attrs.
        # Match only top-level card nodes (cardN) and pick the first one that
        # actually exposes VRAM info.
        card_re = _re.compile(r"/sys/class/drm/card\d+/device$")
        bases = [b for b in _glob.glob("/sys/class/drm/card*/device") if card_re.match(b)]
        base = next((b for b in bases if os.path.exists(f"{b}/mem_info_vram_total")), None)
        if not base:
            return result
        result["util_percent"] = _amdgpu_compute_util()
        vram_used = int(open(f"{base}/mem_info_vram_used").read().strip())
        vram_total = int(open(f"{base}/mem_info_vram_total").read().strip())
        result["vram_used_gb"] = round(vram_used / (1024**3), 1)
        result["vram_total_gb"] = round(vram_total / (1024**3), 1)
        result["vram_percent"] = round(vram_used / vram_total * 100) if vram_total else 0
        temp_paths = _glob.glob(f"{base}/hwmon/hwmon*/temp1_input")
        if temp_paths:
            result["temp_c"] = round(int(open(temp_paths[0]).read().strip()) / 1000, 1)
        result["available"] = True
    except Exception:
        pass
    return result

def get_cpu_temp() -> float | None:
    """CPU temperature in °C.  Checks hwmon first (k10temp on AMD, coretemp
    on Intel), then falls back to thermal_zone (RPi / ARM)."""
    import glob as _glob
    for hwmon in _glob.glob("/sys/class/hwmon/hwmon*/"):
        try:
            name = open(f"{hwmon}name").read().strip()
        except OSError:
            continue
        if name in ("k10temp", "coretemp", "cpu_thermal"):
            try:
                return round(int(open(f"{hwmon}temp1_input").read().strip()) / 1000, 1)
            except (OSError, ValueError):
                continue
    try:
        return round(int(open("/sys/class/thermal/thermal_zone0/temp").read().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def get_models():
    """Load flat model list from platform_models.json."""
    models_path = os.path.join(INSTALL_DIR, 'config', 'platform_models.json')
    with open(models_path) as f:
        data = json.load(f)
    return data.get("models", []), data.get("default", "")

# --- Configuration & Constants ---
# Boot config path detection (filesystem-based)
FACTORY_CONFIG = "/boot/firmware/factory_config.txt" if os.path.isdir("/boot/firmware") else "/opt/homebrain/factory_config.txt"
HOMEBRAIN_ROOT = "/opt/homebrain"
INSTALL_DIR = HOMEBRAIN_ROOT # Alias for backward compatibility if needed
SETUP_STARTED_MARKER = f"{INSTALL_DIR}/.setup_started"
REGISTRATION_MARKER = f"{INSTALL_DIR}/.registration_complete"
ENV_FILE = f"{INSTALL_DIR}/.env"
ENV_TEMPLATE = f"{INSTALL_DIR}/config/.env.template"
COMPOSE_FILE = f"{INSTALL_DIR}/docker-compose.yml"
OVERRIDE_FILE = f"{INSTALL_DIR}/docker-compose.override.yml"
LOG_DIR = "/var/log/homebrain"
BACKUP_DIR = "/mnt/backup"
BACKUP_CRON_FILE = "/etc/cron.d/homebrain-backup"
LEGACY_BACKUP_CRON_FILE = "/etc/cron.d/nextcloud-backup"
PROVISION_SCRIPT = f"{INSTALL_DIR}/scripts/provision.sh"
VERSION_FILE = f"{INSTALL_DIR}/version.json"
REPO_API_URL = "https://api.github.com/repos/oalterg/HomeBrain"
SCRIPT_UPDATE = f"{INSTALL_DIR}/scripts/update.sh"
SCRIPT_UPDATE_DEPS = f"{INSTALL_DIR}/scripts/update-deps.sh"
VERSIONS_FILE = f"{INSTALL_DIR}/config/versions.json"
SCRIPT_BACKUP = f"{INSTALL_DIR}/scripts/backup.sh"
SCRIPT_RESTORE = f"{INSTALL_DIR}/scripts/restore.sh"
SCRIPT_DEPLOY = f"{INSTALL_DIR}/scripts/deploy.sh"
SCRIPT_REDEPLOY = f"{INSTALL_DIR}/scripts/redeploy_tunnels.sh"
SCRIPT_UTILITIES = f"{INSTALL_DIR}/scripts/utilities.sh"
SCRIPT_NUCLEAR   = f"{INSTALL_DIR}/scripts/nuclear_reset.sh"
SCRIPT_ROTATE    = f"{INSTALL_DIR}/scripts/rotate_master_password.sh"
INSTALL_CREDS_PATH = f"{INSTALL_DIR}/install_creds.json"
STAGING_CREDS_PATH = f"{INSTALL_DIR}/.install_creds_staging"

STATUS_FILE = os.path.join(tempfile.gettempdir(), "homebrain_task_status.json")

LOG_FILES = {
    "setup": f"{LOG_DIR}/main_setup.log",
    "backup": f"{LOG_DIR}/backup.log",
    "restore": f"{LOG_DIR}/restore.log",
    "update": f"{LOG_DIR}/manager_update.log",
    "manager": f"{LOG_DIR}/manager.log",
    "nuclear": f"{LOG_DIR}/nuclear_reset.log",
}


def compose_ps_q(service):
    """Container ID of a compose service, or "" if not created/running."""
    try:
        return subprocess.check_output(
            ["docker", "compose", "-f", COMPOSE_FILE, "ps", "-q", service],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return ""

JOURNAL_SERVICES = {"openclaw", "llama-server"}

# --- Logging Configuration (Global & Robust) ---
# Configure logging immediately so Gunicorn or Dev server both use it
log_file = LOG_FILES["manager"]

# --- Security: Factory Auth ---
SECRET_KEY_FILE = f"{INSTALL_DIR}/.secret_key"

def load_persistent_secret_key():
    """Ensures sessions remain valid across service restarts."""
    try:
        # Open file in read/write mode, creating if not exists
        # Open file in append binary mode to ensure existence and acquiring lock
        with open(SECRET_KEY_FILE, 'ab+') as f:
            # Acquire exclusive lock to prevent race conditions between workers
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                key = f.read()

                if len(key) >= 32:
                    return key
                
                # Generate new key
                key = secrets.token_bytes(32)
                with open(SECRET_KEY_FILE, 'wb') as fw:
                    fw.write(key)
                os.chmod(SECRET_KEY_FILE, 0o600)
                return key
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logging.error(f"Secret Key Error: {e}. Using ephemeral key.")
        return secrets.token_bytes(32)

app.secret_key = load_persistent_secret_key()


def _enforce_vault_signups_lockdown():
    """If at least one vault user already exists, force VAULT_SIGNUPS_ALLOWED=false
    and (if currently true) restart the vaultwarden container so the env takes
    effect. Runs once at manager startup as a defence-in-depth measure: someone
    hand-editing .env back to SIGNUPS_ALLOWED=true would otherwise expose the
    public vault to fresh-account creation. Idempotent and silent on first
    boot when no users exist yet."""
    try:
        with open(ENV_FILE) as f:
            env = {}
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v.strip("'\"")
    except OSError:
        return

    if env.get("VAULT_ENABLED", "true").lower() == "false":
        return

    db_pass = env.get("VAULT_DB_PASSWORD")
    db_name = env.get("VAULT_DB_NAME", "vaultwarden")
    db_user = env.get("VAULT_DB_USER", "vaultwarden_user")
    if not db_pass:
        return

    try:
        db_cid = compose_ps_q("db")
        if not db_cid:
            return
        out = subprocess.check_output(
            ["docker", "exec", "-e", f"MYSQL_PWD={db_pass}", db_cid,
             "mariadb", "-u", db_user, "-N", "-s", "-e",
             f"SELECT COUNT(*) FROM `{db_name}`.users;"],
            stderr=subprocess.DEVNULL, timeout=4,
        ).decode().strip()
    except Exception:
        return

    user_count = int(out) if out.isdigit() else 0
    if user_count == 0:
        return  # First boot, leave signups open until the wizard creates a user

    if env.get("VAULT_SIGNUPS_ALLOWED", "true").lower() == "true":
        logging.warning(
            "Vault has %d user(s) but SIGNUPS_ALLOWED=true — locking down.",
            user_count,
        )
        update_env_var("VAULT_SIGNUPS_ALLOWED", "false")
        try:
            subprocess.run(
                ["docker", "compose", "-f", COMPOSE_FILE, "--env-file", ENV_FILE,
                 "up", "-d", "vaultwarden"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
        except Exception:
            pass


# Run the lockdown check after the env loader is defined. We can't call it at
# module import time because update_env_var is defined later — register a
# Flask deferred startup hook instead.
@app.before_request
def _vault_signups_check_once():
    if getattr(app, "_vault_signups_checked", False):
        return
    app._vault_signups_checked = True
    try:
        _enforce_vault_signups_lockdown()
    except Exception as e:
        logging.warning("Vault signups lockdown check failed: %s", e)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    SESSION_REFRESH_EACH_REQUEST=True
)

@app.context_processor
def inject_platform():
    arch = platform.machine()
    if arch == "aarch64":
        return {"platform": {"product_name": "HomeCloud", "product_suffix": "Cloud"}}
    return {"platform": {"product_name": "HomeBrain", "product_suffix": "Brain"}}

def get_factory_password():
    """Reads the factory password securely from config."""
    try:
        with open(FACTORY_CONFIG, 'r') as f:
            for line in f:
                if line.startswith("FACTORY_PASSWORD="):
                    return line.strip().split("=", 1)[1]
    except: pass
    return None

# 1. Define Filter to suppress noisy polling logs
class AccessLogFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # Filter out frequent polling endpoints to prevent log flooding
        if any(x in msg for x in ["GET /api/task_status", "GET /api/status", "GET /api/logs/"]):
            return False
        return True

logging.basicConfig(filename=log_file, level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Apply filter to relevant loggers
for logger_name in ["werkzeug", "gunicorn.access", "root"]:
    logging.getLogger(logger_name).addFilter(AccessLogFilter())


task_lock = threading.Lock()
current_task_status = {"status": "idle", "message": "", "log_type": "setup"}

def write_status(status):
    try:
        # Use mkstemp for secure file creation (prevents race conditions)
        # Use system temp dir to ensure write permissions regardless of user (root/www-data)
        fd, temp_path = tempfile.mkstemp(dir=tempfile.gettempdir(), text=True)
        with os.fdopen(fd, 'w') as f:
            json.dump(status, f)
        os.chmod(temp_path, 0o644)  # Set safe perms
        os.rename(temp_path, STATUS_FILE) # Atomic replacement
    except Exception as e:
        logging.error(f"Failed to write status file: {e}. Using in-memory fallback.")
        global current_task_status
        current_task_status = status  # Fallback to global if file fails

def read_status():
    # 1. Try reading from file (Active Background Task)
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
         logging.error(f"Failed to read status file: {e}")

    # 2. Check Physical State (No active task file)
    # If credentials exist, we are in the Handover Phase.
    # We report 'success' to ensure the dashboard/installer shows the completion state.
    if os.path.exists(INSTALL_CREDS_PATH):
        return {"status": "success", "message": "Setup complete"}
    
    # 3. Default: System Active / Idle
    return {"status": "idle", "message": "", "log_type": "setup"}

# Initialize status on startup
try:
    # On startup, any 'running' task in the status file is dead.
    # We remove the file to force a reset to the true persistent state (Idle or Handover).
    if os.path.exists(STATUS_FILE):
        logging.info("Startup: Cleaning up previous task status file.")
        os.remove(STATUS_FILE)
except Exception as e:
    logging.error(f"Startup status cleanup failed: {e}")
    # Ensure in-memory fallback is clean
    current_task_status = {"status": "idle", "message": "", "log_type": "setup"}

@app.route("/api/task_status")
def get_task_status():
    return jsonify(read_status())

# Rate limits live in the stack's Redis (loopback-published) so all gunicorn
# workers share one counter — in-memory storage counted per worker, turning
# "5 per minute" into 15. Uses remote IP for identification. If Redis is down
# (e.g. during boot) the limiter falls back to per-worker memory rather than
# failing requests.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["2000 per minute"], # High limit to prevent dashboard polling blocks
    storage_uri="redis://127.0.0.1:6379/1",
    in_memory_fallback_enabled=True,
)

# Mount the OpenClaw integrations module — adds /api/integrations/* routes
# for the Connections dashboard card and the bearer-token endpoints used
# by the homebrain-self MCP server. See src/integrations.py.
integrations.register_integrations(app, limiter)

# WebSocket support for the /openclaw reverse-proxy. Requires gunicorn to
# run with a gevent worker class (see config/homebrain-manager.service).
sock = Sock(app)

# --- Security: Split-Horizon Auth Middleware ---
@app.before_request
def security_middleware():
    # 1. Static Resources
    if request.endpoint == 'static' or request.path == '/favicon.ico':
        return
    
    # 2. Universal Authentication Check
    # Require valid session for ALL traffic (Welcome, Setup, Dashboard, API).
    if session.get('authenticated'):
        return

    # 3. Allow Login Handler
    if request.path == '/login':
        return

    # 3b. Bearer-token endpoints for the OpenClaw self-MCP. The integrations
    # module checks the bearer token itself; we just have to bypass the
    # session gate so it gets a chance to.
    if request.path.startswith('/api/integrations/self/'):
        return

    # 3c. Recovery-phrase break-glass. These must work while the user is locked
    # out, so they bypass the session gate exactly like /login. The handlers
    # enforce their own protections (LAN-origin, strict rate-limit, the phrase
    # itself). Only the pre-auth pair is whitelisted; status/regenerate stay
    # gated because they are management actions for an already-logged-in user.
    if request.path in ('/api/recovery/verify', '/api/recovery/reset'):
        return

    # 4. Default: Block & Show Login Gate
    abort(401)


@app.route('/login', methods=['POST'])
@limiter.limit("5 per minute") 
def login():
    password = request.form.get('password')
    # Determine which password to enforce based on state
    if is_setup_complete():
        # Post-Setup: Secure Generated Master Password
        env = get_env_config()
        target_pass = env.get('MANAGER_PASSWORD')
    else:
        # Pre-Setup: Factory Sticker Password
        target_pass = get_factory_password()

    if target_pass and hmac.compare_digest((password or "").encode(), target_pass.encode()):
        session['authenticated'] = True
        session.permanent = True  # Keep session active across browser restarts
        return jsonify({"status": "success", "redirect": "/"})
    
    time.sleep(2) # Mitigation against timing attacks
    return jsonify({"error": "Invalid Password"}), 401


@app.route("/api/setup/credentials")
def get_one_time_credentials():
    creds_file = INSTALL_CREDS_PATH
    if os.path.exists(creds_file):
        try:
            with open(creds_file, "r") as f:
                data = json.load(f)
            # Check if cloud registration is configured in factory settings
            factory_conf = get_factory_config()
            data["cloud_enabled"] = bool(factory_conf.get("REGISTRAR_URL"))
            return jsonify(data)
        except Exception as e:
            logging.error(f"Failed to read creds file: {e}")
    
    return jsonify({"error": "Credentials not found."}), 410  # Use 410 Gone for permanent absence

@app.route("/api/setup/cleanup_credentials", methods=["POST"])
def cleanup_credentials():
    """Called by the frontend after successfully rendering the success page."""
    
    # 1. Enforcement: Check if Registration is required but missing
    factory_conf = get_factory_config()
    if factory_conf.get("REGISTRAR_URL") and not os.path.exists(REGISTRATION_MARKER):
         return jsonify({"error": "Mandatory email registration not completed."}), 403

    # 2. Cleanup
    if os.path.exists(INSTALL_CREDS_PATH):
        os.remove(INSTALL_CREDS_PATH)
    if os.path.exists(REGISTRATION_MARKER):
        os.remove(REGISTRATION_MARKER)
    session.pop('authenticated', None) # Force re-login with new master password
    
    # Now, start the remaining profile tunnel containers
    subprocess.run(["chmod", "+x", SCRIPT_UTILITIES])
    cmd = f"bash {SCRIPT_UTILITIES} activate_tunnels >> {LOG_FILES['setup']} 2>&1"
    threading.Thread(
        target=run_background_task, args=("Activating Tunnels", cmd, "setup")
    ).start()

    return jsonify({"status": "ok"})

# --- Helpers ---
def get_local_version():
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"channel": "unknown", "ref": "unknown", "updated_at": "unknown"}

def run_background_task(task_name, command, log_type):
    # The one deliberate shell=True in this codebase: callers hand over
    # internally-built command strings with log redirection (>> file 2>&1) and
    # shlex.quote() at every interpolation. Never pass request data here
    # unquoted.
    status = {
             "status": "running",
             "message": f"{task_name} in progress...",
             "log_type": log_type,
         }
    write_status(status)

    try:
        result = subprocess.run(
            command, shell=True, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.stdout:
            app.logger.info(f"[{task_name}] {result.stdout}")
        status["status"] = "success"
        status["message"] = f"{task_name} completed successfully."
        write_status(status)
    except subprocess.CalledProcessError as e:
        if e.output:
            app.logger.error(f"[{task_name} failed] {e.output}")
        status["status"] = "error"
        status["message"] = f"{task_name} failed. Check logs."
        write_status(status)
    except Exception as e:
        status["status"] = "error"
        status["message"] = str(e)
        write_status(status)

    time.sleep(10)
    current = read_status()
    if current["status"] != "running":
        current["status"] = "idle"
        write_status(current)


def get_factory_config():
    config = {}
    if os.path.exists(FACTORY_CONFIG):
        with open(FACTORY_CONFIG, "r") as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    val_str = value.strip()
                    if val_str.startswith(("'", '"')) and val_str.endswith(val_str[0]):
                        inner = val_str[1:-1]
                        if val_str[0] == "'":
                            inner = inner.replace("'\\''", "'")
                        config[key] = inner
                    else:
                        config[key] = val_str
    return config


def get_env_config():
    config = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    val_str = value.strip()
                    if val_str.startswith(("'", '"')) and val_str.endswith(val_str[0]):
                        inner = val_str[1:-1]
                        if val_str[0] == "'":
                            inner = inner.replace("'\\''", "'")
                        config[key] = inner
                    else:
                        config[key] = val_str
    return config


def update_env_var(key, value):
    try:
        # If value is None, remove the line
        if value is None:
            subprocess.run(["sed", "-i", f"/^{key}=/d", ENV_FILE])
            return True

        # Avoid shell=True for grep
        rc = subprocess.call(["grep", "-q", f"^{key}=", ENV_FILE])
        
        # Escape single quotes and wrap value in single quotes 
        # to prevent execution when sourced by bash (e.g. VAR=$(payload))
        safe_val_bash = str(value).replace("'", "'\\''")
        quoted_val = f"'{safe_val_bash}'"

        if rc == 0:
            # Use | delimiter for sed. Escape \ first (sed treats \X as escape
            # sequences in replacement), then escape | delimiter.
            sed_val = quoted_val.replace("\\", "\\\\").replace("|", "\\|")
            subprocess.run(["sed", "-i", f"s|^{key}=.*|{key}={sed_val}|", ENV_FILE])
        else:
            with open(ENV_FILE, "r+") as f:
                content = f.read()
                if content and not content.endswith("\n"):
                    f.write("\n")
            
            with open(ENV_FILE, "a") as f:
                f.write(f"{key}={quoted_val}\n")
        return True
    except:
        return False


def is_setup_complete():
    return os.path.exists(f"{INSTALL_DIR}/.setup_complete")


def is_setup_started():
    return os.path.exists(SETUP_STARTED_MARKER)


def is_local_mode():
    """Returns True when running in LAN-only mode.
    Local if Pangolin credentials are absent, or if user explicitly set DEPLOYMENT_MODE=local.
    When credentials are present, tunnel is on by default (DEPLOYMENT_MODE defaults to remote).
    """
    env = get_env_config()
    # No Pangolin credentials — always local
    if not all(env.get(k) for k in ["NEWT_ID", "NEWT_SECRET", "PANGOLIN_DOMAIN"]):
        return True
    # Credentials present but user opted out
    return env.get("DEPLOYMENT_MODE", "remote") == "local"


def get_lan_ip():
    """Returns the primary LAN IP of this machine."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def _is_lan_request():
    """True when the browser reached the dashboard via a LAN address."""
    try:
        host = request.host.split(":", 1)[0].lower()
    except RuntimeError:
        return False
    if host in ("localhost", "127.0.0.1", "[::1]"):
        return True
    if host.endswith(".local"):
        return True
    if re.match(r"^10\.", host):
        return True
    if re.match(r"^192\.168\.", host):
        return True
    if re.match(r"^172\.(1[6-9]|2[0-9]|3[01])\.", host):
        return True
    return False


def calculate_sha256(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


# --- Route: Trigger Initial Setup ---
@app.route("/start_setup", methods=["POST"])
@limiter.limit("3 per minute") # Strict limit on setup triggering
def start_setup():
    if is_setup_complete():
        return jsonify({"error": "Setup already complete"}), 400

    # 1. Mark setup as started
    with open(SETUP_STARTED_MARKER, "w") as f:
        f.write(str(int(time.time())))

    # 2. Bootstrap .env file
    # Do not override env if redeploying, ensure we start from the robust template
    if not os.path.exists(ENV_FILE):
        if os.path.exists(ENV_TEMPLATE):
            shutil.copyfile(ENV_TEMPLATE, ENV_FILE)
        else:
            with open(ENV_FILE, "w") as f:
                f.write("# Auto-generated by HomeBrain Manager\n")
        # Harden: Ensure file is only readable by root
        os.chmod(ENV_FILE, 0o600)
        # Set Passwords & Critical Defaults
        update_env_var("NEXTCLOUD_DATA_DIR", os.path.join(os.environ.get("HOMEBRAIN_HOME", "/home/homebrain"), "nextcloud-data"))
        update_env_var("NEXTCLOUD_ADMIN_USER", "admin")


    # Write deployment mode chosen in the setup wizard
    data = request.get_json(silent=True) or {}
    deployment_mode = data.get("deployment_mode", "local")
    update_env_var("DEPLOYMENT_MODE", deployment_mode)

    # Map credentials: prefer form submission, fall back to factory config
    factory = get_factory_config()

    if deployment_mode == "remote":
        newt_id       = data.get("pangolin_id")       or factory.get("NEWT_ID", "")
        newt_secret   = data.get("pangolin_secret")   or factory.get("NEWT_SECRET", "")
        pan_endpoint  = data.get("pangolin_endpoint") or factory.get("PANGOLIN_ENDPOINT", "")
        pan_domain    = data.get("pangolin_domain")   or factory.get("PANGOLIN_DOMAIN", "")

        update_env_var("NEWT_ID",           newt_id)
        update_env_var("NEWT_SECRET",       newt_secret)
        update_env_var("PANGOLIN_ENDPOINT", pan_endpoint)

        if pan_domain:
            update_env_var("PANGOLIN_DOMAIN",            pan_domain)
            update_env_var("MANAGER_DOMAIN",             pan_domain)
            update_env_var("NEXTCLOUD_TRUSTED_DOMAINS",  f"nc.{pan_domain}")
            update_env_var("HA_TRUSTED_DOMAINS",         f"ha.{pan_domain}")
            update_env_var("VAULT_TRUSTED_DOMAINS",      f"vault.{pan_domain}")
            update_env_var("VAULT_DOMAIN",               f"https://vault.{pan_domain}")

    env_config = get_env_config()

    # 1. Get master password
    master_pass = env_config.get('MASTER_PASSWORD')

    # 2. Generation (B1: memorable hyphen-joined word passphrase). Falls back to
    #    the legacy 16-char random token if the wordlist is unavailable, so a
    #    missing asset can never block provisioning.
    if not master_pass:
        try:
            master_pass = recovery.generate_password()
        except recovery.RecoveryError as e:
            logging.warning(f"Recovery wordlist unavailable, using random password: {e}")
            alphabet = string.ascii_letters + string.digits
            master_pass = ''.join(secrets.choice(alphabet) for _ in range(16))

    # 2b. Mint an INDEPENDENT recovery phrase (B2). Stored only as a scrypt hash
    #     in .env; the plaintext is shown once on the success page (carried in
    #     install_creds.json, wiped by cleanup_credentials) and never persisted.
    #     Best-effort: a failure here must not abort setup.
    recovery_phrase = None
    try:
        recovery_phrase = recovery.generate_phrase()
        record = recovery.build_recovery_record(
            recovery_phrase, recovery.DEFAULT_PHRASE_WORDS, time.time())
        for k, v in record.items():
            update_env_var(k, v)
    except Exception as e:
        logging.warning(f"Could not mint recovery phrase during setup: {e}")
        recovery_phrase = None

    # 3. Write to install_creds.json
    creds_data = {
        "username": "admin",
        "password": master_pass,
        "recovery_phrase": recovery_phrase,
        "domain": env_config.get('PANGOLIN_DOMAIN'),
        "generated_at": time.time()
    }
    # Write to staging first. deploy.sh will move it to final path on success.
    with open(STAGING_CREDS_PATH, 'w') as f:
        json.dump(creds_data, f)

    # 4. Assign master password to all services
    for key in ["MASTER_PASSWORD", "MANAGER_PASSWORD", "NEXTCLOUD_ADMIN_PASSWORD",
                "MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "HA_ADMIN_PASSWORD"]:
        update_env_var(key, master_pass)

    # 5. Trigger deploy
    cmd = f"bash {SCRIPT_DEPLOY} >> {LOG_FILES['setup']} 2>&1"
    threading.Thread(
        target=run_background_task, args=("Initial Setup", cmd, "setup")
    ).start()
    # Session already established by login
    return jsonify({"status": "started"})

@app.route('/api/cloud/register', methods=['POST'])
@limiter.limit("5 per minute")
def register_cloud():
    # 1. Verify user is authenticated (Factory Password during setup, or Admin Token after)
    # The auth_gate handles the check, ensuring only owners can trigger this.
    
    email = request.json.get('email')
    if not email: return jsonify({"error": "Email is required"}), 400
    
    # 2. Get Device ID
    config = get_factory_config()
    registrar_url = config.get("REGISTRAR_URL")
    registrar_secret = config.get("REGISTRAR_SECRET")
    device_id = config.get("NEWT_ID", "unknown")
    
    if not registrar_url:
        return jsonify({"error": "Cloud registration not configured on this device."}), 400

    # 3. Call Registrar
    try:
        # Timeout is crucial for robustness so we don't hang the UI
        r = requests.post(
            registrar_url, 
            json={"email": email, "device_id": device_id},
            headers={"Authorization": f"Bearer {registrar_secret}"},
            timeout=10
        )
        if r.status_code in [200, 201]:
            # Mark registration as complete
            with open(REGISTRATION_MARKER, 'w') as f:
                f.write(str(int(time.time())))
            # Persist Email for Dashboard visibility
            update_env_var("CLOUD_EMAIL", email)
            return jsonify({"status": "success", "message": "Invitation sent! Check your email."})
        else:
            # Attempt to parse specific error from Worker JSON
            try:
                worker_error = r.json().get("error", r.text)
            except:
                worker_error = r.text
            
            logging.error(f"Registrar Error {r.status_code}: {worker_error}")
            return jsonify({"error": f"Registration failed: {worker_error}"}), 502
    except Exception as e:
        logging.error(f"Registrar Connection Exception: {e}")
        return jsonify({"error": "Could not connect to registrar service."}), 503

# --- Route: Adopt Existing Drive ---
@app.route("/api/drives/mount", methods=["POST"])
@limiter.limit("5 per minute")
def mount_drive():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    drive_path = request.json.get("path")
    if not drive_path or "mmcblk" in drive_path:
        return jsonify({"error": "Invalid drive"}), 400

    # 1. Determine the actual device/partition path to use
    target_path = drive_path
    
    # Check if this is a whole disk device (e.g., /dev/sda)
    # If it is a whole disk, append '1' to check the first partition (e.g., /dev/sda1)
    # This is a common convention, though 'p1' for NVMe/MMC can also occur.
    # The lsblk output in list_drives provides only whole disk paths, so we check for common partition suffixes.
    if not drive_path.endswith(('0','1','2','3','4','5','6','7','8','9')) and not drive_path.endswith('p'):
        # Try appending '1'
        target_path = drive_path + '1'
        
        # Verify if the partition actually exists
        if not os.path.exists(target_path):
             # For some systems (e.g., NVMe/MMC), the partition might be /dev/nvme0n1p1.
             # We rely on the user selecting the whole disk, so if /dev/sdX1 doesn't exist, we fallback
             # to trying the original whole-disk path, as it *might* have been formatted without partitions.
             target_path = drive_path
             
    # 2. Get UUID and FSType from the target path
    try:
        # Check UUID/FSType of the identified partition/device
        uuid = subprocess.check_output(
            ["blkid", "-o", "value", "-s", "UUID", target_path]).decode().strip()
        fstype = subprocess.check_output(
            ["blkid", "-o", "value", "-s", "TYPE", target_path]).decode().strip()

        if not uuid:
            # Fallback to the original whole-disk path if the partition check failed
            if target_path != drive_path:
                uuid = subprocess.check_output(
                    ["blkid", "-o", "value", "-s", "UUID", drive_path]).decode().strip()
                fstype = subprocess.check_output(
                    ["blkid", "-o", "value", "-s", "TYPE", drive_path]).decode().strip()
                if uuid:
                     target_path = drive_path # Use the whole disk path if it has a UUID
            
        if not uuid:
            # If still no UUID, it means the disk or its first partition is unformatted.
            return jsonify({"error": "No UUID found. Drive is likely unformatted or partitioned incorrectly."}), 400

        if fstype not in ["ext4", "ext3", "xfs"]:
            return jsonify({"error": f"Unsupported filesystem ({fstype})"}), 400
    except:
        return jsonify({"error": "Could not read drive info (blkid failed)"}), 500

    # 3. Use the discovered UUID and FSType to update fstab
    # We use target_path for unmounting but UUID for fstab entry.
    cmd = (
        f"umount {shlex.quote(target_path)} || true; "
        f"mkdir -p {BACKUP_DIR}; "
        # Remove any existing entry for the backup dir to avoid conflicts
        f"sed -i '\\|{BACKUP_DIR}|d' /etc/fstab; "
        # Add new entry using the validated UUID
        f'echo "UUID={uuid} {BACKUP_DIR} {fstype} defaults,nofail 0 2" >> /etc/fstab; '
        f"mount -a"
    )

    threading.Thread(
        target=run_background_task, args=("Mount Existing Drive", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})


# --- Routes: Main UI ---
@app.route("/")
def index():
    # Robustness: If credentials exist, we are in the "Handover Phase".
    # We MUST show the installing/success view so the user can claim them,
    # even if the setup is marked as complete.
    if os.path.exists(INSTALL_CREDS_PATH):
        return render_template("installing.html", handover_ready=True, has_gpu=has_gpu())

    if not is_setup_complete():
        # If setup is running, show progress (No auth required for this specific view state)
        if is_setup_started():
            return render_template("installing.html", has_gpu=has_gpu())
        return render_template("welcome.html", config=get_factory_config(), has_gpu=has_gpu())

    factory = get_factory_config()
    env = get_env_config()

    # Deployment mode
    local = is_local_mode()
    deployment_mode = "local" if local else "remote"

    # Compute service URLs.  When the browser reached us on a LAN address
    # (IP, .local mDNS) serve local URLs so the links work even when
    # the tunnel is down — regardless of the configured deployment mode.
    lan_access = _is_lan_request()
    if local or lan_access:
        host = request.host.split(":", 1)[0] if lan_access else get_lan_ip()
        nc_https_port = env.get("NC_LOCAL_HTTPS_PORT", "8444")
        nc_url = f"https://{host}:{nc_https_port}"
        ha_url = f"http://{host}:8123"
    else:
        nc_url = f"https://{env.get('NEXTCLOUD_TRUSTED_DOMAINS', '')}"
        ha_url = f"https://{env.get('HA_TRUSTED_DOMAINS', '')}"

    # Determine Tunnel Provider Mode
    # If any CF token exists, we are in Cloudflare mode
    cf_mode = bool(env.get("CF_TOKEN_NC") or env.get("CF_TOKEN_HA"))

    # Check if Pangolin is custom
    is_custom_pangolin = False
    if not cf_mode:
        # Check if critical tunnel params differ from factory
        is_custom_pangolin = (
            env.get("PANGOLIN_ENDPOINT") != factory.get("PANGOLIN_ENDPOINT") or
            env.get("PANGOLIN_DOMAIN") != factory.get("PANGOLIN_DOMAIN")
        )
    # Feature Flag: Cloud/Email features only active if Registrar is provisioned
    cloud_enabled = bool(factory.get("REGISTRAR_URL"))

    return render_template(
        "dashboard.html",
        deployment_mode=deployment_mode,
        nc_url=nc_url,
        ha_url=ha_url,
        main_domain=env.get("PANGOLIN_DOMAIN"),
        nc_domain=env.get("NEXTCLOUD_TRUSTED_DOMAINS"),
        ha_domain=env.get("HA_TRUSTED_DOMAINS"),
        manager_domain=env.get("MANAGER_DOMAIN"),
        tunnel={
            "factory": factory,
            "current": env,
            "mode": "cloudflare" if cf_mode else "pangolin",
            "is_custom_pangolin": is_custom_pangolin,
        },
        cloud_enabled=cloud_enabled,
        cloud_account={"email": env.get("CLOUD_EMAIL", ""), "tier": "Trial (100GB)"},
        has_gpu=has_gpu()
    )


# --- Routes: Deployment Mode ---
@app.route("/api/deployment-mode")
def deployment_mode_api():
    """Returns current deployment mode and tunnel domain (if any)."""
    env = get_env_config()
    local = is_local_mode()
    tunnel_domain = None
    if not local:
        tunnel_domain = env.get("PANGOLIN_DOMAIN") or env.get("NEXTCLOUD_TRUSTED_DOMAINS") or None
    return jsonify({"mode": "local" if local else "remote", "tunnelDomain": tunnel_domain})


HEALTH_FILE = "/var/lib/homebrain/health.json"

@app.route("/api/health")
def health_status():
    """Latest health-check report, written by scripts/healthcheck.py from the
    homebrain-health.timer. Drives the dashboard banner; the push channel
    (WhatsApp/Telegram) is handled by the checker itself."""
    try:
        with open(HEALTH_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return jsonify({"overall": "unknown", "checks": []})
    # A stale report (timer dead > 2h) is itself a signal — surface it.
    if time.time() - data.get("ts", 0) > 2 * 3600:
        data["overall"] = "unknown"
    return jsonify(data)


# --- Routes: API Status ---
@app.route("/api/status")
def system_status():
    services = {
        "nextcloud": "stopped",
        "db": "stopped",
        "homeassistant": "stopped",
        "tunnel": "stopped",
        "vaultwarden": "stopped",
        "caddy": "stopped",
    }
    try:
        # Docker Services Check
        # Get profiles dynamically from common.sh for consistency
        profiles = subprocess.check_output(
            ["bash", "-c",
             f"source {INSTALL_DIR}/scripts/common.sh; load_env; "
             "echo $(get_tunnel_profiles) $(get_vault_profiles)"],
        ).decode().strip()

        compose_cmd = ["docker", "compose", "-f", COMPOSE_FILE, "--env-file", ENV_FILE]
        if os.path.exists(OVERRIDE_FILE):
            compose_cmd += ["-f", OVERRIDE_FILE]
        compose_cmd += profiles.split() + ["ps", "--format", "{{.Service}}:{{.State}}:{{.Health}}"]

        out = subprocess.check_output(compose_cmd).decode()
        tunnel_status = "stopped"

        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 2:
                svc = parts[0]
                state = parts[1]
                health = parts[2] if len(parts) > 2 else ""

                status = "running" if "running" in state else "stopped"
                if "unhealthy" in health:
                    status = "unhealthy"
                elif "starting" in health:
                    status = "starting"

                if svc in services:
                    services[svc] = status

                # Consolidate Tunnel Status
                if svc == "newt" or svc.startswith("cloudflared"):
                    if status == "running":
                        tunnel_status = "running"

        services["tunnel"] = tunnel_status

        # Maintenance Mode Check
        try:
            m_check = subprocess.check_output(
                ["docker", "compose", "-f", COMPOSE_FILE, "exec", "-u", "www-data",
                 "nextcloud", "php", "occ", "maintenance:mode"],
            ).decode()
            services["maintenance_mode"] = (
                "enabled" if "enabled" in m_check else "disabled"
            )
        except:
            services["maintenance_mode"] = "unknown"

        # System Resources Stats (CPU/RAM/Root Disk)
        try:
            # CPU load
            def get_cpu_times():
                with open('/proc/stat') as f:
                    line = f.readline().strip()
                    fields = line.split()[1:]  # user nice system idle ...
                    return [int(x) for x in fields[:4]]  # user, nice, system, idle

            times1 = get_cpu_times()
            time.sleep(0.5)
            times2 = get_cpu_times()

            deltas = [t2 - t1 for t1, t2 in zip(times1, times2)]
            total_delta = sum(deltas)
            if total_delta == 0:
                cpu_load = 0.0
            else:
                idle_delta = deltas[3]
                used = total_delta - idle_delta
                cpu_load = round(100.0 * used / total_delta, 1)
            services["cpu_load"] = cpu_load
            cpu_temp = get_cpu_temp()
            if cpu_temp is not None:
                services["cpu_temp"] = cpu_temp

            # RAM
            # Because we used quoted EOF, $2 and $3 are preserved literally for awk here:
            mem_line = next(
                line for line in subprocess.check_output(["free", "-m"]).decode().splitlines()
                if line.startswith("Mem:")
            ).split()
            mem_info = mem_line[1:3]  # total, used
            if len(mem_info) == 2:
                total_mem = int(mem_info[0])
                used_mem = int(mem_info[1])
                services["ram_percent"] = round((used_mem / total_mem) * 100, 1)
                services["ram_text"] = f"{used_mem}MB / {total_mem}MB"

            # Root Disk
            total, used, free = shutil.disk_usage("/")
            services["root_total_gb"] = round(total / (1024**3), 1)
            services["root_free_gb"] = round(free / (1024**3), 1)
            services["root_percent"] = round((used / total) * 100, 1)

        except Exception as e:
            services["sys_error"] = str(e)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if has_gpu():
        services["gpu"] = get_gpu_stats()

    return jsonify(services)

@app.route("/api/logs/<log_target>")
def get_logs(log_target):
    # If target is a systemd service, read from journal
    if log_target in JOURNAL_SERVICES:
        try:
            cmd = [
                "journalctl", "-u", log_target,
                "-n", "200", "--no-pager", "--output=short-iso"
            ]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=10).decode("utf-8", errors="replace")
        except subprocess.CalledProcessError as e:
            output = e.output.decode("utf-8", errors="replace")
        except Exception as e:
            output = f"[Error reading journal for {log_target}: {e}]"
        return output

    # If target is in our known file list, read the file
    if log_target in LOG_FILES:
        filepath = LOG_FILES[log_target]
        if os.path.exists(filepath):
            return subprocess.check_output(["tail", "-n", "100", filepath]).decode()
        return "Log file empty or not found."

    # Otherwise, assume it is a docker service name
    try:
        # Security: Allow only alphanumeric service names
        if not log_target.isalnum():
            return "Invalid service name."

        # Use docker compose logs
        cmd_list = ["docker", "compose", "-f", COMPOSE_FILE, "logs", "--tail=100", log_target]
        output = subprocess.check_output(
            cmd_list, stderr=subprocess.STDOUT
        ).decode()
        return output
    except subprocess.CalledProcessError as e:
        return "Failed to fetch docker logs. Service might not be running."
    except Exception as e:
        return f"Error: {str(e)}"
    
@app.route("/api/logs/client", methods=["POST"])
@limiter.limit("20 per minute") # Prevent client loops from DDOSing logs
def client_log():
    """Receives logs/errors from the frontend dashboard."""
    data = request.json
    level = data.get("level", "INFO").upper()
    message = data.get("message", "No message")
    
    # Sanitize inputs
    if level not in ["INFO", "WARNING", "ERROR", "DEBUG"]:
        level = "INFO"
    
    # Log to the main manager log
    logging.log(getattr(logging, level), f"[Frontend] {message}")
    return jsonify({"status": "ok"})

# --- Routes: Drives & Storage ---
@app.route("/api/drives")
def list_drives():
    try:
        root_dev = (
            subprocess.check_output(["findmnt", "-n", "-o", "SOURCE", "/"])
            .decode()
            .strip()
        )
        # Layer 1: Robustly strip partition suffix to get the whole-disk name.
        # re.sub handles NVMe (nvme0n1p7→nvme0n1), SATA (sda3→sda), eMMC (mmcblk0p1→mmcblk0).
        root_disk = re.sub(r'p?\d+$', '', root_dev)

        # Include children (partitions) so we can inspect their mount points.
        # TRAN exposes the bus transport — "usb", "sata", "nvme", etc. The
        # `rm` (removable) bit alone is unreliable: many USB SSD enclosures
        # (P3-1TB, Samsung T5/T7, etc.) report rm=0 because the underlying
        # SATA controller doesn't advertise removable media. TRAN=usb is
        # the truthful signal for an externally-attached drive.
        output = subprocess.check_output(
            ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL,RM,MOUNTPOINT,TRAN"]
        ).decode()
        data = json.loads(output)

        # Check explicit mount point for backup
        backup_mount_source = ""
        try:
            # Returns e.g. /dev/sda1
            backup_mount_source = (
                subprocess.check_output(["findmnt", "-n", "-o", "SOURCE", BACKUP_DIR])
                .decode()
                .strip()
            )
        except Exception:
            pass  # not mounted

        system_mounts = {'/', '/boot', '/boot/efi', '/opt/homebrain'}

        candidates = []
        for dev in data["blockdevices"]:
            dev_name = f"/dev/{dev['name']}"

            # Filter out system pseudo-devices (zram, loop, ram)
            if dev["type"] != "disk":
                continue
            if dev_name.startswith("/dev/zram") or dev_name.startswith("/dev/loop") or dev_name.startswith("/dev/ram"):
                continue

            # Layer 2: Accept a drive if EITHER it advertises removable
            # (rm bit set) OR it is attached via USB transport. The rm
            # bit is unreliable on USB SSD enclosures that wrap SATA —
            # they report rm=0 because the underlying SATA controller
            # doesn't advertise removable media. The transport type is
            # set by the kernel based on the bus and is reliable.
            # Older util-linux (<2.37) emits `rm` as a string ("0"/"1");
            # newer versions emit a JSON bool. Accept both.
            rm = dev.get("rm")
            tran = dev.get("tran") or ""
            is_removable = rm in (True, 1, "1")
            is_usb = tran.lower() == "usb"
            if not (is_removable or is_usb):
                continue

            # Layer 1 (continued): Skip the disk that contains the root filesystem.
            if dev_name == root_disk or root_disk.startswith(dev_name):
                continue

            # Layer 3: Skip any disk whose partitions host critical system mount points.
            # Belt-and-suspenders safety net in case RM or name matching ever misses.
            child_mounts = {c.get("mountpoint") for c in dev.get("children", []) if c.get("mountpoint")}
            if child_mounts & system_mounts:
                continue

            # Robust check: Is this drive (or a partition on it) mounted at /mnt/backup?
            is_backup = False
            if backup_mount_source and dev_name in backup_mount_source:
                is_backup = True

            candidates.append(
                {
                    "path": dev_name,
                    "size": dev["size"],
                    "model": dev.get("model", "Unknown"),
                    "is_backup": is_backup,
                }
            )

        return jsonify(candidates)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/drives/format", methods=["POST"])
@limiter.limit("3 per minute")
def format_drive():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    drive_path = request.json.get("path")
    if not drive_path or "mmcblk" in drive_path:
        return jsonify({"error": "Invalid drive"}), 400

    # Quote drive path to prevent command injection
    safe_path = shlex.quote(drive_path)
    # Use && so any failed step aborts the chain (mkfs, blkid, mount). udevadm settle
    # ensures the kernel has published the new UUID before we read it.
    cmd = (
        f"set -e; "
        f"umount {safe_path}* 2>/dev/null || true; "
        f"wipefs -a {safe_path} && "
        f"mkfs.ext4 -F -L 'NextcloudBackup' {safe_path} && "
        f"udevadm settle && "
        f"mkdir -p {BACKUP_DIR} && "
        f"UUID=$(blkid -o value -s UUID {safe_path}) && "
        f'[ -n "$UUID" ] && '
        f"sed -i '\\|{BACKUP_DIR}|d' /etc/fstab && "
        f'echo "UUID=$UUID {BACKUP_DIR} ext4 defaults,nofail 0 2" >> /etc/fstab && '
        f"mount -a"
    )

    threading.Thread(
        target=run_background_task, args=("Format Drive", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})


# --- Routes: Backup Config & Stats ---
@app.route("/api/backup/stats")
def backup_stats():
    # Check if mounted
    if not os.path.ismount(BACKUP_DIR):
        return jsonify({"mounted": False, "free_gb": 0, "total_gb": 0, "percent": 0})
    try:
        total, used, free = shutil.disk_usage(BACKUP_DIR)
        return jsonify(
            {
                "mounted": True,
                "free_gb": round(free / (1024**3), 2),
                "total_gb": round(total / (1024**3), 2),
                "used_gb": round(used / (1024**3), 2),
                "percent": round((used / total) * 100, 1),
            }
        )
    except:
        return jsonify({"mounted": False, "error": "Disk check failed"})


@app.route("/api/backup/config", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def backup_config():
    if request.method == "GET":
        env = get_env_config()
        return jsonify(
            {
                "retention": env.get("BACKUP_RETENTION", "8"),
                "hour": env.get("BACKUP_HOUR", "3"),
                "minute": env.get("BACKUP_MINUTE", "0"),
                "day_week": env.get("BACKUP_DAY_WEEK", "*"),
                "day_month": env.get("BACKUP_DAY_MONTH", "*"),
            }
        )

    # POST: Save Settings
    data = request.json
    retention = data.get("retention", "8")
    hour = data.get("hour", "3")
    minute = data.get("minute", "0")
    day_week = data.get("day_week", "*")
    day_month = data.get("day_month", "*")

    # Validate Inputs
    try:
        # Ensure numeric values are integers within valid ranges
        if not (0 <= int(minute) <= 59) or not (0 <= int(hour) <= 23):
            raise ValueError("Invalid time format")
        if day_month != "*" and not (1 <= int(day_month) <= 31):
            raise ValueError("Invalid day of month")
        if day_week != "*" and not (0 <= int(day_week) <= 6):
            raise ValueError("Invalid day of week")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Update .env for persistence
    update_env_var("BACKUP_RETENTION", retention)
    update_env_var("BACKUP_HOUR", hour)
    update_env_var("BACKUP_MINUTE", minute)
    update_env_var("BACKUP_DAY_WEEK", day_week)
    update_env_var("BACKUP_DAY_MONTH", day_month)

    # Apply as a persistent systemd timer (fires missed runs on next boot —
    # plain cron silently skips backups the box sleeps through). The helper
    # also removes the legacy cron files it replaces.
    try:
        subprocess.run(
            ["bash", SCRIPT_UTILITIES, "backup_timer"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return jsonify({"status": "success"})
    except subprocess.CalledProcessError as e:
        logging.error(f"backup_timer failed: {e.stderr}")
        return jsonify({"error": "Failed to apply backup schedule"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/offsite", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def backup_offsite():
    if request.method == "GET":
        env = get_env_config()
        return jsonify(
            {
                "enabled": env.get("OFFSITE_ENABLED", "false") == "true",
                "type": env.get("OFFSITE_TYPE", ""),
                "host": env.get("OFFSITE_HOST", ""),
                "user": env.get("OFFSITE_USER", ""),
                "has_pass": bool(env.get("OFFSITE_PASS", "")),
                "path": env.get("OFFSITE_PATH", ""),
            }
        )

    data = request.json
    enabled = bool(data.get("enabled"))
    remote_type = data.get("type", "")
    host = data.get("host", "").strip()
    user = data.get("user", "").strip()
    password = data.get("pass", "")
    path = data.get("path", "").strip()

    if enabled:
        if remote_type not in ["sftp", "webdav", "s3"]:
            return jsonify({"error": "Invalid remote type"}), 400
        if not host:
            return jsonify({"error": "Host is required"}), 400

    update_env_var("OFFSITE_ENABLED", "true" if enabled else "false")
    update_env_var("OFFSITE_TYPE", remote_type)
    update_env_var("OFFSITE_HOST", host)
    update_env_var("OFFSITE_USER", user)
    if password:  # an empty field means "keep the saved password"
        update_env_var("OFFSITE_PASS", password)
    update_env_var("OFFSITE_PATH", path)

    if enabled and not shutil.which("rclone"):
        try:
            subprocess.run(
                ["apt-get", "install", "-y", "rclone"],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except Exception:
            return jsonify({"error": "Could not install rclone"}), 500
    return jsonify({"status": "success"})


@app.route("/api/backup/offsite/test", methods=["POST"])
@limiter.limit("5 per minute")
def backup_offsite_test():
    try:
        result = subprocess.run(
            ["bash", SCRIPT_UTILITIES, "offsite_test"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Connection test timed out"}), 500
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        msg = lines[-1].removeprefix("[ERROR] ") if lines else "Connection test failed"
        return jsonify({"error": msg}), 400
    return jsonify({"status": "success"})


@app.route("/api/backup/replica", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def backup_replica():
    nc_domain = get_env_config().get("NEXTCLOUD_TRUSTED_DOMAINS", "")
    url = f"https://{nc_domain}/remote.php/dav/files/replica" if nc_domain else ""

    if request.method == "GET":
        result = subprocess.run(
            ["bash", SCRIPT_UTILITIES, "replica_status"],
            capture_output=True, text=True, timeout=30,
        )
        out = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if result.returncode != 0:
            return jsonify({"error": "Could not read replica status"}), 500
        if out.startswith("enabled"):
            used = out.split("used=", 1)[1] if "used=" in out else "?"
            return jsonify({"enabled": True, "url": url, "user": "replica", "used": used})
        return jsonify({"enabled": False})

    action = request.json.get("action", "")
    if action == "enable":
        result = subprocess.run(
            ["bash", SCRIPT_UTILITIES, "replica_enable"],
            capture_output=True, text=True, timeout=60,
        )
        # The password is shown once to be copied into the sending box's
        # Off-site form — it is never stored on this box.
        for line in result.stdout.splitlines():
            if line.startswith("REPLICA_PASS="):
                return jsonify({"status": "success", "url": url, "user": "replica",
                                "pass": line.split("=", 1)[1]})
        return jsonify({"error": "Could not enable the replica account"}), 500
    if action == "disable":
        result = subprocess.run(
            ["bash", SCRIPT_UTILITIES, "replica_disable"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": "Could not remove the replica account"}), 500
        return jsonify({"status": "success"})
    return jsonify({"error": "Invalid action"}), 400


# --- Routes: Backup & Restore Execution ---
@app.route("/api/backup/now", methods=["POST"])
@limiter.limit("3 per minute")
def trigger_backup():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    # 'full' = Database + NC Data + NC Config + HA Config
    # 'data_only' = NC Data + HA Config (No Database, No NC Config)
    strategy = request.json.get("strategy", "full")

    # Validate strategy
    if strategy not in ["full", "data_only"]:
        return jsonify({"error": "Invalid strategy"}), 400

    # We delegate strictly to the bash script to ensure locking and consistent logic
    # Quote the strategy argument
    cmd = f"bash {SCRIPT_BACKUP} --strategy {shlex.quote(strategy)} >> {LOG_FILES['backup']} 2>&1"

    label = "Full System Backup" if strategy == "full" else "Data-Only Backup"
    
    threading.Thread(
        target=run_background_task, args=(label, cmd, "backup")
    ).start()
    return jsonify({"status": "started"})


@app.route("/api/backups/list")
def list_backups():
    backups = []
    if os.path.exists(BACKUP_DIR):
        for f in os.listdir(BACKUP_DIR):
            if f.endswith(".tar.gz") or f.endswith(".tar.gz.gpg"):
                path = os.path.join(BACKUP_DIR, f)
                try:
                    size = os.path.getsize(path) / (1024 * 1024)
                    # Infer type from filename injected by backup.sh
                    if "data_only" in f:
                        btype = "Data Only"
                    elif "_system_" in f:
                        btype = "System snapshot"
                    else:
                        btype = "Full System"
                    backups.append({"name": f, "size": f"{size:.2f} MB",
                                    "type": btype,
                                    "encrypted": f.endswith(".gpg")})
                except:
                    pass
    backups.sort(key=lambda x: x["name"], reverse=True)
    return jsonify(backups)


@app.route("/api/restore", methods=["POST"])
@limiter.limit("3 per minute")
def trigger_restore():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    filename = request.json.get("filename")
    if not filename or "/" in filename:
        return jsonify({"error": "Invalid filename"}), 400

    full_path = os.path.join(BACKUP_DIR, filename)

    # Optional passphrase for encrypted archives made under a DIFFERENT master
    # password (pre-rotation or from another box). Passed via a root-only temp
    # file so the secret never appears in argv; restore.sh deletes it after
    # reading. Without it, restore.sh uses the current MASTER_PASSWORD.
    pass_env = ""
    passphrase = request.json.get("passphrase") or ""
    if passphrase:
        fd, pass_path = tempfile.mkstemp(dir=tempfile.gettempdir())
        with os.fdopen(fd, "w") as f:
            f.write(passphrase)
        os.chmod(pass_path, 0o600)
        pass_env = f"RESTORE_PASSPHRASE_FILE={shlex.quote(pass_path)} "

    # restore.sh handles auto-detection of content (HA vs NC vs DB)
    # Quote full path
    cmd = f"{pass_env}bash {SCRIPT_RESTORE} {shlex.quote(full_path)} --no-prompt >> {LOG_FILES['restore']} 2>&1"
    task_name = "System Restore"

    threading.Thread(
        target=run_background_task, args=(task_name, cmd, "restore")
    ).start()
    return jsonify({"status": "started"})


@app.route('/api/openclaw/backup-status')
def openclaw_backup_status():
    """Return OpenClaw backup configuration and workspace stats."""
    openclaw_dir = os.path.join(os.environ.get("HOMEBRAIN_HOME", "/home/homebrain"), ".openclaw")
    config_path = os.path.join(openclaw_dir, "openclaw.json")
    workspace_path = os.path.join(openclaw_dir, "workspace")

    config_present = os.path.isfile(config_path)

    workspace_size_mb = None
    if os.path.isdir(workspace_path):
        try:
            result = subprocess.check_output(
                ["du", "-sm", workspace_path], stderr=subprocess.DEVNULL
            )
            workspace_size_mb = int(result.decode().split()[0])
        except Exception:
            pass

    warn_threshold = int(os.environ.get("BACKUP_OPENCLAW_SIZE_WARN_MB", "500"))

    return jsonify({
        "config_present": config_present,
        "workspace_size_mb": workspace_size_mb,
        "workspace_size_warning": (
            workspace_size_mb is not None and workspace_size_mb > warn_threshold
        ),
        "warn_threshold_mb": warn_threshold,
        "backup_workspace": os.environ.get("BACKUP_OPENCLAW_WORKSPACE", "true").lower() == "true",
        "exclude_caches": os.environ.get("BACKUP_OPENCLAW_EXCLUDE_CACHES", "false").lower() == "true",
    })


@app.route('/api/backup/openclaw-settings', methods=['POST'])
def update_openclaw_backup_settings():
    """Update OpenClaw backup settings."""
    data = request.get_json(silent=True) or {}
    if "include_workspace" in data:
        val = "true" if data["include_workspace"] else "false"
        update_env_var("BACKUP_OPENCLAW_WORKSPACE", val)
        os.environ["BACKUP_OPENCLAW_WORKSPACE"] = val
    if "exclude_caches" in data:
        val = "true" if data["exclude_caches"] else "false"
        update_env_var("BACKUP_OPENCLAW_EXCLUDE_CACHES", val)
        os.environ["BACKUP_OPENCLAW_EXCLUDE_CACHES"] = val
    return jsonify({"success": True})


# --- Routes: Tunnel Management ---
@app.route("/api/tunnel", methods=["POST"])
@limiter.limit("5 per minute")
def update_tunnel():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    data = request.json
    action = data.get("action")

    # Ensure we are in Pangolin mode: Clear CF tokens
    update_env_var("CF_TOKEN_NC", None)
    update_env_var("CF_TOKEN_HA", None)

    if action == "revert":
        factory = get_factory_config()
        update_env_var("PANGOLIN_ENDPOINT", factory.get("PANGOLIN_ENDPOINT", ""))
        update_env_var("NEWT_ID", factory.get("NEWT_ID", ""))
        update_env_var("NEWT_SECRET", factory.get("NEWT_SECRET", ""))
        
        # Revert Domain Logic
        main_dom = factory.get("PANGOLIN_DOMAIN", "")
        update_env_var("PANGOLIN_DOMAIN", main_dom)
        update_env_var("MANAGER_DOMAIN", main_dom)
        update_env_var("NEXTCLOUD_TRUSTED_DOMAINS", f"nc.{main_dom}" if main_dom else "")
        update_env_var("HA_TRUSTED_DOMAINS", f"ha.{main_dom}" if main_dom else "")

    else:
        update_env_var("PANGOLIN_ENDPOINT", data.get("endpoint"))
        update_env_var("NEWT_ID", data.get("id"))
        update_env_var("NEWT_SECRET", data.get("secret"))
        
        # Consolidate Domain Logic
        if data.get("main_domain"):
            main_dom = data.get("main_domain")
            update_env_var("PANGOLIN_DOMAIN", main_dom)
            update_env_var("MANAGER_DOMAIN", main_dom)
            update_env_var("NEXTCLOUD_TRUSTED_DOMAINS", f"nc.{main_dom}")
            update_env_var("HA_TRUSTED_DOMAINS", f"ha.{main_dom}")

    # Trigger deploy script to update stack logic
    subprocess.run(["chmod", "+x", SCRIPT_REDEPLOY])
    cmd = f"bash {SCRIPT_REDEPLOY} >> {LOG_FILES['setup']} 2>&1"
    threading.Thread(
        target=run_background_task, args=("Update Tunnel (Pangolin)", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})


# --- Routes: Tunnel Management (Cloudflare) ---
@app.route("/api/tunnel/cloudflare", methods=["POST"])
@limiter.limit("5 per minute")
def update_tunnel_cloudflare():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    domain = request.json.get("domain")
    service = request.json.get("service")  # 'nc' or 'ha'
    token = request.json.get("token")

    if not token or not service:
        return jsonify({"error": "Missing token or service definition"}), 400

    # Write Token to .env
    if service == "nc":
        update_env_var("NEXTCLOUD_TRUSTED_DOMAINS", domain)
        update_env_var("CF_TOKEN_NC", token)
    elif service == "ha":
        update_env_var("HA_TRUSTED_DOMAINS", domain)
        update_env_var("CF_TOKEN_HA", token)

    # NOTE: We do not explicitly unset PANGOLIN vars here because
    # deploy.sh prioritizes CF tokens if present.
    # This allows a cleaner "Revert" later.

    subprocess.run(["chmod", "+x", SCRIPT_REDEPLOY])
    cmd = f"bash {SCRIPT_REDEPLOY} >> {LOG_FILES['setup']} 2>&1"
    threading.Thread(
        target=run_background_task, args=("Update Tunnel (Cloudflare)", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})


@app.route("/api/tunnel/revert", methods=["POST"])
@limiter.limit("3 per minute")
def revert_tunnel_provider():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    # To revert to factory (Pangolin)
    # 1. Clear CF tokens
    update_env_var("CF_TOKEN_NC", None)
    update_env_var("CF_TOKEN_HA", None)

    # 2. Restore Factory Pangolin vars
    factory = get_factory_config()
    update_env_var("PANGOLIN_ENDPOINT", factory.get("PANGOLIN_ENDPOINT", ""))
    update_env_var("NEWT_ID", factory.get("NEWT_ID", ""))
    update_env_var("NEWT_SECRET", factory.get("NEWT_SECRET", ""))
    # Derive trusted domains from the factory PANGOLIN_DOMAIN rather than the
    # legacy NC_DOMAIN/HA_DOMAIN keys: provision.sh (remote mode) rewrites
    # factory_config without those keys, so reading them here would blank the
    # trusted domains on revert. Mirrors the /api/tunnel revert + start_setup map.
    main_dom = factory.get("PANGOLIN_DOMAIN", "")
    update_env_var("PANGOLIN_DOMAIN", main_dom)
    update_env_var("MANAGER_DOMAIN", main_dom)
    update_env_var("NEXTCLOUD_TRUSTED_DOMAINS", f"nc.{main_dom}" if main_dom else "")
    update_env_var("HA_TRUSTED_DOMAINS", f"ha.{main_dom}" if main_dom else "")
    update_env_var("VAULT_TRUSTED_DOMAINS", f"vault.{main_dom}" if main_dom else "")
    update_env_var("VAULT_DOMAIN", f"https://vault.{main_dom}" if main_dom else "")

    subprocess.run(["chmod", "+x", SCRIPT_REDEPLOY])
    cmd = f"bash {SCRIPT_REDEPLOY} >> {LOG_FILES['setup']} 2>&1"
    threading.Thread(
        target=run_background_task, args=("Revert to Factory Settings", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})


# --- Routes: Maintenance & Updates ---
@app.route("/api/maintenance/mode", methods=["POST"])
@limiter.limit("10 per minute")
def set_maintenance():
    mode = request.json.get("mode")
    flag = "--on" if mode == "on" else "--off"
    try:
        subprocess.check_call(
            ["docker", "compose", "-f", COMPOSE_FILE, "exec", "-u", "www-data",
             "nextcloud", "php", "occ", "maintenance:mode", flag],
        )
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Routes: Hardware / Zigbee ---
@app.route("/api/hardware/serial")
def list_serial_devices():
    try:
        # Find USB and ACM devices
        devices = []
        for dev in os.listdir("/dev"):
            if dev.startswith("ttyUSB") or dev.startswith("ttyACM"):
                devices.append(f"/dev/{dev}")
        return jsonify(devices)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/manager/zigbee", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def manage_zigbee():
    """
    GET: Returns the currently configured Zigbee device from the override file.
    POST: Updates the override file and restarts Home Assistant in the background.
    """

    # --- GET: Persistence Check ---
    if request.method == "GET":
        current_device = "none"
        if os.path.exists(OVERRIDE_FILE):
            try:
                with open(OVERRIDE_FILE, "r") as f:
                    content = f.read()
                    # Look for common serial device patterns in the mapped volume
                    if "/dev/ttyUSB0" in content: current_device = "/dev/ttyUSB0"
                    elif "/dev/ttyACM0" in content: current_device = "/dev/ttyACM0"
                    elif "/dev/ttyAMA0" in content: current_device = "/dev/ttyAMA0"
            except Exception as e:
                logging.error(f"Failed to read override file: {e}")
        return jsonify({"current": current_device})

    # --- POST: Configuration Update ---
    data = request.json
    device = data.get("device")
    valid_devices = ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyAMA0", "none"]

    if device not in valid_devices:
        return jsonify({"error": "Invalid device path"}), 400

    try:
        if device == "none":
            if os.path.exists(OVERRIDE_FILE):
                os.remove(OVERRIDE_FILE)
            message = "Zigbee device removed."
        else:
            # Generate the override YAML. 
            # This ensures HA gets the device even if the main compose doesn't have it.
            yaml_content = f"""
services:
  homeassistant:
    devices:
      - {device}:{device}
"""
            with open(OVERRIDE_FILE, "w") as f:
                f.write(yaml_content.strip())
            message = f"Zigbee device set to {device}."

        # Robust Restart Logic:
        # We must tell Docker to use both files if the override exists, 
        # otherwise it won't see the new mapping during the restart.
        def restart_ha():
            compose_cmd = ["docker", "compose", "-f", COMPOSE_FILE]
            if os.path.exists(OVERRIDE_FILE):
                compose_cmd.extend(["-f", OVERRIDE_FILE])
            
            # Use 'up -d' instead of 'restart' because 'up' recreates 
            # the container if the hardware mapping (config) changed.
            compose_cmd.extend(["up", "-d", "homeassistant"])
            
            logging.info(f"Executing: {' '.join(compose_cmd)}")
            subprocess.run(compose_cmd, check=False)

        # Fire and forget to keep the UI responsive
        threading.Thread(target=restart_ha, daemon=True).start()

        return jsonify({
            "status": "success",
            "message": f"{message} Home Assistant is restarting..."
        })

    except Exception as e:
        logging.error(f"Zigbee update error: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/upgrade", methods=["POST"])
@limiter.limit("2 per minute") # Very expensive operation
def trigger_upgrade():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    # 1. Fetch Active Profiles (Critical for Tunnels)
    # We reuse the bash logic to ensure consistency with deploy.sh
    try:
        profiles = subprocess.check_output(
            ["bash", "-c",
             f"source {INSTALL_DIR}/scripts/common.sh; load_env; get_tunnel_profiles"],
        ).decode().strip()
    except Exception as e:
        logging.error(f"Failed to fetch profiles: {e}")
        # Fail safe: don't proceed if we can't determine the profile, 
        # otherwise we might start the stack without the tunnel.
        return jsonify({"error": "Could not determine system profile. Upgrade aborted."}), 500

    # 2. Construct Docker Arguments
    safe_env = shlex.quote(ENV_FILE)
    safe_compose = shlex.quote(COMPOSE_FILE)
    safe_log = shlex.quote(LOG_FILES["setup"])
    
    # Handle Override File
    compose_args = f"-f {safe_compose}"
    if os.path.exists(OVERRIDE_FILE):
        safe_override = shlex.quote(OVERRIDE_FILE)
        compose_args += f" -f {safe_override}"

    # 3. Build the Command Chain
    # Note: 'profiles' variable contains flags (e.g. --profile cloudflare), so it cannot be quoted as a single string.
    cmd = (
        f"echo '=== Starting System & Stack Upgrade ===' > {safe_log}; "
        
        # Step A: OS Updates
        f"echo '[1/4] Updating System Packages...' >> {safe_log}; "
        "export DEBIAN_FRONTEND=noninteractive; "
        f"apt-get update >> {safe_log} 2>&1; "
        f"apt-get upgrade -y >> {safe_log} 2>&1; "
        f"apt-get autoremove -y >> {safe_log} 2>&1; "

        # Step B: Docker Pull (Updates Images)
        f"echo '[2/4] Pulling Docker Images...' >> {safe_log}; "
        f"docker compose --env-file {safe_env} {compose_args} {profiles} pull >> {safe_log} 2>&1; "

        # Step C: Docker Up (Recreates Containers)
        f"echo '[3/4] Restarting Stack...' >> {safe_log}; "
        f"docker compose --env-file {safe_env} {compose_args} {profiles} up -d --remove-orphans >> {safe_log} 2>&1; "
        
        # Step D: Cleanup
        f"echo '[4/4] Cleaning up...' >> {safe_log}; "
        f"docker image prune -f >> {safe_log} 2>&1; "
        
        f"echo '=== Upgrade Complete ===' >> {safe_log}"
    )

    threading.Thread(
        target=run_background_task, args=("System Upgrade", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})

@app.route("/api/manager/check_update", methods=["GET"])
def check_manager_update():
    channel = request.args.get("channel", "stable") # 'stable' or 'beta'
    local_ver = get_local_version()
    
    try:
        remote_ref = ""
        message = ""
        update_available = False

        if channel == "stable":
            # Check Latest Release
            resp = requests.get(f"{REPO_API_URL}/releases/latest", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                remote_ref = data.get("tag_name", "")
                # Compare tags (Simple string comparison, semantic versioning library is better but heavy)
                if remote_ref != local_ver.get("ref"):
                    update_available = True
                    message = f"New Release Available: {remote_ref}"
                else:
                    message = f"Up to date ({remote_ref})"
            else:
                 # Fallback if no releases exist yet
                 message = "No releases found."

        else: # Beta / Dev
            # Check Main Branch Commit
            resp = requests.get(f"{REPO_API_URL}/commits/main", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                remote_ref = data.get("sha", "")[:7] # Short SHA
                if remote_ref != local_ver.get("ref"):
                    update_available = True
                    message = f"New Beta Commit: {remote_ref}"
                else:
                    message = f"Beta up to date ({remote_ref})"
            else:
                message = "Failed to fetch beta info."

        return jsonify({
            "available": update_available,
            "message": message,
            "current_ref": local_ver.get("ref"),
            "target_ref": remote_ref,
            "channel": channel
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/manager/update", methods=["POST"])
@limiter.limit("3 per minute")
def do_manager_update():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    data = request.json
    channel = data.get("channel", "stable")
    target_ref = data.get("target_ref", "main")

    # Ensure update script is executable
    if not os.path.exists(SCRIPT_UPDATE):
         # Fallback: Try to chmod if it exists, or error out
         # In a broken state, one might curl the script here first
         return jsonify({"error": "Update script missing. Re-install required."}), 500

    subprocess.run(["chmod", "+x", SCRIPT_UPDATE])

    # Snapshot current pinned dep versions before update.sh runs the file sync.
    # The actual bump detection and install happens inside update.sh after rsync.
    old_versions = {}
    try:
        with open(VERSIONS_FILE) as f:
            old_versions = json.load(f)
    except Exception:
        pass

    # Fire and forget thread, as the service will restart
    def _run_update():
        with open(LOG_FILES["update"], "w") as log:
            log.write(f"Starting Manager Update ({channel})...\n")
            log.flush()
            subprocess.run(["bash", SCRIPT_UPDATE, channel, target_ref],
                           stdout=log, stderr=subprocess.STDOUT)
    threading.Thread(target=_run_update).start()

    return jsonify({
        "status": "started",
        "message": f"Updating to {channel} {target_ref}. Interface will restart.",
        "current_versions": old_versions,
    })

# --- Auto-Update Logic ---
def perform_first_boot_update():
    """Checks connectivity and updates the manager before allowing setup."""
    if is_setup_complete() or is_setup_started():
        return

    marker = f"{INSTALL_DIR}/.first_boot_update_done"
    if os.path.exists(marker):
        return

    logging.info("First Boot: Checking for Critical Updates...")
    try:
        # Simple connectivity check
        requests.get("https://github.com", timeout=5)
        
        # Run Update Script
        logging.info("Network up. Running update.sh...")
        # We run this synchronously to block startup until updated
        subprocess.run([SCRIPT_UPDATE, "stable", "main"], check=True)
        
        # Mark done
        with open(marker, "w") as f:
            f.write(str(time.time()))
            
        logging.info("First Boot Update Complete. Restarting service...")
        # Self-restart
        os._exit(0) 
    except Exception as e:
        logging.error(f"First boot update skipped (No Network?): {e}")

@app.route("/api/ai/models", methods=["GET"])
@limiter.limit("30 per minute")
def get_ai_models():
    """Returns available AI models (GPU-gated)."""
    if not has_gpu():
        return jsonify({"error": "no_gpu", "message": "AI features require a GPU"}), 503
    try:
        models_file = os.path.join(INSTALL_DIR, "config", "platform_models.json")
        with open(models_file, "r") as f:
            data = json.load(f)
        return jsonify({
            "models": data.get("models", []),
            "llama_server": data.get("llama_server", {}),
            "whisper_models": data.get("whisper", {}).get("models", [])
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ai/model", methods=["POST"])
@limiter.limit("5 per minute")
def set_ai_model():
    """Persist the user's model selection to .env for the AI setup scripts (GPU-gated)."""
    if not has_gpu():
        return jsonify({"error": "no_gpu", "message": "AI features require a GPU"}), 503

    data = request.json
    model_id = data.get("model_id")
    if not model_id:
        return jsonify({"error": "model_id required"}), 400

    # Look up model in registry
    models_file = os.path.join(INSTALL_DIR, "config", "platform_models.json")
    try:
        with open(models_file, "r") as f:
            all_models = json.load(f)
        models = all_models.get("models", [])
        model = next((m for m in models if m["id"] == model_id), None)
        if not model:
            return jsonify({"error": f"Unknown model: {model_id}"}), 400

        server_defaults = all_models.get("llama_server", {})
        update_env_var("AI_MODEL_ID", model["id"])
        update_env_var("AI_MODEL_FILENAME", model["filename"])
        update_env_var("AI_MODEL_URL", model["url"])
        update_env_var("AI_MODEL_MIN_SIZE", str(model["min_size_bytes"]))
        return jsonify({"status": "ok", "model": model["id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ai/model/switch", methods=["POST"])
@limiter.limit("3 per minute")
def switch_ai_model():
    """Switch to a different AI model (updates .env, restarts services) (GPU-gated)."""
    if not has_gpu():
        return jsonify({"error": "no_gpu", "message": "AI features require a GPU"}), 503
    if current_task_status["status"] == "running":
        return jsonify({"error": "A task is already running"}), 409

    data = request.json
    model_id = data.get("model_id")
    if not model_id:
        return jsonify({"error": "model_id required"}), 400

    models_file = os.path.join(INSTALL_DIR, "config", "platform_models.json")
    try:
        with open(models_file, "r") as f:
            all_models = json.load(f)
        models = all_models.get("models", [])
        model = next((m for m in models if m["id"] == model_id), None)
        if not model:
            return jsonify({"error": f"Unknown model: {model_id}"}), 400

        server_defaults = all_models.get("llama_server", {})
        update_env_var("AI_MODEL_ID", model["id"])
        update_env_var("AI_MODEL_FILENAME", model["filename"])
        update_env_var("AI_MODEL_URL", model["url"])
        update_env_var("AI_MODEL_MIN_SIZE", str(model["min_size_bytes"]))

        cmd = f"bash {shlex.quote(SCRIPT_UTILITIES)} switch_model >> {LOG_FILES['setup']} 2>&1"
        threading.Thread(
            target=run_background_task, args=(f"Switch AI model to {model_id}", cmd, "setup")
        ).start()

        return jsonify({"status": "started", "model": model_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/system/capabilities", methods=["GET"])
@limiter.limit("30 per minute")
def system_capabilities():
    """Returns system capabilities (GPU availability, AI features, etc.)."""
    return jsonify({"has_gpu": has_gpu(), "ai_enabled": has_gpu()})

@app.route("/api/system/config", methods=["GET"])
@limiter.limit("30 per minute")
def get_system_config():
    """Returns status of Watchdog, PCI, and Cron."""
    try:
        # Call utilities.sh system_status
        result = subprocess.check_output(
            ["bash", SCRIPT_UTILITIES, "system_status"]
        ).decode().strip()
        # Parse the JSON returned by bash
        return Response(result, mimetype='application/json')
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/system/config", methods=["POST"])
@limiter.limit("5 per minute")
def update_system_config():
    """Generic endpoint to toggle system settings."""
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    data = request.json
    feature = data.get("feature") # watchdog, cron, pci, openclaw
    action = data.get("action")   # enable/disable or gen3/gen2

    # Whitelist features and actions
    if feature not in ["watchdog", "cron", "pci", "openclaw"]:
        return jsonify({"error": "Invalid feature"}), 400

    # Pi-only features (check by boot config path existence)
    if feature in ["watchdog", "pci"] and not os.path.isdir("/boot/firmware"):
        return jsonify({"error": f"{feature} is only available on Raspberry Pi"}), 400

    safe_action = shlex.quote(action) if action else ""

    cmd = ""
    label = ""
    
    if feature == "watchdog":
        # Validate action for watchdog
        if action not in ["enable", "disable"]: return jsonify({"error": "Invalid action"}), 400
        cmd = f"bash {SCRIPT_UTILITIES} watchdog {safe_action}"
        label = f"Configure Watchdog ({action})"
    elif feature == "cron":
        cmd = f"bash {SCRIPT_UTILITIES} cron"
        label = "Configure Nextcloud Cron"
    elif feature == "pci":
        # Validate action for pci
        target = "gen3" if action == "enable" else "gen2" 
        cmd = f"bash {SCRIPT_UTILITIES} pci {target}"
        label = f"Configure PCIe ({target})"
    elif feature == "openclaw":
        update_env_var("ENABLE_OPENCLAW", "true" if action == "enable" else "false")
        if action == "enable":
            cmd = f"bash {shlex.quote(SCRIPT_UTILITIES)} setup_ai"
            label = "Install & Start AI Stack"
        else:
            cmd = f"bash {shlex.quote(SCRIPT_UTILITIES)} stop_ai"
            label = "Disable AI Stack"

    # Execute
    cmd += f" >> {LOG_FILES['setup']} 2>&1"
    threading.Thread(
        target=run_background_task, args=(label, cmd, "setup")
    ).start()
    
    return jsonify({"status": "started"})

# --- Reverse-proxy: OpenClaw Control UI ---
# Goal: single authenticated origin for the user. The master-password session
# (security_middleware) gates access; we then forward the request to the local
# OpenClaw gateway, injecting the gateway bearer token server-side so the
# token never lands in the browser. OpenClaw is configured with
# gateway.controlUi.basePath = "/openclaw" so its own routing matches the
# proxy mount point — no body rewriting, no fragile path translation.
_OPENCLAW_PROXY_HOST = "127.0.0.1"
_OPENCLAW_PROXY_PORT = int(os.environ.get("OPENCLAW_PORT", "18789"))
_OPENCLAW_PROXY_PREFIX = "/openclaw"
_OPENCLAW_CONFIG_PATH = os.path.join(
    os.environ.get("HOMEBRAIN_HOME", "/home/homebrain"), ".openclaw/openclaw.json"
)
# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1). Adding `host`
# because the upstream needs its own Host header, not the manager's external
# one, or the gateway's Origin check rejects the connection.
_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}
_openclaw_token_cache = {"token": None, "loaded_at": 0.0}

def _openclaw_token():
    """Read the OpenClaw bearer token from openclaw.json (cached 60 s).
    The token is generated by patch_openclaw_config from MASTER_PASSWORD,
    so it survives manager restarts but rotates when MASTER_PASSWORD does.
    """
    now = time.time()
    if _openclaw_token_cache["token"] and now - _openclaw_token_cache["loaded_at"] < 60:
        return _openclaw_token_cache["token"]
    try:
        with open(_OPENCLAW_CONFIG_PATH) as f:
            token = json.load(f).get("gateway", {}).get("auth", {}).get("token", "") or ""
    except (OSError, ValueError):
        token = ""
    _openclaw_token_cache["token"] = token
    _openclaw_token_cache["loaded_at"] = now
    return token


def _openclaw_upstream_path(subpath):
    """Build the upstream path matching OpenClaw's basePath mount."""
    target = f"{_OPENCLAW_PROXY_PREFIX}/{subpath}" if subpath else f"{_OPENCLAW_PROXY_PREFIX}/"
    # Avoid stripping the trailing slash on the root path — OpenClaw's SPA
    # serves index.html only when asked for "/openclaw/", not "/openclaw".
    return target


def _openclaw_bootstrap_script(token: str) -> bytes:
    """Tiny inline bootstrap injected into the Control UI's index.html.

    The SPA's URL-fragment handler (`pI` in the bundle) applies a token
    via `lI(state, {...settings, token: u})` when `#token=...` is present
    and *no* `gatewayUrl` is given — that's the same-origin path that
    bypasses the "switch gateway" confirmation prompt. We also seed
    localStorage under every known gatewayUrl as a belt-and-braces so
    subsequent visits without a fragment still authenticate.
    """
    del token  # injected server-side in WS proxy; not needed client-side
    js = (
        "<script>"
        "window.__OPENCLAW_CONTROL_UI_BASE_PATH__='/openclaw';"
        "</script>"
    )
    return js.encode("utf-8")


@app.route("/openclaw", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@app.route("/openclaw/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@app.route("/openclaw/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@limiter.exempt
def openclaw_proxy(subpath=""):
    """HTTP reverse-proxy to the OpenClaw gateway control UI.

    The before_request middleware has already verified the session, so any
    request reaching this view is authenticated. We forward verbatim, stream
    the response body (important for SSE / long-running calls), and inject
    the gateway bearer token so the browser never sees it.

    For the SPA index.html we additionally inject a bootstrap script that
    primes the gatewayUrl + token in the URL hash so the UI auto-connects
    to the same origin without ever showing its built-in auth dialog.
    """
    target_path = _openclaw_upstream_path(subpath)
    url = f"http://{_OPENCLAW_PROXY_HOST}:{_OPENCLAW_PROXY_PORT}{target_path}"
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}
    # Forward the browser's Origin to the gateway. patch_openclaw_config
    # writes every origin the dashboard can be reached at (loopback, LAN IP,
    # mDNS, Pangolin domain) into gateway.controlUi.allowedOrigins; the
    # gateway accepts or rejects based on this header. Overriding Origin to
    # the proxy's own loopback URL — as we did before — leaves a port-mismatch
    # against the allowlist entries and gets us rejected.
    token = _openclaw_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        upstream = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=(10, 300),  # connect, read — generous read for SSE/agent calls
        )
    except requests.RequestException as e:
        return jsonify({"error": "openclaw_unreachable", "detail": str(e)[:200]}), 502

    # Strip hop-by-hop and content-encoding (already decoded by requests).
    excluded = _HOP_BY_HOP_HEADERS | {"content-encoding", "content-length"}
    resp_headers = [(k, v) for k, v in upstream.raw.headers.items() if k.lower() not in excluded]

    upstream_ct = upstream.headers.get("Content-Type", "")
    is_html = (
        request.method == "GET"
        and upstream.status_code == 200
        and upstream_ct.lower().startswith("text/html")
    )

    if is_html:
        # Buffer the small HTML so we can splice in the base-path global
        # before the SPA module evaluates. Streaming buys nothing for
        # ~10 KB and prevents body rewriting.
        try:
            body = upstream.raw.read(decode_content=True)
        finally:
            upstream.close()
        # Force a fresh fetch every visit so stale HTML can never run a
        # prior bootstrap variant against current state.
        resp_headers = [(k, v) for k, v in resp_headers if k.lower() != "cache-control"]
        resp_headers.append(("Cache-Control", "no-store, max-age=0"))
        marker = b"<head>"
        idx = body.find(marker)
        if idx != -1:
            inject = _openclaw_bootstrap_script(token)
            body = body[: idx + len(marker)] + inject + body[idx + len(marker) :]
        return Response(body, status=upstream.status_code, headers=resp_headers)

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=16384):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers)


# The Control UI's `new WebSocket(gatewayUrl)` call targets the basePath
# root, not a subpath — without an exact-match route flask-sock never matches
# and the bare `/openclaw` upgrade falls through to the HTTP proxy view → 400.
# Register the handler under all three rules. NB: sock.route decorators do not
# return the wrapped function, so they can't be stacked syntactically; call
# them as plain functions to bind the same handler under each rule.
def openclaw_ws_proxy_root(ws):
    return _openclaw_ws_handle(ws, "")


def openclaw_ws_proxy(ws, subpath):
    return _openclaw_ws_handle(ws, subpath)


sock.route("/openclaw", endpoint="openclaw_ws_root")(openclaw_ws_proxy_root)
sock.route("/openclaw/", endpoint="openclaw_ws_root_slash")(openclaw_ws_proxy_root)
sock.route("/openclaw/<path:subpath>", endpoint="openclaw_ws_sub")(openclaw_ws_proxy)


def _openclaw_ws_handle(ws, subpath):
    """WebSocket reverse-proxy to the OpenClaw gateway.

    OpenClaw's control UI uses WS for live agent streaming. We bridge the
    browser <-> gateway sockets in a single greenlet-per-direction so the
    flow is fully bidirectional.
    """
    if not session.get("authenticated"):
        ws.close(reason="unauthenticated")
        return

    import websocket as ws_client  # websocket-client; lazy import keeps cold-start cheap

    target_path = _openclaw_upstream_path(subpath)
    if request.query_string:
        target_path += "?" + request.query_string.decode("latin-1")
    upstream_url = f"ws://{_OPENCLAW_PROXY_HOST}:{_OPENCLAW_PROXY_PORT}{target_path}"

    headers = []
    token = _openclaw_token()
    if token:
        headers.append(f"Authorization: Bearer {token}")
    forwarded_proto = ("Sec-WebSocket-Protocol", request.headers.get("Sec-WebSocket-Protocol"))
    if forwarded_proto[1]:
        headers.append(f"{forwarded_proto[0]}: {forwarded_proto[1]}")

    # Forward the browser's Origin verbatim. The gateway's
    # controlUi.allowedOrigins is populated by patch_openclaw_config with
    # every dashboard origin (loopback, LAN IP, mDNS, Pangolin domain);
    # the gateway only accepts the connection when this header matches.
    # Pass via the explicit `origin=` kwarg — including it in header= as
    # well makes websocket-client send TWO Origin headers which get merged
    # into a comma-separated value that matches nothing in the allowlist.
    browser_origin = request.headers.get("Origin") or f"http://{_OPENCLAW_PROXY_HOST}"

    try:
        upstream = ws_client.create_connection(
            upstream_url, header=headers, origin=browser_origin,
            timeout=30, enable_multithread=True,
        )
    except Exception as e:
        logging.warning("OpenClaw WS upstream connect failed: %s", e)
        try:
            ws.close(reason=f"upstream_unreachable")
        except Exception:
            pass
        return

    stop = threading.Event()

    def client_to_upstream():
        # The manager session has already authenticated the user, and we
        # hold the gateway bearer token server-side. Rather than coax the
        # SPA into shipping it (URL hash, localStorage, settings dance —
        # all browser-state-dependent), inject auth.token into the very
        # first `connect` request as it passes through. The Control UI's
        # connect frame is a small JSON text message; subsequent traffic
        # is forwarded verbatim. Force-write the token even if the SPA
        # supplied one — server-side state is authoritative.
        connect_injected = False
        try:
            while not stop.is_set():
                msg = ws.receive()
                if msg is None:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    upstream.send_binary(bytes(msg))
                    continue
                if not connect_injected and token and isinstance(msg, str):
                    try:
                        parsed = json.loads(msg)
                        if (
                            isinstance(parsed, dict)
                            and parsed.get("type") == "req"
                            and parsed.get("method") == "connect"
                        ):
                            params = parsed.setdefault("params", {})
                            auth = params.setdefault("auth", {})
                            auth["token"] = token
                            # The browser's device-identity signature is
                            # computed over a payload that includes its
                            # idea of the token (usually empty, since
                            # the user never sees it). Injecting the
                            # real token here would make that signature
                            # invalid, so strip the device field — we
                            # rely on token + the gateway's allowInsecureAuth
                            # setting for auth. The manager session is the
                            # real defense layer; device identity is a
                            # nice-to-have we cannot satisfy without the
                            # browser's private key.
                            params.pop("device", None)
                            msg = json.dumps(parsed)
                            connect_injected = True
                    except (ValueError, TypeError):
                        pass
                upstream.send(msg)
        except Exception:
            pass
        finally:
            stop.set()
            try: upstream.close()
            except Exception: pass

    def upstream_to_client():
        try:
            while not stop.is_set():
                opcode, data = upstream.recv_data(control_frame=False)
                if opcode == ws_client.ABNF.OPCODE_BINARY:
                    ws.send(bytes(data))
                elif opcode == ws_client.ABNF.OPCODE_TEXT:
                    ws.send(data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else data)
                elif opcode == ws_client.ABNF.OPCODE_CLOSE:
                    break
        except ws_client.WebSocketConnectionClosedException:
            pass
        except Exception:
            pass
        finally:
            stop.set()
            try: ws.close()
            except Exception: pass

    t1 = threading.Thread(target=client_to_upstream, daemon=True)
    t2 = threading.Thread(target=upstream_to_client, daemon=True)
    t1.start(); t2.start()
    # Block this handler until both directions are done so flask-sock keeps
    # the underlying WSGI connection open.
    t1.join(); t2.join()


# --- Routes: HomeBrain Vault (Vaultwarden) ---
def _vault_admin_token_plain():
    """Recompute the plaintext admin token (sha256(MASTER:NONCE)) — never stored on disk.
    Mirrors scripts/provision_vault.sh derivation. Returns empty string if either
    component is missing.
    """
    env = get_env_config()
    mp = env.get("MASTER_PASSWORD", "")
    nonce = env.get("VAULT_ADMIN_NONCE", "")
    if not mp or not nonce:
        return ""
    return hashlib.sha256(f"{mp}:{nonce}".encode()).hexdigest()


def _vault_base_url():
    """Internal URL the dashboard uses to talk to vaultwarden. Always 127.0.0.1."""
    env = get_env_config()
    port = env.get("VAULT_PORT", "8082")
    return f"http://127.0.0.1:{port}"


def _vault_public_url():
    """The user-facing vault URL.

    In local mode, derive it from the dashboard's request Host header so the
    link matches whatever path the user took to reach the dashboard (e.g.
    accessing via 192.168.178.58 → vault on https://192.168.178.58:8443;
    accessing via homebrain.local → vault on https://homebrain.local:8443).
    Caddy serves a SAN-correct cert for each. The env var VAULT_DOMAIN
    remains the canonical URL Vaultwarden itself uses internally (Send
    links, etc.).

    In remote mode, always use the configured tunnel URL.
    """
    env = get_env_config()
    if not is_local_mode() and not _is_lan_request():
        domain = env.get("VAULT_DOMAIN")
        if domain:
            return domain
        pd = env.get("PANGOLIN_DOMAIN")
        return f"https://vault.{pd}" if pd else ""

    # Local / LAN access: derive from the request hostname so the link
    # matches how the user reached the dashboard.
    https_port = env.get("VAULT_LOCAL_HTTPS_PORT", "8443")
    host = ""
    try:
        host = request.host.split(":", 1)[0]
    except RuntimeError:
        pass
    if not host:
        host = "homebrain.local"
    return f"https://{host}:{https_port}"


@app.route("/api/vault/status")
def vault_status():
    """Surface vault state for the dashboard tile."""
    env = get_env_config()
    enabled = env.get("VAULT_ENABLED", "true").lower() != "false"
    info = {
        "enabled": enabled,
        "public_url": _vault_public_url(),
        "signups_allowed": env.get("VAULT_SIGNUPS_ALLOWED", "true").lower() == "true",
        "configured": bool(env.get("VAULT_ADMIN_TOKEN")),
        "container": "stopped",
        "users": None,
        "items": None,
    }
    # Container running?
    try:
        cid = compose_ps_q("vaultwarden")
        if cid:
            state = subprocess.check_output(
                ["docker", "inspect", "-f", "{{.State.Health.Status}}", cid],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            info["container"] = state or "running"
    except Exception:
        pass

    # User count: query the DB directly (avoids the admin-cookie JWT dance).
    if info["container"] in ("running", "healthy"):
        try:
            db_cid = compose_ps_q("db")
            db_name = env.get("VAULT_DB_NAME", "vaultwarden")
            db_user = env.get("VAULT_DB_USER", "vaultwarden_user")
            db_pass = env.get("VAULT_DB_PASSWORD", "")
            if db_cid and db_pass:
                out = subprocess.check_output(
                    ["docker", "exec", "-e", f"MYSQL_PWD={db_pass}", db_cid,
                     "mariadb", "-u", db_user, "-N", "-s", "-e",
                     f"SELECT COUNT(*) FROM `{db_name}`.users;"],
                    stderr=subprocess.DEVNULL, timeout=4,
                ).decode().strip()
                info["users"] = int(out) if out.isdigit() else None
        except Exception:
            pass
    return jsonify(info)


# --- Vault admin reverse proxy ---
# Why this exists: opening Vaultwarden's /admin in a new tab cross-origin
# was fragile in browsers — the user's TLS-cert-warning click-through could
# break the auto-submitted token POST and dump them on Vaultwarden's login
# form (the very prompt we're trying to avoid). Proxying through the
# manager keeps the user same-origin (the dashboard's HTTP origin), keeps
# the admin token server-side, and removes any cross-site cookie issues.
def _vault_admin_jwt():
    """Get a fresh VW_ADMIN JWT for proxying. Cached in the manager session
    until ~30s before its Max-Age expiry, then re-issued by POSTing the admin
    token to the internal vault endpoint. Returns None if the vault isn't
    provisioned or the login fails."""
    now = int(time.time())
    cached = session.get('vault_admin_jwt')
    cached_exp = session.get('vault_admin_jwt_exp', 0)
    if cached and cached_exp > now + 30:
        return cached
    token = _vault_admin_token_plain()
    if not token:
        return None
    try:
        r = requests.post(
            f"{_vault_base_url()}/admin",
            data={"token": token},
            allow_redirects=False,
            timeout=5,
        )
    except Exception as e:
        logging.error(f"vault admin login failed: {e}")
        return None
    if r.status_code not in (200, 302):
        return None
    set_cookie = r.headers.get("Set-Cookie", "")
    m = re.search(r"VW_ADMIN=([^;]+)", set_cookie)
    if not m:
        return None
    jwt = m.group(1)
    ma = re.search(r"Max-Age=(\d+)", set_cookie, re.IGNORECASE)
    ttl = int(ma.group(1)) if ma else 1200
    session['vault_admin_jwt'] = jwt
    session['vault_admin_jwt_exp'] = now + ttl
    return jwt


_VAULT_PROXY_HOP_HEADERS = {
    "host", "cookie", "content-length", "content-encoding",
    "transfer-encoding", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "upgrade",
}


def _vault_proxy(upstream_path: str):
    """Proxy the current request to the internal vault container with the
    cached admin JWT attached. Streams the response body."""
    if not session.get("authenticated"):
        abort(401)
    jwt = _vault_admin_jwt()
    if not jwt:
        return Response("Vault admin not configured.", status=503)
    upstream = f"{_vault_base_url()}{upstream_path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _VAULT_PROXY_HOP_HEADERS}
    try:
        upstream_resp = requests.request(
            request.method, upstream,
            params=request.args,
            data=request.get_data() if request.method != "GET" else None,
            headers=headers,
            cookies={"VW_ADMIN": jwt},
            allow_redirects=False,
            stream=True,
            timeout=30,
        )
    except Exception as e:
        logging.error(f"vault admin proxy upstream error: {e}")
        return Response(f"Vault unreachable: {e}", status=502)

    excluded = _VAULT_PROXY_HOP_HEADERS | {"set-cookie"}
    out_headers = [(k, v) for k, v in upstream_resp.raw.headers.items()
                   if k.lower() not in excluded]
    return Response(
        upstream_resp.iter_content(chunk_size=8192),
        status=upstream_resp.status_code,
        headers=out_headers,
    )


@app.route("/admin", methods=["GET", "POST"])
@app.route("/admin/", methods=["GET", "POST"])
@app.route("/admin/<path:p>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@limiter.limit("120 per minute")
def vault_admin_proxy(p: str = ""):
    """Reverse-proxy for Vaultwarden's /admin panel. The user lands here
    same-origin under the manager; the admin token never leaves the server."""
    suffix = f"/{p}" if p else ""
    return _vault_proxy(f"/admin{suffix}")


@app.route("/vw_static/<path:p>", methods=["GET"])
@limiter.limit("240 per minute")
def vault_static_proxy(p: str):
    """Static assets used by the proxied admin panel (CSS/JS/icons)."""
    if not session.get("authenticated"):
        abort(401)
    upstream = f"{_vault_base_url()}/vw_static/{p}"
    try:
        r = requests.get(upstream, stream=True, timeout=30)
    except Exception as e:
        return Response(f"Vault unreachable: {e}", status=502)
    excluded = _VAULT_PROXY_HOP_HEADERS | {"set-cookie"}
    out_headers = [(k, v) for k, v in r.raw.headers.items()
                   if k.lower() not in excluded]
    return Response(r.iter_content(chunk_size=8192),
                    status=r.status_code, headers=out_headers)


# --- Vault MCP unlock (P6) ---
# The MCP server (scripts/mcp-vault.py) refuses to handle secrets unless a
# valid `bw` session token sits at $VAULT_SESSION_FILE. The dashboard is the
# only component that ever sees the user's vault master password — it runs
# `bw unlock` here and writes the token (mode 0600). The LLM never sees it.
VAULT_MCP_SESSION_FILE = os.path.join(
    os.environ.get("HOMEBRAIN_HOME", "/home/homebrain"),
    ".openclaw", "vault.session",
)


def _vault_bw_url():
    """The URL the bw CLI on this box uses to reach the local vault.

    Always loopback to the Caddy TLS edge — Vaultwarden insists on HTTPS,
    and Caddy's `tls internal` cert for 127.0.0.1 has a matching IP SAN.
    Talking to the public VAULT_DOMAIN would route through Pangolin/Traefik
    and present the wrong cert (TRAEFIK DEFAULT CERT), which is what made
    every `bw unlock` fail with "self-signed certificate" before.
    """
    env = get_env_config()
    port = env.get("VAULT_LOCAL_HTTPS_PORT", "8443")
    return f"https://127.0.0.1:{port}"


def _vault_bw_argv(*bw_args, session=None):
    """Build a `sudo -u homebrain env … bw …` argv that survives sudo's
    env-stripping. We need NODE_TLS_REJECT_UNAUTHORIZED=0 (loopback Caddy
    presents an IP-SAN cert that Node's hostname check rejects; safe
    because the destination is 127.0.0.1 — MITM there already implies
    code execution as root) and optionally BW_SESSION inside the bw
    process's environment, not just the sudo wrapper's."""
    extra_env = ["NODE_TLS_REJECT_UNAUTHORIZED=0"]
    if session:
        extra_env.append(f"BW_SESSION={session}")
    return ["sudo", "-u", "homebrain", "env", *extra_env, "bw", *bw_args]


def _vault_first_user_email():
    """Return the email of the (typically single) vault user, or "".
    Used so the unlock form does not need to ask for the email."""
    env = get_env_config()
    db_pass = env.get("VAULT_DB_PASSWORD", "")
    db_name = env.get("VAULT_DB_NAME", "vaultwarden")
    db_user = env.get("VAULT_DB_USER", "vaultwarden_user")
    if not db_pass:
        return ""
    try:
        db_cid = compose_ps_q("db")
        if not db_cid:
            return ""
        out = subprocess.check_output(
            ["docker", "exec", "-e", f"MYSQL_PWD={db_pass}", db_cid,
             "mariadb", "-u", db_user, "-N", "-s", "-e",
             f"SELECT email FROM `{db_name}`.users ORDER BY created_at LIMIT 1;"],
            stderr=subprocess.DEVNULL, timeout=4,
        ).decode().strip()
        return out
    except Exception:
        return ""


def _vault_bw_status(session=None):
    """Run `bw status` as the homebrain user (whose bw state the MCP uses).
    Returns the parsed JSON dict, or {} on failure."""
    try:
        r = subprocess.run(
            _vault_bw_argv("status", session=session),
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return {}


@app.route("/api/vault/mcp/status")
def vault_mcp_status():
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401
    info = {
        "session_file": VAULT_MCP_SESSION_FILE,
        "unlocked": False,
        "bw_status": "unknown",
        "bw_installed": False,
        "openclaw_wired": False,
        "openclaw_available": shutil.which("openclaw") is not None,
        "needs_login": False,
        "known_email": "",
    }
    info["bw_installed"] = shutil.which("bw") is not None
    if info["openclaw_available"]:
        try:
            out = subprocess.check_output(
                ["sudo", "-u", "homebrain", "openclaw", "mcp", "show"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode()
            info["openclaw_wired"] = "homebrain-vault" in out
        except Exception:
            pass
    if info["bw_installed"]:
        bw_state = _vault_bw_status()
        info["bw_status"] = bw_state.get("status", "unknown")
        info["needs_login"] = info["bw_status"] == "unauthenticated"
        if os.path.exists(VAULT_MCP_SESSION_FILE):
            try:
                tok = open(VAULT_MCP_SESSION_FILE).read().strip()
                r = subprocess.run(
                    _vault_bw_argv("status", session=tok),
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    info["unlocked"] = json.loads(r.stdout).get("status") == "unlocked"
            except Exception:
                pass
    info["known_email"] = _vault_first_user_email()
    return jsonify(info)


@app.route("/api/vault/mcp/unlock", methods=["POST"])
@limiter.limit("5 per minute")
def vault_mcp_unlock():
    """Authenticate bw to the local vault and persist a usable BW_SESSION at
    VAULT_MCP_SESSION_FILE (mode 0600). Runs bw as the homebrain user so
    cached vault data lands where the openclaw MCP server can read it.

    Flow:
      * `bw config server` → loopback Caddy URL
      * `bw status`
        - "unauthenticated" → `bw login --raw <email> <pw>` (email from
          request body or, falling back, the single vault user in the DB)
        - "locked" / "unlocked" → `bw unlock --raw <pw>`

    The password is never logged or persisted."""
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401
    if shutil.which("bw") is None:
        return jsonify({
            "error": "Bitwarden CLI not installed",
            "hint": "Run `sudo npm install -g @bitwarden/cli` then retry.",
        }), 503

    data = request.get_json(silent=True) or {}
    master_pw = data.get("master_password", "")
    if not master_pw:
        return jsonify({"error": "master_password required"}), 400
    email = (data.get("email") or "").strip().lower()

    env_cfg = get_env_config()
    if env_cfg.get("VAULT_ENABLED", "true").lower() == "false":
        return jsonify({"error": "vault not provisioned"}), 503
    bw_url = _vault_bw_url()

    # Point bw at the local vault. Idempotent; safe to re-run every call.
    try:
        subprocess.run(
            _vault_bw_argv("config", "server", bw_url),
            capture_output=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "bw config timed out"}), 504

    bw_state = _vault_bw_status()
    status_str = bw_state.get("status", "")

    try:
        if status_str == "unauthenticated":
            if not email:
                email = _vault_first_user_email()
            if not email:
                return jsonify({
                    "error": "email required",
                    "detail": "bw has not been logged in yet — provide the "
                              "vault account email.",
                }), 400
            proc = subprocess.run(
                _vault_bw_argv("login", "--raw", email, master_pw),
                capture_output=True, text=True, timeout=30,
            )
            failure_label = "login failed"
        else:
            proc = subprocess.run(
                _vault_bw_argv("unlock", "--raw", master_pw),
                capture_output=True, text=True, timeout=15,
            )
            failure_label = "unlock failed"
    except subprocess.TimeoutExpired:
        return jsonify({"error": "bw operation timed out"}), 504

    if proc.returncode != 0:
        # `bw` prints a hard error line we can surface — usually
        # "Username or password is incorrect" or a server-reachability hint.
        detail = (proc.stderr or proc.stdout or "").strip()
        # Drop Node deprecation noise from the surfaced message.
        detail = "\n".join(
            ln for ln in detail.splitlines()
            if "DeprecationWarning" not in ln
            and "NODE_TLS_REJECT_UNAUTHORIZED" not in ln
            and "trace-deprecation" not in ln
        ).strip()
        return jsonify({"error": failure_label, "detail": detail[:300]}), 401

    token = proc.stdout.strip()
    if not token:
        return jsonify({"error": "no session token returned"}), 502

    os.makedirs(os.path.dirname(VAULT_MCP_SESSION_FILE), exist_ok=True)
    fd = os.open(VAULT_MCP_SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    try:
        import pwd
        hb_uid = pwd.getpwnam("homebrain").pw_uid
        os.chown(VAULT_MCP_SESSION_FILE, hb_uid, hb_uid)
    except Exception:
        pass
    return jsonify({"status": "unlocked"})


@app.route("/api/vault/mcp/wire-up", methods=["POST"])
@limiter.limit("5 per minute")
def vault_mcp_wire_up():
    """Register the Vault MCP server with the local OpenClaw daemon via
    `openclaw mcp set`. Idempotent — re-running just overwrites the entry.
    Restarts the OpenClaw daemon so it picks up the new MCP server."""
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401
    if shutil.which("openclaw") is None:
        return jsonify({"error": "openclaw CLI not found"}), 503

    spec = {
        "command": "/usr/bin/python3",
        "args": [f"{INSTALL_DIR}/scripts/mcp-vault.py"],
        "env": {
            "VAULT_URL": _vault_bw_url(),
            "VAULT_SESSION_FILE": VAULT_MCP_SESSION_FILE,
            "VAULT_AUDIT_LOG": "/var/log/homebrain/mcp-vault-audit.log",
            # bw on this box talks to Caddy's `tls internal` cert on
            # 127.0.0.1; Node's hostname check rejects the IP-SAN cert.
            # Loopback only — MITM impossible without root.
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        },
    }
    try:
        # `openclaw mcp set` runs as the homebrain user (config lives at
        # /home/homebrain/.openclaw/openclaw.json owned by homebrain).
        subprocess.check_call(
            ["sudo", "-u", "homebrain", "openclaw", "mcp", "set",
             "homebrain-vault", json.dumps(spec)],
            stderr=subprocess.STDOUT, timeout=15,
        )
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"openclaw mcp set failed: {e}"}), 500

    # Restart the daemon so the new MCP server is picked up. Best-effort —
    # if the daemon isn't running we just leave the config in place.
    try:
        subprocess.run(
            ["sudo", "-u", "homebrain", "openclaw", "daemon", "restart"],
            capture_output=True, timeout=20,
        )
    except Exception:
        pass

    return jsonify({"status": "registered", "name": "homebrain-vault"})


@app.route("/api/vault/mcp/unwire", methods=["POST"])
@limiter.limit("5 per minute")
def vault_mcp_unwire():
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401
    if shutil.which("openclaw") is None:
        return jsonify({"error": "openclaw CLI not found"}), 503
    try:
        subprocess.run(
            ["sudo", "-u", "homebrain", "openclaw", "mcp", "unset",
             "homebrain-vault"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "-u", "homebrain", "openclaw", "daemon", "restart"],
            capture_output=True, timeout=20,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"status": "removed"})


@app.route("/api/vault/mcp/lock", methods=["POST"])
@limiter.limit("10 per minute")
def vault_mcp_lock():
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401
    try:
        if os.path.exists(VAULT_MCP_SESSION_FILE):
            os.remove(VAULT_MCP_SESSION_FILE)
        subprocess.run(
            _vault_bw_argv("lock"),
            capture_output=True, timeout=10,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"status": "locked"})


@app.route("/api/vault/docs/status")
def vault_docs_status():
    """Report whether the Nextcloud end-to-end-encryption app is enabled and
    whether the canonical encrypted folder exists for the admin user."""
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401
    info = {"e2ee_enabled": False, "folder_exists": False, "folder_url": ""}
    try:
        nc_cid = compose_ps_q("nextcloud")
        if not nc_cid:
            return jsonify(info)
        out = subprocess.check_output(
            ["docker", "exec", "-u", "www-data", nc_cid,
             "php", "occ", "app:list", "--output=json"],
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode()
        try:
            apps = json.loads(out)
            enabled = apps.get("enabled", {})
            info["e2ee_enabled"] = "end_to_end_encryption" in enabled
        except Exception:
            pass
        # Folder existence: probe the admin user's data dir directly
        env = get_env_config()
        nc_user = env.get("NEXTCLOUD_ADMIN_USER", "admin")
        data_dir = env.get("NEXTCLOUD_DATA_DIR", "/home/homebrain/nextcloud-data")
        folder_path = os.path.join(data_dir, nc_user, "files", "Documents (Encrypted)")
        info["folder_exists"] = os.path.isdir(folder_path)
        if info["e2ee_enabled"] and info["folder_exists"]:
            base = ""
            if is_local_mode():
                base = f"http://{get_lan_ip()}:8080"
            else:
                base = f"https://{env.get('NEXTCLOUD_TRUSTED_DOMAINS', '')}"
            info["folder_url"] = f"{base}/apps/files/?dir=/Documents%20(Encrypted)"
    except Exception:
        pass
    return jsonify(info)


@app.route("/api/vault/docs/setup", methods=["POST"])
@limiter.limit("5 per minute")
def vault_docs_setup():
    """Provision the canonical 'Documents (Encrypted)' folder for the admin
    user, and best-effort install + enable Nextcloud's end_to_end_encryption
    app. The folder always gets created (so the user has a known landing
    spot); the E2E app install can fail offline or on a Nextcloud install
    without app-store access, in which case we surface that and let the user
    enable it manually. The folder still works as a private folder; users
    can mark it for E2EE in the Nextcloud desktop client once the app is
    enabled. Idempotent."""
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401
    try:
        nc_cid = compose_ps_q("nextcloud")
        if not nc_cid:
            return jsonify({"error": "Nextcloud container not running"}), 503

        # 1. Best-effort enable end_to_end_encryption. Don't fail the whole
        # request if the app store isn't reachable.
        e2ee_enabled = False
        e2ee_error = ""
        try:
            inst = subprocess.run(
                ["docker", "exec", "-u", "www-data", nc_cid,
                 "php", "occ", "app:install", "end_to_end_encryption"],
                capture_output=True, text=True, timeout=60,
            )
            en = subprocess.run(
                ["docker", "exec", "-u", "www-data", nc_cid,
                 "php", "occ", "app:enable", "end_to_end_encryption"],
                capture_output=True, text=True, timeout=30,
            )
            if en.returncode == 0:
                e2ee_enabled = True
            else:
                # Concatenate the most useful diagnostic from both calls.
                e2ee_error = (inst.stdout + inst.stderr + en.stdout + en.stderr).strip()[:300]
        except subprocess.TimeoutExpired:
            e2ee_error = "occ command timed out"

        # 2. Always create the folder via host filesystem + files:scan.
        env = get_env_config()
        nc_user = env.get("NEXTCLOUD_ADMIN_USER", "admin")
        data_dir = env.get("NEXTCLOUD_DATA_DIR", "/home/homebrain/nextcloud-data")
        folder_path = os.path.join(data_dir, nc_user, "files", "Documents (Encrypted)")
        os.makedirs(folder_path, exist_ok=True)
        try:
            subprocess.check_call(
                ["chown", "-R", "33:33", folder_path], stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        subprocess.run(
            ["docker", "exec", "-u", "www-data", nc_cid,
             "php", "occ", "files:scan", f"--path={nc_user}/files/Documents (Encrypted)"],
            stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=120,
        )

        return jsonify({
            "status": "ready",
            "folder": "Documents (Encrypted)",
            "e2ee_app_enabled": e2ee_enabled,
            "e2ee_hint": (
                "" if e2ee_enabled else
                "End-to-end encryption app could not be installed automatically "
                "(usually because the app store is unreachable). Folder created "
                "regardless — install end_to_end_encryption from your Nextcloud "
                "Apps page, then mark the folder as encrypted in the desktop "
                "client. Detail: " + (e2ee_error or "no further detail")
            ),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vault/local-ca")
@limiter.limit("20 per minute")
def vault_local_ca():
    """Serve the Caddy-issued internal CA root certificate so users can
    install it on their LAN clients (one-time per device). Master-password
    gated. Returns 404 in remote mode where Pangolin's public CA chain is
    used. Returns 503 if Caddy hasn't minted the CA yet (first boot)."""
    if not session.get("authenticated"):
        abort(401)
    if not is_local_mode():
        return ("Local CA is only used in LAN-only deployments. "
                "Remote-mode installs use Pangolin's public TLS chain."), 404
    try:
        cid = compose_ps_q("caddy")
        if not cid:
            return "Caddy container not running.", 503
        # Caddy stores its internal CA root at a known path inside the
        # caddy_data volume.
        pem = subprocess.check_output(
            ["docker", "exec", cid, "cat",
             "/data/caddy/pki/authorities/local/root.crt"],
            stderr=subprocess.DEVNULL, timeout=5,
        )
        if not pem.strip().startswith(b"-----BEGIN"):
            return "CA not yet generated — try again in 30 s.", 503
        return Response(
            pem,
            mimetype="application/x-pem-file",
            headers={
                "Content-Disposition": 'attachment; filename="homebrain-vault-ca.pem"',
            },
        )
    except subprocess.CalledProcessError:
        return "CA not available yet.", 503
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/api/vault/bootstrap", methods=["POST"])
@limiter.limit("5 per minute")
def vault_bootstrap():
    """Create the first vault user via the admin API, then disable signups.
    Idempotent: returns {already_bootstrapped: true} if any user already exists.
    """
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "valid email required"}), 400

    token = _vault_admin_token_plain()
    if not token:
        return jsonify({"error": "vault not provisioned"}), 503

    base = _vault_base_url()
    try:
        # Vaultwarden's /admin POST exchanges the plaintext admin token for a
        # JWT cookie (VW_ADMIN). All subsequent /admin/* calls require the JWT.
        s = requests.Session()
        login = s.post(f"{base}/admin", data={"token": token}, timeout=5, allow_redirects=False)
        if login.status_code not in (200, 302):
            return jsonify({"error": f"admin login failed: HTTP {login.status_code}"}), 502
        if not s.cookies.get("VW_ADMIN"):
            return jsonify({"error": "admin login produced no session cookie — check ADMIN_TOKEN"}), 502

        # Existing users?
        r = s.get(f"{base}/admin/users", headers={"Accept": "application/json"}, timeout=5)
        if r.status_code == 200:
            try:
                existing = r.json()
                if isinstance(existing, list) and len(existing) > 0:
                    update_env_var("VAULT_SIGNUPS_ALLOWED", "false")
                    return jsonify({"already_bootstrapped": True, "users": len(existing)})
            except Exception:
                pass

        # Issue invite. Without SMTP, Vaultwarden creates the invite locally;
        # the recipient finishes signup by visiting the public vault URL.
        # /admin/invite expects JSON (Form on POST / login, Json on /invite).
        r = s.post(f"{base}/admin/invite", json={"email": email}, timeout=10, allow_redirects=False)
        if r.status_code not in (200, 302):
            return jsonify({"error": f"invite failed: HTTP {r.status_code}", "body": r.text[:300]}), 502

        update_env_var("VAULT_SIGNUPS_ALLOWED", "false")
        return jsonify({
            "status": "invited",
            "email": email,
            "next": "Open the public vault URL to set the master password for this user.",
        })
    except requests.RequestException as e:
        return jsonify({"error": f"vault unreachable: {e}"}), 503


@app.route("/api/redis/status")
def redis_status():
    try:
        status = subprocess.check_output(["bash", SCRIPT_UTILITIES, "redis_status"]).decode().strip()
        return jsonify({"status": status})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/api/redis/configure", methods=["POST"])
@limiter.limit("5 per minute")
def redis_configure():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    cmd = f"bash {SCRIPT_UTILITIES} redis_configure >> {LOG_FILES['setup']} 2>&1"
    threading.Thread(
        target=run_background_task, args=("Configure Redis", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})


# --- Routes: Nuclear Reset (Factory Wipe) ---
# EXTREMELY DANGEROUS — only after full multi-factor confirmation in the UI.
@app.route("/api/system/nuclear-reset", methods=["POST"])
@limiter.limit("1 per 10 minutes")
def nuclear_reset():
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401

    if current_task_status["status"] == "running":
        return jsonify({"error": "Another task is already running"}), 409

    data = request.get_json(silent=True) or {}

    # 1. Verify current master password (constant-time comparison)
    env = get_env_config()
    current_pw = data.get("current_password", "")
    expected_pw = env.get("MANAGER_PASSWORD", "")
    if not current_pw or not expected_pw or not hmac.compare_digest(current_pw, expected_pw):
        time.sleep(2)
        return jsonify({"error": "Invalid current password"}), 401

    # 2. Verify exact confirmation phrase (case-sensitive, no trimming)
    if data.get("confirmation_phrase") != "DESTROY ALL DATA":
        return jsonify({"error": "Confirmation phrase did not match exactly"}), 400

    # 3. Read wipe flags (defaults per plan)
    wipe_models  = bool(data.get("wipe_ai_models", True))
    wipe_runtime = bool(data.get("wipe_ai_runtime", False))

    # 4. Ensure script is executable
    subprocess.run(["chmod", "+x", SCRIPT_NUCLEAR], check=False)

    # 5. Launch the nuclear script in background
    # We pass the two booleans as arguments (the script treats any non-"true" as false)
    # Log primarily to dedicated nuclear log (also visible in setup log via the UI if needed)
    nuclear_log = LOG_FILES.get("nuclear", LOG_FILES["setup"])
    cmd = (
        f"bash {shlex.quote(SCRIPT_NUCLEAR)} "
        f"{shlex.quote('true' if wipe_models else 'false')} "
        f"{shlex.quote('true' if wipe_runtime else 'false')} "
        f">> {shlex.quote(nuclear_log)} 2>&1"
    )

    threading.Thread(
        target=run_background_task,
        args=("Nuclear Reset (Factory Wipe)", cmd, "setup")
    ).start()

    # Immediately invalidate this session (device will reboot anyway)
    session.clear()

    return jsonify({
        "status": "started",
        "message": "Nuclear reset initiated. Device will reboot when complete."
    })


# --- Routes: Recovery Phrase ---
# Break-glass recovery for a forgotten master password. See
# docs/plans/RECOVERY_PHRASE.md. The verify/reset pair is reachable pre-auth
# (whitelisted in security_middleware) but LAN-origin-only by default and
# strictly rate-limited; status/regenerate are management actions for a
# logged-in user. The phrase is an INDEPENDENT secret stored only as a scrypt
# hash — recovery rotates the master password across the stack; it does NOT
# decrypt per-user Vaultwarden vaults (those are end-to-end encrypted).

RECOVERY_REMOTE_ENV = "RECOVERY_ALLOW_REMOTE"


def _recovery_configured():
    env = get_env_config()
    return bool(env.get("RECOVERY_SCRYPT_HASH") and env.get("RECOVERY_SCRYPT_SALT")
                and env.get("RECOVERY_PARAMS"))


def _recovery_origin_allowed():
    """Recovery is LAN-only unless RECOVERY_ALLOW_REMOTE=true in .env."""
    if _is_lan_request():
        return True
    return get_env_config().get(RECOVERY_REMOTE_ENV, "false").lower() == "true"


def _verify_recovery_phrase(phrase):
    env = get_env_config()
    return recovery.verify_phrase(
        phrase,
        env.get("RECOVERY_SCRYPT_SALT", ""),
        env.get("RECOVERY_SCRYPT_HASH", ""),
        env.get("RECOVERY_PARAMS", ""),
    )


@app.route("/api/recovery/verify", methods=["POST"])
@limiter.limit("5 per hour")
def recovery_verify():
    """Non-committal check of a recovery phrase (drives the reset-form UX)."""
    if not _recovery_origin_allowed():
        return jsonify({"error": "Recovery is restricted to local network access."}), 403
    if not _recovery_configured():
        return jsonify({"error": "No recovery phrase is configured on this device."}), 404
    data = request.get_json(silent=True) or {}
    if _verify_recovery_phrase(data.get("phrase", "")):
        return jsonify({"valid": True})
    time.sleep(2)  # match the login-handler timing penalty
    return jsonify({"valid": False, "error": "Recovery phrase did not match."}), 401


@app.route("/api/recovery/reset", methods=["POST"])
@limiter.limit("5 per hour")
def recovery_reset():
    """Verify the phrase, restore dashboard login immediately, then rotate the
    whole stack in the background."""
    if not _recovery_origin_allowed():
        return jsonify({"error": "Recovery is restricted to local network access."}), 403
    if not _recovery_configured():
        return jsonify({"error": "No recovery phrase is configured on this device."}), 404
    if current_task_status["status"] == "running":
        return jsonify({"error": "Another task is already running"}), 409

    data = request.get_json(silent=True) or {}
    phrase = data.get("phrase", "")
    new_password = data.get("new_password", "")

    # Re-verify here — never trust a prior /verify call (stateless across workers).
    if not _verify_recovery_phrase(phrase):
        time.sleep(2)
        return jsonify({"error": "Recovery phrase did not match."}), 401

    if not recovery.is_valid_new_password(new_password):
        return jsonify({"error": f"Invalid new password. {recovery.NEW_PASSWORD_RULE}"}), 400

    # 1. Restore dashboard access immediately and independently. MANAGER_PASSWORD
    #    is login-only (no container consumes it), so this cannot desync anything
    #    and guarantees the user is back in even if the stack rotation below
    #    partially fails. Then drop the (now stale) session.
    update_env_var("MANAGER_PASSWORD", new_password)
    session.clear()

    # 2. Hand the new password to the rotation script via a 0600 temp file
    #    (never argv/env), and run it as a tracked background task. The script
    #    shreds the file on exit.
    fd, secrets_path = tempfile.mkstemp(prefix="hb_rotate_", suffix=".tmp")
    try:
        os.write(fd, new_password.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(secrets_path, 0o600)

    subprocess.run(["chmod", "+x", SCRIPT_ROTATE], check=False)
    cmd = (
        f"bash {shlex.quote(SCRIPT_ROTATE)} {shlex.quote(secrets_path)} "
        f">> {shlex.quote(LOG_FILES['setup'])} 2>&1"
    )
    threading.Thread(
        target=run_background_task,
        args=("Master Password Rotation", cmd, "setup")
    ).start()

    # Out-of-band security event: the root credential was reset via recovery.
    logging.warning("SECURITY: master password reset via recovery phrase from %s",
                    request.remote_addr)

    return jsonify({
        "status": "started",
        "message": ("Recovery accepted. Log in with your new password — "
                    "the full stack rotation is finishing in the background.")
    })


@app.route("/api/recovery/status", methods=["GET"])
def recovery_status():
    env = get_env_config()
    return jsonify({
        "configured": _recovery_configured(),
        "created_at": env.get("RECOVERY_CREATED_AT"),
        "word_count": env.get("RECOVERY_WORD_COUNT"),
        "wordlist_ok": recovery.wordlist_ok(),
        "remote_allowed": env.get(RECOVERY_REMOTE_ENV, "false").lower() == "true",
    })


@app.route("/api/recovery/regenerate", methods=["POST"])
@limiter.limit("5 per hour")
def recovery_regenerate():
    """Mint a NEW phrase, return it once, replace the stored hash. The old
    phrase can never be revealed (only its hash is stored) — only regenerated."""
    try:
        phrase = recovery.generate_phrase()
        record = recovery.build_recovery_record(
            phrase, recovery.DEFAULT_PHRASE_WORDS, time.time())
        for k, v in record.items():
            update_env_var(k, v)
    except Exception as e:
        return jsonify({"error": f"Could not generate recovery phrase: {e}"}), 500
    return jsonify({"status": "ok", "recovery_phrase": phrase})


# --- Routes: FTP Management ---
@app.route("/api/ftp/users", methods=["GET"])
def list_ftp_users():
    """Parses VSFTPD config to return list of FTP users and their mapped Nextcloud users."""
    users = []
    user_conf_dir = "/etc/vsftpd/user_conf"
    
    if os.path.exists(user_conf_dir):
        try:
            for ftp_user in os.listdir(user_conf_dir):
                conf_path = os.path.join(user_conf_dir, ftp_user)
                nc_user = "Unknown"
                if os.path.isfile(conf_path):
                    with open(conf_path, "r") as f:
                        # We look for the comment we injected: # NC_USER=admin
                        for line in f:
                            if line.startswith("# NC_USER="):
                                nc_user = line.split("=")[1].strip()
                                break
                    users.append({"ftp_user": ftp_user, "nc_user": nc_user})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    return jsonify(users)

@app.route("/api/ftp/setup", methods=["POST"])
@limiter.limit("5 per minute")
def setup_ftp():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    data = request.json
    nc_user = data.get("nc_user")
    ftp_user = data.get("ftp_user")
    ftp_pass = data.get("ftp_pass")

    if not all([nc_user, ftp_user, ftp_pass]):
        return jsonify({"error": "Missing required fields"}), 400
        
    # Validation: FTP User should be alphanumeric
    if not ftp_user.isalnum():
        return jsonify({"error": "FTP Username must be alphanumeric"}), 400

    # Ensure utility script is executable
    subprocess.run(["chmod", "+x", SCRIPT_UTILITIES])

    # Write the password to a private temp file (0600) and pass only its path —
    # this keeps the secret out of argv (and therefore out of `ps`). utilities.sh
    # reads then shreds the file.
    fd, pass_file = tempfile.mkstemp(prefix="hb_ftp_", suffix=".pw")
    try:
        os.write(fd, ftp_pass.encode())
    finally:
        os.close(fd)
    os.chmod(pass_file, 0o600)

    cmd = (
        f"bash {shlex.quote(SCRIPT_UTILITIES)} setup "
        f"{shlex.quote(nc_user)} {shlex.quote(ftp_user)} {shlex.quote(pass_file)} "
        f">> {LOG_FILES['setup']} 2>&1"
    )

    threading.Thread(
        target=run_background_task, args=("Setup FTP Server", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})

@app.route("/api/ftp/delete", methods=["POST"])
@limiter.limit("5 per minute")
def delete_ftp():
    if current_task_status["status"] == "running":
        return jsonify({"error": "Task running"}), 409

    ftp_user = request.json.get("ftp_user")
    if not ftp_user or not ftp_user.isalnum():
        return jsonify({"error": "Invalid FTP username"}), 400

    cmd = f"bash {SCRIPT_UTILITIES} delete {shlex.quote(ftp_user)} >> {LOG_FILES['setup']} 2>&1"

    threading.Thread(
        target=run_background_task, args=("Delete FTP User", cmd, "setup")
    ).start()
    return jsonify({"status": "started"})


# --- Routes: Network (stable address for cameras) ---
# Cameras can't resolve mDNS (.local) and consumer routers often can't reserve a
# DHCP lease, so a fixed IP on the box is the reliable way to keep a camera's FTP
# target valid. Pinning uses a confirm-or-revert guard (see utilities.sh) so a bad
# address self-heals back to DHCP rather than locking the box off the network.
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")

def _valid_ipv4(s):
    m = _IPV4_RE.match((s or "").strip())
    return bool(m) and all(0 <= int(g) <= 255 for g in m.groups())

@app.route("/api/network/info", methods=["GET"])
def api_network_info():
    """Current addressing (iface, method, ip, gateway, dns) + a suggested free static IP."""
    try:
        out = subprocess.check_output(
            ["bash", SCRIPT_UTILITIES, "network_info"], text=True, timeout=20
        ).strip()
        return Response(out, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/network/pin", methods=["POST"])
@limiter.limit("10 per minute")
def api_network_pin():
    """Apply a static IP to the running device with an auto-revert guard.

    The box's IP changes ~2s after this returns, so the client must reconnect at
    the new address and POST /api/network/confirm before the revert window
    elapses, or addressing rolls back to DHCP automatically.
    """
    data = request.json or {}
    ip = (data.get("ip") or "").strip()
    prefix = str(data.get("prefix") or "24").strip()
    gateway = (data.get("gateway") or "").strip()
    dns = (data.get("dns") or gateway).strip()
    try:
        revert = str(max(30, min(600, int(data.get("revert", 180)))))
    except (TypeError, ValueError):
        revert = "180"

    if not _valid_ipv4(ip):
        return jsonify({"error": "Invalid IP address"}), 400
    if not _valid_ipv4(gateway):
        return jsonify({"error": "Invalid gateway"}), 400
    if not prefix.isdigit() or not (1 <= int(prefix) <= 32):
        return jsonify({"error": "Invalid prefix length"}), 400
    if not dns or any(not _valid_ipv4(d) for d in dns.split()):
        return jsonify({"error": "Invalid DNS server"}), 400

    subprocess.run(["chmod", "+x", SCRIPT_UTILITIES])
    try:
        # network_pin schedules a detached applier and returns immediately, so
        # this call does not block on the IP change itself.
        subprocess.check_output(
            ["bash", SCRIPT_UTILITIES, "network_pin", ip, prefix, gateway, dns, revert],
            text=True, timeout=15, stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        return jsonify({"error": (e.output or "").strip() or "Failed to schedule static IP"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "status": "applying",
        "new_ip": ip,
        "new_url": f"http://{ip}/",
        "revert_seconds": int(revert),
    })

@app.route("/api/network/confirm", methods=["POST"])
@limiter.limit("10 per minute")
def api_network_confirm():
    """Persist the pending static IP and cancel the auto-revert guard."""
    try:
        subprocess.check_output(
            ["bash", SCRIPT_UTILITIES, "network_confirm"], text=True, timeout=30,
            stderr=subprocess.STDOUT,
        )
        return jsonify({"status": "confirmed"})
    except subprocess.CalledProcessError as e:
        return jsonify({"error": (e.output or "").strip() or "Confirm failed"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/network/dhcp", methods=["POST"])
@limiter.limit("10 per minute")
def api_network_dhcp():
    """Revert addressing to automatic (DHCP)."""
    subprocess.run(["chmod", "+x", SCRIPT_UTILITIES])
    try:
        subprocess.check_output(
            ["bash", SCRIPT_UTILITIES, "network_dhcp"], text=True, timeout=30,
            stderr=subprocess.STDOUT,
        )
        return jsonify({"status": "reverting"})
    except subprocess.CalledProcessError as e:
        return jsonify({"error": (e.output or "").strip() or "Revert failed"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    try:
        migration.run_migrations()
    except Exception as e:
        # Log to file so we can see it, but don't crash the web server
        logging.error(f"CRITICAL: Migration failed: {e}")
        
    # Attempt update before starting web server
    # threading.Thread(target=perform_first_boot_update).start()

    # For local dev only; in production, use Gunicorn
    # app.run(host="0.0.0.0", port=80, debug=True)  # Keep debug=True for dev, but remove in prod    

# Exempt polling endpoints from rate limiting to prevent UI "Request Failed" errors
limiter.exempt(get_task_status)
limiter.exempt(system_status)

# --- Template Injection: Login Gate ---
# We inject the login page dynamically to avoid file dependencies for this critical security feature
from flask import render_template_string
@app.errorhandler(401)
def custom_401(e):
    # Dynamic Title based on state
    title = "Master Access" if is_setup_complete() else "Factory Access"
    hint = "Enter your Master Admin Password." if is_setup_complete() else "Enter the Factory Password found on your device label."

    # Self-contained on purpose — this 401 surface must work even if every
    # template file is broken. Tokens hand-picked to match _theme.html light
    # mode so the page reads consistently with the rest of the dashboard.
    resp = Response(render_template_string("""
    <!DOCTYPE html>
    <html lang="en" data-theme="light"><head><title>HomeBrain Access</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script>
      (function () {
        try {
          var stored = localStorage.getItem('hb-theme');
          var theme = (stored === 'dark' || stored === 'light') ? stored : 'light';
          document.documentElement.setAttribute('data-theme', theme);
        } catch (e) {}
      })();
    </script>
    <style>
      :root, :root[data-theme="light"] {
        --bg:#f4f6f3; --surface:#ffffff; --text:#0f1813; --text-dim:#5b6660;
        --border:#d6dbd3; --accent:#16a34a; --accent-hover:#128039;
        --accent-ring:rgba(22,163,74,0.28); --danger:#b91c1c;
        color-scheme: light;
      }
      :root[data-theme="dark"] {
        --bg:#0e1311; --surface:#181d1a; --text:#e8ede9; --text-dim:#9aa39d;
        --border:#2a3128; --accent:#2ecc71; --accent-hover:#27b765;
        --accent-ring:rgba(46,204,113,0.32); --danger:#ef4444;
        color-scheme: dark;
      }
      * { box-sizing: border-box; }
      html, body { height: 100%; }
      body {
        margin: 0;
        font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        background: var(--bg);
        color: var(--text);
        display: grid;
        place-items: center;
        padding: 24px;
      }
      .card {
        width: 100%;
        max-width: 380px;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 32px 28px;
        box-shadow: 0 1px 0 rgba(15,24,19,0.02), 0 6px 24px rgba(15,24,19,0.06);
      }
      h1 {
        font-size: 1.25rem;
        margin: 0 0 6px;
        font-weight: 600;
        letter-spacing: -0.01em;
      }
      p.hint {
        margin: 0 0 22px;
        font-size: 0.9rem;
        color: var(--text-dim);
      }
      input[type="password"] {
        width: 100%;
        padding: 11px 13px;
        font-size: 0.95rem;
        font-family: inherit;
        background: var(--surface);
        color: var(--text);
        border: 1px solid var(--border);
        border-radius: 8px;
        outline: none;
        transition: border-color 120ms ease, box-shadow 120ms ease;
      }
      input[type="password"]:focus {
        border-color: var(--accent);
        box-shadow: 0 0 0 3px var(--accent-ring);
      }
      button {
        width: 100%;
        margin-top: 14px;
        padding: 11px 13px;
        font-size: 0.95rem;
        font-family: inherit;
        font-weight: 600;
        background: var(--accent);
        color: #ffffff;
        border: 1px solid var(--accent);
        border-radius: 8px;
        cursor: pointer;
        transition: background-color 120ms ease;
      }
      button:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
      button:active { transform: translateY(1px); }
      .err {
        margin: 14px 0 0;
        font-size: 0.85rem;
        color: var(--danger);
        min-height: 1.1em;
      }
      .linkbtn {
        display: inline;
        width: auto;
        margin: 18px 0 0;
        padding: 0;
        background: none;
        border: none;
        color: var(--text-dim);
        font-size: 0.82rem;
        font-weight: 500;
        text-decoration: underline;
        cursor: pointer;
      }
      .linkbtn:hover { background: none; color: var(--accent); }
      input[type="text"], textarea {
        width: 100%;
        padding: 11px 13px;
        font-size: 0.95rem;
        font-family: inherit;
        background: var(--surface);
        color: var(--text);
        border: 1px solid var(--border);
        border-radius: 8px;
        outline: none;
        transition: border-color 120ms ease, box-shadow 120ms ease;
      }
      textarea { resize: vertical; min-height: 64px; margin-bottom: 10px; }
      input[type="text"]:focus, textarea:focus {
        border-color: var(--accent);
        box-shadow: 0 0 0 3px var(--accent-ring);
      }
      .field-label {
        display: block;
        font-size: 0.8rem;
        color: var(--text-dim);
        margin: 12px 0 5px;
      }
      .rule { font-size: 0.78rem; color: var(--text-dim); margin: 6px 0 0; }
      .ok { margin: 14px 0 0; font-size: 0.85rem; color: var(--accent); min-height: 1.1em; }
      .hidden { display: none; }
    </style>
    </head><body>
    <main class="card">
      <div id="login-view">
        <h1>{{ title }}</h1>
        <p class="hint">{{ hint }}</p>
        <form id="f" autocomplete="on">
          <input type="password" name="password" placeholder="Password" required autofocus autocomplete="current-password">
          <button type="submit">Unlock</button>
          <p class="err" id="err"></p>
        </form>
        {% if show_recovery %}
        <button type="button" class="linkbtn" id="show-recovery">Forgot your password?</button>
        {% endif %}
      </div>

      {% if show_recovery %}
      <div id="recovery-view" class="hidden">
        <h1>Recover Access</h1>
        <p class="hint">Enter your recovery phrase and choose a new master password. This resets access to the Dashboard, Nextcloud and Home Assistant — it cannot decrypt individual vault items.</p>
        <form id="rf" autocomplete="off">
          <label class="field-label" for="rec-phrase">Recovery phrase</label>
          <textarea id="rec-phrase" name="phrase" placeholder="six words separated by spaces" required autocomplete="off" spellcheck="false"></textarea>
          <label class="field-label" for="rec-pw">New master password</label>
          <input type="password" id="rec-pw" name="new_password" placeholder="New password" required autocomplete="new-password">
          <label class="field-label" for="rec-pw2">Confirm new password</label>
          <input type="password" id="rec-pw2" placeholder="Repeat new password" required autocomplete="new-password">
          <p class="rule">{{ password_rule }}</p>
          <button type="submit">Reset password</button>
          <p class="err" id="rec-err"></p>
          <p class="ok" id="rec-ok"></p>
        </form>
        <button type="button" class="linkbtn" id="back-login">&larr; Back to login</button>
      </div>
      {% endif %}
    </main>
    <script>
      document.getElementById('f').addEventListener('submit', async (e) => {
        e.preventDefault();
        const err = document.getElementById('err');
        err.textContent = '';
        try {
          const r = await fetch('/login', { method: 'POST', body: new FormData(e.target) });
          const d = await r.json();
          if (d.status === 'success') { location.reload(); }
          else { err.textContent = d.error || 'Login failed'; }
        } catch (ex) {
          err.textContent = 'Network error — try again';
        }
      });
    </script>
    {% if show_recovery %}
    <script>
      const loginView = document.getElementById('login-view');
      const recView = document.getElementById('recovery-view');
      document.getElementById('show-recovery').addEventListener('click', () => {
        loginView.classList.add('hidden'); recView.classList.remove('hidden');
      });
      document.getElementById('back-login').addEventListener('click', () => {
        recView.classList.add('hidden'); loginView.classList.remove('hidden');
      });
      document.getElementById('rf').addEventListener('submit', async (e) => {
        e.preventDefault();
        const err = document.getElementById('rec-err');
        const ok = document.getElementById('rec-ok');
        err.textContent = ''; ok.textContent = '';
        const phrase = document.getElementById('rec-phrase').value;
        const pw = document.getElementById('rec-pw').value;
        const pw2 = document.getElementById('rec-pw2').value;
        if (pw !== pw2) { err.textContent = 'Passwords do not match'; return; }
        try {
          const r = await fetch('/api/recovery/reset', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ phrase: phrase, new_password: pw })
          });
          const d = await r.json();
          if (d.status === 'started') {
            ok.textContent = 'Recovery accepted. Returning to login…';
            setTimeout(() => location.reload(), 3500);
          } else {
            err.textContent = d.error || 'Recovery failed';
          }
        } catch (ex) {
          err.textContent = 'Network error — try again';
        }
      });
    </script>
    {% endif %}
    </body></html>
    """, title=title, hint=hint,
         show_recovery=(is_setup_complete() and _recovery_configured()),
         password_rule=recovery.NEW_PASSWORD_RULE), 401)
    
    # Prevent browser caching of the login gate
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

# --- Robustness: Auto-Resume Setup on Restart ---
def resume_incomplete_setup():
    """Checks if setup was interrupted (e.g. by reboot) and resumes it automatically."""
    try:
        # Simple lock to prevent multiple workers from triggering resume
        resume_lock = os.path.join(tempfile.gettempdir(), "homebrain_resume.lock")
        if os.path.exists(resume_lock):
            return

        if is_setup_started() and not is_setup_complete():
            # Claim lock (cleared on reboot naturally via tmp)
            with open(resume_lock, 'w') as f: f.write("locked")
            
            logging.info("Detected interrupted setup state.")
            
            # If credentials don't exist (neither final nor staging), setup failed too early.
            # We reset the state so the user is redirected to Welcome screen to try again.
            if not os.path.exists(INSTALL_CREDS_PATH) and not os.path.exists(STAGING_CREDS_PATH):
                logging.warning("Credentials missing. Resetting setup state to allow retry.")
                try: os.remove(SETUP_STARTED_MARKER)
                except: pass
                return

            logging.info("Resuming deployment script...")
            # Announce resume in logs for the UI +            
            try:
                with open(LOG_FILES['setup'], "a") as f:
                    f.write(f"\n\n{'='*40}\n SYSTEM RESTART DETECTED: Resuming Installation... \n{'='*40}\n\n")
            except: pass
            
            cmd = f"bash {SCRIPT_DEPLOY} >> {LOG_FILES['setup']} 2>&1"
            threading.Thread(target=run_background_task, args=("Resumed Setup", cmd, "setup")).start()
    except Exception as e:
        logging.error(f"Failed to resume setup: {e}")

# Start resume check in background on app load
threading.Thread(target=resume_incomplete_setup, daemon=True).start()
