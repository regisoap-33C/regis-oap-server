import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import os
import json
import secrets
import string
import threading
import io
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

CONFIG_FILE = "config_licencia.json"
DB_PATH = "licencias.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

TIERS = {
    "fluid": {"name": "Fluid", "trial_hours": 24, "cor": 0xB080FF, "emoji": "🟣"},
    "advanced": {"name": "Advanced", "trial_hours": 0, "cor": 0x00BFFF, "emoji": "🔵"},
    "absolute": {"name": "Absolute", "trial_hours": 0, "cor": 0xFF4444, "emoji": "🔴"},
}

class Database:
    def __init__(self):
        self.use_pg = bool(DATABASE_URL)
        self.pg_ok = False
        if self.use_pg:
            import psycopg2
            self.psycopg2 = psycopg2
            try:
                conn = self.psycopg2.connect(DATABASE_URL, connect_timeout=5)
                conn.close()
                self.pg_ok = True
                print("[DB] PostgreSQL conectado")
            except Exception as e:
                print(f"[DB] PostgreSQL falhou ({e}), usando SQLite")
                self.use_pg = False

    def get_conn(self):
        if self.use_pg and self.pg_ok:
            return self.psycopg2.connect(DATABASE_URL)
        return sqlite3.connect(DB_PATH)

    def _sql(self, sql: str) -> str:
        if self.use_pg:
            return sql.replace("?", "%s").replace("AUTOINCREMENT", "SERIAL")
        return sql

    def execute(self, sql: str, params=None):
        conn = self.get_conn()
        try:
            c = conn.cursor()
            c.execute(self._sql(sql), params or ())
            conn.commit()
            return c
        finally:
            conn.close()

    def fetchone(self, sql: str, params=None):
        conn = self.get_conn()
        try:
            c = conn.cursor()
            c.execute(self._sql(sql), params or ())
            return c.fetchone()
        finally:
            conn.close()

    def fetchall(self, sql: str, params=None):
        conn = self.get_conn()
        try:
            c = conn.cursor()
            c.execute(self._sql(sql), params or ())
            return c.fetchall()
        finally:
            conn.close()

    def init_db(self):
        keys_sql = """
            CREATE TABLE IF NOT EXISTS keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_code TEXT UNIQUE NOT NULL,
                tier TEXT NOT NULL,
                discord_id TEXT,
                hwid TEXT,
                created_at TEXT NOT NULL,
                activated_at TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """
        trials_sql = """
            CREATE TABLE IF NOT EXISTS trials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hwid TEXT NOT NULL,
                tier TEXT NOT NULL,
                started_at TEXT NOT NULL
            )
        """
        tickets_sql = """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                tier TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            )
        """
        conn = self.get_conn()
        c = conn.cursor()
        c.execute(self._sql(keys_sql))
        c.execute(self._sql(trials_sql))
        if not self.use_pg:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trials_unique ON trials(hwid, tier)")
        c.execute(self._sql(tickets_sql))
        c.execute(self._sql("CREATE INDEX IF NOT EXISTS idx_keys_code ON keys(key_code)"))
        c.execute(self._sql("CREATE INDEX IF NOT EXISTS idx_keys_discord ON keys(discord_id)"))
        c.execute(self._sql("CREATE INDEX IF NOT EXISTS idx_keys_hwid ON keys(hwid)"))
        c.execute(self._sql("CREATE INDEX IF NOT EXISTS idx_trials_hwid ON trials(hwid)"))
        c.execute(self._sql("CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id)"))
        conn.commit()
        conn.close()
        print(f"[DB] Inicializado ({'PostgreSQL' if self.use_pg else 'SQLite'})")

    def insert_key(self, key_code, tier, discord_id=None, created_at=None):
        if created_at is None:
            created_at = datetime.utcnow().isoformat()
        self.execute("INSERT INTO keys (key_code, tier, discord_id, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                     (key_code, tier, discord_id, created_at))

    def find_key(self, key_code):
        return self.fetchone("SELECT tier, discord_id, hwid, status, activated_at FROM keys WHERE key_code = ?", (key_code,))

    def find_key_by_discord(self, discord_id):
        return self.fetchone("SELECT key_code, tier, status, activated_at FROM keys WHERE discord_id = ?", (discord_id,))

    def link_key_to_hwid(self, key_code, hwid, activated_at=None):
        if activated_at is None:
            activated_at = datetime.utcnow().isoformat()
        self.execute("UPDATE keys SET hwid = ?, status = 'used', activated_at = ? WHERE key_code = ?",
                     (hwid, activated_at, key_code))

    def link_key_to_user(self, key_code, discord_id):
        self.execute("UPDATE keys SET discord_id = ? WHERE key_code = ?", (discord_id, key_code))

    def use_key(self, key_code):
        self.execute("UPDATE keys SET status = 'used' WHERE key_code = ? AND status = 'active'", (key_code,))

    def revoke_key(self, key_code):
        self.execute("UPDATE keys SET status = 'revoked' WHERE key_code = ?", (key_code,))

    def list_keys(self, limit=20):
        return self.fetchall("SELECT key_code, tier, status, discord_id, hwid, created_at FROM keys ORDER BY created_at DESC LIMIT ?", (limit,))

    def get_trial(self, hwid, tier):
        return self.fetchone("SELECT started_at FROM trials WHERE hwid = ? AND tier = ?", (hwid, tier))

    def insert_trial(self, hwid, tier, started_at=None):
        if started_at is None:
            started_at = datetime.utcnow().isoformat()
        self.execute("INSERT INTO trials (hwid, tier, started_at) VALUES (?, ?, ?)", (hwid, tier, started_at))

    def create_ticket(self, channel_id, user_id, tier, created_at=None):
        if created_at is None:
            created_at = datetime.utcnow().isoformat()
        self.execute("INSERT INTO tickets (channel_id, user_id, tier, status, created_at) VALUES (?, ?, ?, 'open', ?)",
                     (channel_id, user_id, tier, created_at))

    def find_open_ticket(self, user_id):
        return self.fetchone("SELECT channel_id FROM tickets WHERE user_id = ? AND status = 'open'", (user_id,))

    def close_ticket(self, channel_id):
        self.execute("UPDATE tickets SET status = 'closed' WHERE channel_id = ?", (channel_id,))

db = Database()

DEFAULT_CONFIG = {
    "bot_token": "SEU_TOKEN_AQUI",
    "admin_ids": [123456789012345678],
    "api_port": 8080,
    "server_url": "http://localhost:8080",
    "discord_invite": "https://discord.gg/seuconvite",
    "canal_loja_id": None,
    "mensagem_loja_id": None,
    "categoria_tickets": None,
    "precos": {"fluid": 10, "advanced": 25, "absolute": 50},
    "moeda": "R$",
    "pix": {"chave": "seuemail@email.com", "nome": "Seu Nome", "cidade": "Sua Cidade"}
}

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)

    if os.environ.get("BOT_TOKEN"):
        cfg["bot_token"] = os.environ["BOT_TOKEN"]
    if os.environ.get("ADMIN_IDS"):
        try:
            cfg["admin_ids"] = json.loads(os.environ["ADMIN_IDS"])
        except:
            cfg["admin_ids"] = [int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip().isdigit()]
    if os.environ.get("API_PORT"):
        cfg["api_port"] = int(os.environ["API_PORT"])
    if os.environ.get("SERVER_URL"):
        cfg["server_url"] = os.environ["SERVER_URL"]
    if os.environ.get("DISCORD_INVITE"):
        cfg["discord_invite"] = os.environ["DISCORD_INVITE"]
    if os.environ.get("CANAL_LOJA_ID"):
        cfg["canal_loja_id"] = os.environ["CANAL_LOJA_ID"]
    if os.environ.get("CATEGORIA_TICKETS"):
        cfg["categoria_tickets"] = os.environ["CATEGORIA_TICKETS"]
    if os.environ.get("PRECOS"):
        try:
            cfg["precos"] = json.loads(os.environ["PRECOS"])
        except:
            pass
    if os.environ.get("MOEDA"):
        cfg["moeda"] = os.environ["MOEDA"]
    if os.environ.get("PIX_CHAVE"):
        cfg.setdefault("pix", {})["chave"] = os.environ["PIX_CHAVE"]
    if os.environ.get("PIX_NOME"):
        cfg.setdefault("pix", {})["nome"] = os.environ["PIX_NOME"]
    if os.environ.get("PIX_CIDADE"):
        cfg.setdefault("pix", {})["cidade"] = os.environ["PIX_CIDADE"]

    return cfg

config = load_config()

def generate_key():
    chars = string.ascii_uppercase + string.digits
    return '-'.join(''.join(secrets.choice(chars) for _ in range(4)) for _ in range(4))

def gerar_qrcode_pix(chave, nome, cidade, valor=None):
    try:
        import qrcode
        import crcmod
    except ImportError:
        return None
    nome = nome[:25]
    cidade = cidade[:15]
    valor_str = f"{valor:.2f}" if valor else ""
    payload = f"0002010102122687BR.GOV.BCB.PIX01{chave}520400005303986"
    if valor:
        payload += f"54{len(valor_str):02d}{valor_str}"
    payload += f"5802BR5913{nome}6008{cidade}62070503***6304"
    crc16 = crcmod.predefined.mkCrcFun('crc-16-modbus')
    resto = payload.encode('ascii')
    crc = crc16(resto)
    payload += f"{crc:04X}"
    img = qrcode.make(payload, box_size=6)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

app_flask = Flask(__name__)

@app_flask.route('/api/verificar', methods=['POST'])
def verificar_licenca():
    data = request.get_json(silent=True)
    if not data or 'hwid' not in data:
        return jsonify({"valido": False, "erro": "HWID não fornecido"}), 400
    hwid = data['hwid'].strip().upper()
    key_code = data.get('key', '').strip().upper()
    if not key_code:
        row = db.get_trial(hwid, 'fluid')
        if row:
            started = datetime.fromisoformat(row[0])
            remaining = timedelta(hours=TIERS['fluid']['trial_hours']) - (datetime.utcnow() - started)
            if remaining.total_seconds() > 0:
                return jsonify({"valido": True, "tier": "fluid", "tipo": "trial",
                    "expiracao": (started + timedelta(hours=TIERS['fluid']['trial_hours'])).isoformat(),
                    "horas_restantes": round(remaining.total_seconds() / 3600, 1)})
            return jsonify({"valido": False, "tier": "fluid", "tipo": "trial_expirado",
                "mensagem": "Trial expirou. Compre uma key no Discord."})
        return jsonify({"valido": False, "tipo": "trial_disponivel", "mensagem": "Ative o trial gratuito de 24h!"})
    row = db.find_key(key_code)
    if not row:
        return jsonify({"valido": False, "erro": "Key invalida"}), 200
    tier, discord_id, bound_hwid, status, activated_at = row
    if status == 'revoked':
        return jsonify({"valido": False, "erro": "Key revogada"}), 200
    if status == 'active' and not bound_hwid:
        now = datetime.utcnow().isoformat()
        db.link_key_to_hwid(key_code, hwid, now)
        return jsonify({"valido": True, "tier": tier, "tipo": "key", "primeira_ativacao": True})
    if bound_hwid and bound_hwid != hwid:
        return jsonify({"valido": False, "erro": "Key ja vinculada a outro HWID"}), 200
    return jsonify({"valido": True, "tier": tier, "tipo": "key"})

@app_flask.route('/api/ativar_trial', methods=['POST'])
def ativar_trial():
    data = request.get_json(silent=True)
    if not data or 'hwid' not in data:
        return jsonify({"valido": False, "erro": "HWID nao fornecido"}), 400
    hwid = data['hwid'].strip().upper()
    if db.get_trial(hwid, 'fluid'):
        return jsonify({"valido": False, "erro": "Trial ja utilizado neste HWID"}), 200
    now = datetime.utcnow()
    db.insert_trial(hwid, 'fluid', now.isoformat())
    exp = now + timedelta(hours=TIERS['fluid']['trial_hours'])
    return jsonify({"valido": True, "tier": "fluid", "tipo": "trial",
        "expiracao": exp.isoformat(), "horas_restantes": TIERS['fluid']['trial_hours']})

@app_flask.route('/api/info', methods=['GET'])
def info():
    return jsonify({"sistema": "REGIS OAP Licensing", "versao": "1.0", "discord": config.get("discord_invite", "")})

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

ADMIN_IDS = config.get("admin_ids", [])
CATEGORIA_TICKETS = config.get("categoria_tickets")
PRECOS = config.get("precos", {})
MOEDA = config.get("moeda", "R$")
PIX = config.get("pix", {})

def is_admin(interaction):
    return interaction.user.id in ADMIN_IDS

@bot.event
async def on_ready():
    print(f"[BOT] Online como {bot.user}")
    guild_id = os.environ.get("GUILD_ID", "")
    if guild_id:
        guild = discord.Object(id=int(guild_id))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"[BOT] Comandos sincronizados no servidor {guild_id}")
    else:
        await tree.sync()
    await enviar_loja_persistente()

async def enviar_loja_persistente():
    canal_id = config.get("canal_loja_id")
    if not canal_id:
        return
    channel = bot.get_channel(int(canal_id))
    if not channel:
        return
    embed = criar_embed_loja()
    mensagem_id = config.get("mensagem_loja_id")
    if mensagem_id:
        try:
            msg = await channel.fetch_message(int(mensagem_id))
            await msg.edit(embed=embed, view=TierButton())
            return
        except:
            pass
    msg = await channel.send(embed=embed, view=TierButton())
    config["mensagem_loja_id"] = msg.id
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

def criar_embed_loja():
    embed = discord.Embed(title="REGIS OAP - Loja Oficial", color=0xB080FF)
    embed.description = "Clique no botao do tier desejado para abrir um ticket."
    for t, info in TIERS.items():
        preco = PRECOS.get(t, 0)
        embed.add_field(
            name=f"{info['emoji']} {info['name'].upper()}",
            value=f"Preco: **{MOEDA} {preco:.2f}**\nDescricao: {info.get('desc', 'Otimizacao para Free Fire')}",
            inline=True)
    embed.set_footer(text="REGIS OAP - Licensing v1.0")
    return embed

class TierButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🟣 FLUID", style=discord.ButtonStyle.secondary, custom_id="comprar_fluid", row=0)
    async def btn_fluid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await criar_ticket(interaction, "fluid")

    @discord.ui.button(label="🔵 ADVANCED", style=discord.ButtonStyle.primary, custom_id="comprar_advanced", row=0)
    async def btn_advanced(self, interaction: discord.Interaction, button: discord.ui.Button):
        await criar_ticket(interaction, "advanced")

    @discord.ui.button(label="🔴 ABSOLUTE", style=discord.ButtonStyle.danger, custom_id="comprar_absolute", row=0)
    async def btn_absolute(self, interaction: discord.Interaction, button: discord.ui.Button):
        await criar_ticket(interaction, "absolute")

async def criar_ticket(interaction: discord.Interaction, tier: str):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("Use este comando no servidor.", ephemeral=True)

    existing = db.find_open_ticket(str(interaction.user.id))
    if existing:
        ch = guild.get_channel(int(existing[0]))
        if ch:
            return await interaction.response.send_message(
                f"Voce ja tem um ticket aberto: {ch.mention}. Feche-o antes de criar outro.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    category = None
    if CATEGORIA_TICKETS:
        category = discord.utils.get(guild.categories, id=int(CATEGORIA_TICKETS))
    if not category:
        category = discord.utils.get(guild.categories, name="TICKETS")
    if not category:
        category = await guild.create_category("TICKETS")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    for aid in ADMIN_IDS:
        admin_member = guild.get_member(aid)
        if admin_member:
            overwrites[admin_member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel_name = f"ticket-{interaction.user.name[:20]}-{tier}"
    channel = await category.create_text_channel(
        name=channel_name,
        topic=f"{interaction.user.id}|{tier}",
        overwrites=overwrites
    )

    db.create_ticket(str(channel.id), str(interaction.user.id), tier)

    preco = PRECOS.get(tier, 0)
    tier_info = TIERS.get(tier, {})
    embed = discord.Embed(
        title=f"{tier_info.get('emoji', '')} REGIS OAP - {tier.upper()}",
        description=f"Obrigado pelo interesse, {interaction.user.mention}!",
        color=tier_info.get("cor", 0xB080FF))
    embed.add_field(name="Produto", value=f"**{tier.upper()}**", inline=True)
    embed.add_field(name="Valor", value=f"**{MOEDA} {preco:.2f}**", inline=True)
    embed.add_field(name="Chave Pix", value=f"`{PIX.get('chave', 'N/A')}`", inline=False)
    embed.add_field(name="Instrucoes", value="1. Faca o Pix para a chave acima\n2. Clique em **JA PAGUEI** abaixo\n3. Aguarde o admin confirmar", inline=False)
    embed.set_footer(text=f"Ticket de {interaction.user.name}")
    embed.timestamp = datetime.utcnow()

    qr = gerar_qrcode_pix(PIX.get("chave", ""), PIX.get("nome", ""), PIX.get("cidade", ""), valor=preco)
    qr_file = None
    if qr:
        qr_file = discord.File(qr, filename="pix_qr.png")
        embed.set_image(url="attachment://pix_qr.png")

    view = TicketView(tier)
    msg = await channel.send(embed=embed, view=view, file=qr_file)

    await interaction.followup.send(f"Ticket criado: {channel.mention}", ephemeral=True)

    admin_pings = " ".join(f"<@{aid}>" for aid in ADMIN_IDS)
    await channel.send(f"{admin_pings} Novo ticket {tier.upper()} de {interaction.user.mention}!", delete_after=5)


class TicketView(discord.ui.View):
    def __init__(self, tier: str):
        super().__init__(timeout=None)
        self.tier = tier

    @discord.ui.button(label="✅ JA PAGUEI", style=discord.ButtonStyle.success, custom_id="paguei", row=0)
    async def btn_paguei(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            return await interaction.response.send_message("Apenas o admin pode confirmar.", ephemeral=True)
        await confirmar_pagamento(interaction, self.tier)

    @discord.ui.button(label="🔒 Fechar Ticket", style=discord.ButtonStyle.secondary, custom_id="fechar", row=0)
    async def btn_fechar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            return await interaction.response.send_message("So o admin pode fechar.", ephemeral=True)
        await fechar_ticket(interaction)


async def confirmar_pagamento(interaction: discord.Interaction, tier: str):
    if not interaction.channel:
        return
    topic = interaction.channel.topic
    if not topic or "|" not in topic:
        return await interaction.response.send_message("Erro: ticket invalido.", ephemeral=True)
    user_id, tier_from_topic = topic.split("|", 1)
    user = interaction.guild.get_member(int(user_id))

    key = generate_key()
    db.insert_key(key, tier, user_id)
    db.use_key(key)
    db.close_ticket(str(interaction.channel.id))

    embed = discord.Embed(title="REGIS OAP - Sua Key", color=0x00FF88)
    embed.add_field(name="Tier", value=tier.upper(), inline=True)
    embed.add_field(name="Key", value=f"`{key}`", inline=False)
    embed.set_footer(text="Ative no app ou use /ativar")
    embed.timestamp = datetime.utcnow()

    if user:
        try:
            await user.send(embed=embed)
        except:
            pass

    await interaction.response.send_message(
        f"Pagamento confirmado! Key {tier.upper()} gerada.\n{user.mention if user else ''}",
        ephemeral=False)

    confirm_embed = discord.Embed(title="Pagamento Confirmado!", color=0x00FF88)
    confirm_embed.add_field(name="Key", value=f"`{key}`", inline=False)
    await interaction.channel.send(embed=confirm_embed)
    await fechar_ticket(interaction)


async def fechar_ticket(interaction: discord.Interaction):
    if interaction.channel:
        db.close_ticket(str(interaction.channel.id))
        await interaction.channel.send("Fechando ticket em 3 segundos...")
        await asyncio.sleep(3)
        await interaction.channel.delete()


@tree.command(name="set_loja", description="[ADMIN] Define este canal como loja permanente")
async def set_loja(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("Sem permissao.", ephemeral=True)
    config["canal_loja_id"] = str(interaction.channel_id)
    config["mensagem_loja_id"] = None
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
    embed = criar_embed_loja()
    msg = await interaction.channel.send(embed=embed, view=TierButton())
    config["mensagem_loja_id"] = msg.id
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
    await interaction.response.send_message("Loja configurada neste canal!", ephemeral=True)

@tree.command(name="loja", description="Abre a loja para comprar keys")
async def loja(interaction: discord.Interaction):
    embed = criar_embed_loja()
    await interaction.response.send_message(embed=embed, view=TierButton(), ephemeral=False)


@tree.command(name="gerar_key", description="[ADMIN] Gera uma ou mais keys")
@app_commands.choices(tier=[
    app_commands.Choice(name="Fluid", value="fluid"),
    app_commands.Choice(name="Advanced", value="advanced"),
    app_commands.Choice(name="Absolute", value="absolute"),
])
async def gerar_key(interaction: discord.Interaction, tier: str, quantidade: int = 1):
    if not is_admin(interaction):
        return await interaction.response.send_message("Sem permissao.", ephemeral=True)
    if quantidade < 1 or quantidade > 20:
        return await interaction.response.send_message("Quantidade entre 1 e 20.", ephemeral=True)
    keys = []
    for _ in range(quantidade):
        key = generate_key()
        db.insert_key(key, tier)
        keys.append(key)
    msg = f"**{quantidade}x Key(s) {tier.upper()}:**\n" + "\n".join(f"`{k}`" for k in keys)
    try:
        await interaction.user.send(msg)
        await interaction.response.send_message("Keys enviadas no DM!", ephemeral=True)
    except:
        await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="enviar_key", description="[ADMIN] Gera e envia key direto pro cliente via DM")
@app_commands.choices(tier=[
    app_commands.Choice(name="Fluid", value="fluid"),
    app_commands.Choice(name="Advanced", value="advanced"),
    app_commands.Choice(name="Absolute", value="absolute"),
])
async def enviar_key(interaction: discord.Interaction, tier: str, usuario: discord.Member):
    if not is_admin(interaction):
        return await interaction.response.send_message("Sem permissao.", ephemeral=True)
    key = generate_key()
    db.insert_key(key, tier, str(usuario.id))
    embed = discord.Embed(title="REGIS OAP - Sua Key", color=0x00FF88)
    embed.add_field(name="Tier", value=tier.upper(), inline=True)
    embed.add_field(name="Key", value=f"`{key}`", inline=False)
    embed.set_footer(text="Ative no app ou use /ativar")
    try:
        await usuario.send(embed=embed)
        await interaction.response.send_message(f"Key {tier.upper()} enviada para {usuario.mention} via DM!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"Nao consegui enviar DM. A key: `{key}`", ephemeral=True)


@tree.command(name="keys", description="[ADMIN] Lista as ultimas keys")
async def listar_keys(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("Sem permissao.", ephemeral=True)
    rows = db.list_keys(20)
    if not rows:
        return await interaction.response.send_message("Nenhuma key.", ephemeral=True)
    embed = discord.Embed(title="Keys", color=0xB080FF)
    for k, t, s, did, hwid, ca in rows:
        emoji = {"active": "🟢", "used": "🔵", "revoked": "🔴"}.get(s, "⚪")
        val = f"Tier: {t} | Status: {s}"
        if did: val += f" | <@{did}>"
        if hwid: val += f" | HWID: {hwid[:8]}..."
        embed.add_field(name=f"{emoji} {k}", value=val, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="revogar", description="[ADMIN] Revoga uma key")
async def revogar_key(interaction: discord.Interaction, key: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("Sem permissao.", ephemeral=True)
    db.revoke_key(key.upper())
    await interaction.response.send_message(f"Key `{key.upper()}` revogada.", ephemeral=True)


@tree.command(name="ativar", description="Vincula uma key ao seu Discord")
async def ativar_key(interaction: discord.Interaction, key: str):
    key = key.upper().strip()
    row = db.find_key(key)
    if not row:
        return await interaction.response.send_message("Key invalida.", ephemeral=True)
    tier, _, discord_id, status, _ = row
    if status == "revoked":
        return await interaction.response.send_message("Key revogada.", ephemeral=True)
    if discord_id and discord_id != str(interaction.user.id):
        return await interaction.response.send_message("Key ja vinculada a outro usuario.", ephemeral=True)
    if not discord_id:
        db.link_key_to_user(key, str(interaction.user.id))
        db.use_key(key)
    role_name = tier.capitalize()
    if interaction.guild:
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            try:
                await interaction.user.add_roles(role)
            except:
                pass
    embed = discord.Embed(title="Key Ativada!", color=0x00FF88)
    embed.add_field(name="Tier", value=tier.upper(), inline=True)
    embed.set_footer(text="REGIS OAP - Licensing")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="minha_key", description="Mostra sua key ativa")
async def minha_key(interaction: discord.Interaction):
    row = db.find_key_by_discord(str(interaction.user.id))
    if not row:
        return await interaction.response.send_message("Nenhuma key ativada.", ephemeral=True)
    k, t, s, aa = row
    embed = discord.Embed(title="Sua Licenca", color=0xB080FF)
    embed.add_field(name="Key", value=f"`{k}`", inline=True)
    embed.add_field(name="Tier", value=t.upper(), inline=True)
    embed.add_field(name="Status", value=s, inline=True)
    if aa: embed.add_field(name="Ativada em", value=aa, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="ajuda", description="Info sobre licencas")
async def ajuda(interaction: discord.Interaction):
    embed = discord.Embed(title="REGIS OAP - Licencas", color=0xB080FF)
    embed.add_field(name="Tiers", value="**Fluid** - Trial 24h gratis, depois key\n**Advanced** - Requer key\n**Absolute** - Requer key", inline=False)
    embed.add_field(name="Comandos", value="`/loja` - Abrir loja\n`/ativar <key>` - Ativar key\n`/minha_key` - Ver licenca\n`/ajuda` - Ajuda", inline=False)
    embed.add_field(name="Comprar", value=f"Use `/loja` ou entre no servidor: {config.get('discord_invite', 'Discord')}", inline=False)
    await interaction.response.send_message(embed=embed)


import asyncio

def run_api():
    app_flask.run(host="0.0.0.0", port=config.get("api_port", 8080), debug=False, use_reloader=False)

def run_bot():
    bot.run(config["bot_token"])

if __name__ == "__main__":
    db.init_db()
    print("=== REGIS OAP Licensing Server ===")
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
    print(f"[API] Rodando na porta {config.get('api_port', 8080)}")
    print(f"[BOT] Conectando Discord...")
    run_bot()
