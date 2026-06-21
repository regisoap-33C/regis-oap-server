import json
import os
import sys
import time
import hashlib
import subprocess
import requests
from datetime import datetime

# ── CAMBIAR esto por tu URL de Railway/Supabase ANTES de compilar ──
# Ejemplo: SERVER_URL_DEFAULT = "https://regis-oap-production.up.railway.app"
SERVER_URL_DEFAULT = "http://localhost:8080"

def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(get_app_dir(), "config_licencia.json")
LICENSE_FILE = os.path.join(get_app_dir(), "sys_oap_license.dat")

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"server_url": SERVER_URL_DEFAULT, "discord_invite": ""}

config = load_config()

def get_hwid():
    try:
        r = subprocess.run(
            "wmic csproduct get uuid",
            capture_output=True, text=True, shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        hwid = r.stdout.strip().split('\n')[-1].strip().upper()
        if hwid:
            return hwid
    except:
        pass
    try:
        r = subprocess.run(
            "vol C:",
            capture_output=True, text=True, shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        vol = r.stdout.strip().upper()
        return hashlib.sha256(vol.encode()).hexdigest()[:16].upper()
    except:
        return "UNKNOWN"

class VerificadorLicenca:
    def __init__(self):
        self.server_url = config.get("server_url", "http://localhost:8080")
        self.discord_invite = config.get("discord_invite", "")
        self.cache = None
        self._load_cache()

    def _load_cache(self):
        try:
            if os.path.exists(LICENSE_FILE):
                with open(LICENSE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                cached_at = data.get("cached_at", 0)
                if time.time() - cached_at < 72 * 3600:
                    self.cache = data
        except:
            pass

    def _save_cache(self, data):
        data["cached_at"] = time.time()
        try:
            with open(LICENSE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
            self.cache = data
        except:
            pass

    def _clear_cache(self):
        self.cache = None
        try:
            if os.path.exists(LICENSE_FILE):
                os.remove(LICENSE_FILE)
        except:
            pass

    def verificar(self, key=None):
        hwid = get_hwid()
        body = {"hwid": hwid}
        if key:
            body["key"] = key
        elif self.cache and self.cache.get("valido") and not self.cache.get("tipo") == "trial":
            body["key"] = self.cache.get("key", "")
        try:
            resp = requests.post(
                f"{self.server_url}/api/verificar",
                json=body, timeout=10
            )
            data = resp.json()
            if data.get("valido"):
                entry = {
                    "key": key or (self.cache.get("key") if self.cache else None),
                    "hwid": hwid,
                    "tier": data["tier"],
                    "tipo": data.get("tipo", "key"),
                    "expiracao": data.get("expiracao", ""),
                    "valido": True
                }
                if not key and self.cache:
                    entry["key"] = self.cache.get("key")
                self._save_cache(entry)
            else:
                if data.get("tipo") == "trial_expirado":
                    self._clear_cache()
            return data
        except requests.exceptions.RequestException:
            if self.cache and self.cache.get("valido"):
                return {"valido": True, "tier": self.cache["tier"], "tipo": "cache",
                        "expiracao": self.cache.get("expiracao", "")}
            return {"valido": False, "erro": "Servidor offline"}

    def ativar_trial(self):
        hwid = get_hwid()
        try:
            resp = requests.post(
                f"{self.server_url}/api/ativar_trial",
                json={"hwid": hwid}, timeout=10
            )
            data = resp.json()
            if data.get("valido"):
                self._save_cache({
                    "key": None, "hwid": hwid, "tier": "fluid",
                    "tipo": "trial", "expiracao": data.get("expiracao", ""),
                    "valido": True
                })
            return data
        except requests.exceptions.RequestException:
            return {"valido": False, "erro": "Servidor offline"}

    @property
    def tier_acesso(self):
        if self.cache and self.cache.get("valido"):
            return self.cache.get("tier", "")
        return ""

    @property
    def tem_licenca(self):
        return self.tier_acesso != ""
