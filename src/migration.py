import os
import shutil
import logging
import subprocess
import platform

# Configuration Constants
INSTALL_DIR = "/opt/homebrain"
ENV_FILE = os.path.join(INSTALL_DIR, ".env")
# Boot config path detection (filesystem-based, not platform-based)
FACTORY_CONFIG = "/boot/firmware/factory_config.txt" if os.path.isdir("/boot/firmware") else "/opt/homebrain/factory_config.txt"
SERVICE_TEMPLATE = os.path.join(INSTALL_DIR, "config/homebrain-manager.service")

def get_env_map():
    config = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    key, value = line.strip().split("=", 1)
                    config[key] = value.strip()
    return config

def update_env_file(updates):
    """Updates the .env file with a dictionary of key-value pairs."""
    if not updates:
        return

    # Read existing
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()

    for key, value in updates.items():
        found = False
        new_line = f"{key}={value}\n"
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = new_line
                found = True
                break
        if not found:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(new_line)

    with open(ENV_FILE, "w") as f:
        f.writelines(lines)
    
    # Secure the file
    os.chmod(ENV_FILE, 0o600)


def run_migrations():
    """Main entry point for migration logic."""
    logging.info("Checking for system migrations...")

    # --- Migration 1: Enforce Service Integrity (Golden Copy) ---
    # generalized check: If the installed service file differs from the repo,
    # we overwrite it. This handles Gunicorn, Venv, and any future changes.
    service_file = "/etc/systemd/system/homebrain-manager.service"
    
    if os.path.exists(SERVICE_TEMPLATE):
        needs_sync = False
        
        if not os.path.exists(service_file):
            needs_sync = True
        else:
            # Compare content to detect drift
            try:
                with open(SERVICE_TEMPLATE, 'r') as f1, open(service_file, 'r') as f2:
                    if f1.read().strip() != f2.read().strip():
                        needs_sync = True
            except Exception:
                needs_sync = True

        if needs_sync:
            logging.info("Migration: Service file drift detected. Synchronizing with repository...")
            if os.path.exists(service_file):
                shutil.copy(service_file, service_file + ".bak")
            
            try:
                shutil.copy(SERVICE_TEMPLATE, service_file)
                os.chmod(service_file, 0o644)
                subprocess.run(["systemctl", "daemon-reload"], check=True)
                logging.info("Migration: Service synchronized. Changes will apply on next restart.")
            except Exception as e:
                logging.error(f"Migration: Failed to sync service file: {e}")
    
    # --- Migration 2: Rename Backup Cron File ---
    # Moves /etc/cron.d/nextcloud-backup to /etc/cron.d/homebrain-backup to match new standard
    legacy_cron = "/etc/cron.d/nextcloud-backup"
    new_cron = "/etc/cron.d/homebrain-backup"
    
    if os.path.exists(legacy_cron) and not os.path.exists(new_cron):
        logging.info(f"Migration: Renaming legacy backup cron to {new_cron}...")
        try:
            os.rename(legacy_cron, new_cron)
        except Exception as e:
            logging.error(f"Migration: Failed to rename backup cron: {e}")

    # --- Migration 3: ROCm to Vulkan transition cleanup ---
    # Removes ROCm apt repo, old llama binaries, and stops llama-server for Vulkan reinstall
    rocm_list = "/etc/apt/sources.list.d/rocm.list"
    rocm_gpg = "/etc/apt/keyrings/rocm.gpg"
    old_source_build = "/home/admin/llama.cpp"
    old_prebuilt_dir = "/home/admin/llama-server"

    if os.path.exists(rocm_list) or os.path.exists(old_source_build):
        logging.info("Migration: Cleaning up ROCm artifacts for Vulkan transition...")
        try:
            for f in [rocm_list, rocm_gpg]:
                if os.path.exists(f):
                    os.remove(f)
                    logging.info(f"Migration: Removed {f}")
            if os.path.exists(old_source_build):
                shutil.rmtree(old_source_build, ignore_errors=True)
                logging.info("Migration: Removed old source build directory")
            if os.path.exists(old_prebuilt_dir):
                shutil.rmtree(old_prebuilt_dir, ignore_errors=True)
                logging.info("Migration: Removed old prebuilt directory (will reinstall with Vulkan)")
            subprocess.run(["systemctl", "stop", "llama-server"], capture_output=True)
            subprocess.run(["systemctl", "disable", "llama-server"], capture_output=True)
            logging.info("Migration: ROCm cleanup complete. AI stack will reinstall with Vulkan on next enable.")
        except Exception as e:
            logging.error(f"Migration: ROCm cleanup failed: {e}")
