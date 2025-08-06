import os, re, discord, aiosqlite
from discord.ext import commands, tasks
from discord import ui
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiohttp import web

# ─────────────────── CONFIG ───────────────────
load_dotenv()
TOKEN             = os.getenv("DISCORD_TOKEN")
GUILD_ID          = int(os.getenv("GUILD_ID"))
HISTORY_CHANNEL   = int(os.getenv("HISTORY_CHANNEL"))
LOG_CHANNEL       = int(os.getenv("LOG_CHANNEL", 1386775183762657280))
EXCHANGE_CHANNEL  = int(os.getenv("EXCHANGE_CHANNEL"))
EXCHANGE_CATEGORY = os.getenv("EXCHANGE_CATEGORY", "Needs Convert")

# new channels & roles
VC_TOTAL_ID       = int(os.getenv("VC_TOTAL_ID", 1386770317497864425))
LB_EXCH_ID        = int(os.getenv("LB_EXCH_ID", 1402479459843575860))
LB_CUST_ID        = int(os.getenv("LB_CUST_ID", 1402479617641812058))

# styling & fees
BRAND_BLUE = 0x1E90FF
MIN_FEE    = 3.0

# role → max limit (None = unlimited)
LIMITS = {
    "CAN EXCHANGE ANY AMOUNT":        None,
    "Dont Exchange 250+ (NEVER DM)":  250.0,
    "Dont Exchange 100+ (NEVER DM)":  100.0,
}

PAYMENT_METHODS = [
    ("PayPal",   "10 % Fee", "<:paypal:1386798710276755557>"),
    ("Venmo",    "10 % Fee", "<:venmo:1386798812173172796>"),
    ("ApplePay", "10 % Fee", "<:applepay:1386799029802893394>"),
    ("Zelle",    "10 % Fee", "<:zelle:1386799028611842068>"),
    ("Chime",    "10 % Fee", "<:chime:1386799209264578731>"),
    ("Cashapp",  "10 % Fee", "<:cashapp:1386799345344708648>"),
    ("Crypto",    "5 % Fee", "<:crypto:1386799490198933625>"),
]

# ─────────── DATABASE UTILITIES ───────────
DB_PATH = "data.sqlite"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS global_total (
              id    INTEGER PRIMARY KEY CHECK(id=1),
              total REAL NOT NULL
            );
        """)
        await db.execute("INSERT OR IGNORE INTO global_total(id,total) VALUES(1,0);")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_totals (
              user_id      INTEGER PRIMARY KEY,
              as_exchanger REAL NOT NULL DEFAULT 0,
              as_customer  REAL NOT NULL DEFAULT 0
            );
        """)
        await db.commit()

async def add_exchange(exchanger_id: int, customer_id: int, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        # update global total
        await db.execute("UPDATE global_total SET total = total + ? WHERE id = 1;", (amount,))
        # update per-user fields
        await db.execute(
            "INSERT INTO user_totals(user_id,as_exchanger,as_customer) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET as_exchanger = as_exchanger + excluded.as_exchanger;",
            (exchanger_id, amount, 0)
        )
        await db.execute(
            "INSERT INTO user_totals(user_id,as_exchanger,as_customer) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET as_customer = as_customer + excluded.as_customer;",
            (customer_id, 0, amount)
        )
        await db.commit()

async def fetch_leaderboard(field: str, limit: int = 5):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT user_id,{field} FROM user_totals ORDER BY {field} DESC LIMIT ?;", (limit,)
        )
        return await cursor.fetchall()

async def get_global_total():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT total FROM global_total WHERE id=1;")
        row = await cursor.fetchone()
        return row[0] if row else 0.0

# ───────────── HELPERS ─────────────
def calculate_fee(amount: float, method: str) -> tuple[float,float]:
    m = method.lower()
    if m == "crypto":
        fee = MIN_FEE if amount < 60 else amount * 0.05
    else:
        fee = MIN_FEE if amount < 30 else amount * 0.10
    fee = round(fee,2)
    return fee, round(amount-fee,2)

def user_limit(member: discord.Member) -> float|None:
    # pick lowest numeric limit, or None if unlimited
    vals = [LIMITS[r.name] for r in member.roles if r.name in LIMITS]
    if None in vals: return None
    return min(vals) if vals else 0.0

def has_exchanger(m: discord.Member) -> bool:
    return any(r.name in LIMITS for r in m.roles)

async def log_event(guild: discord.Guild, *, title: str, desc: str, colour: int = BRAND_BLUE):
    ch = guild.get_channel(LOG_CHANNEL)
    if not ch: return
    e = discord.Embed(title=title, description=desc, colour=colour, timestamp=datetime.now(timezone.utc))
    await ch.send(embed=e)

def make_history_embed(*, exchanger: str, client_sent: str, client_received: str, thumb_url: str|None=None) -> discord.Embed:
    now = datetime.now(timezone.utc)
    e = discord.Embed(
        title="✅ Exchange Complete ⚡",
        description=f"**<t:{int(now.timestamp())}:R>**",
        colour=BRAND_BLUE,
        timestamp=now
    )
    e.add_field(name="Exchanger",       value=exchanger,       inline=False)
    e.add_field(name="Client Sent",     value=client_sent,     inline=False)
    e.add_field(name="Client Received", value=client_received, inline=False)
    e.add_field(name="Client Hidden",   value="*For security purposes*", inline=False)
    if thumb_url: e.set_thumbnail(url=thumb_url)
    return e

def setup_embed() -> discord.Embed:
    desc = (
        "You can request a convert by selecting the appropriate option below for the payment type you'll be sending with. "
        "Follow the instructions and fill out the fields as requested.\n\n"
        "• **Reminder**\n"
        "Please read our <#1386775866385760378> before creating a Convert.\n\n"
        "• **Minimum Fees**\n"
        "Our minimum service fee is ~~$5.00~~ **$3.00** (Launch Flash Sale!) and is non-negotiable."
    )
    e = discord.Embed(title="Convert", description=desc, colour=BRAND_BLUE)
    methods_list = "\n".join(f"{icon} {name}" for name,_f,icon in PAYMENT_METHODS)
    e.add_field(name="Available Methods", value=methods_list, inline=False)
    e.set_footer(text="Select a method below to begin ↴")
    return e

# ───────────── HEALTHCHECK ─────────────
async def health(request): return web.Response(text="OK")
async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner,"0.0.0.0",8080)
    await site.start()

# ───────────── VIEWS & MODALS ─────────────
class PaymentFrom(ui.Select):
    def __init__(self):
        super().__init__(placeholder="From…", custom_id="from_method",
                         min_values=1,max_values=1,
                         options=[discord.SelectOption(label=n,description=f,emoji=i)
                                  for n,f,i in PAYMENT_METHODS])
    async def callback(self, inter):
        v=SetupView();v.from_method=self.values[0];v.clear_items();v.add_item(PaymentTo(v))
        await inter.response.send_message(embed=discord.Embed(title="🔁 From selected",
            description=f"{v.from_method} — now pick **To**",colour=BRAND_BLUE),view=v,ephemeral=True)
class PaymentTo(ui.Select):
    def __init__(self,parent:SetupView):
        super().__init__(placeholder="To…",min_values=1,max_values=1,
                         options=[discord.SelectOption(label=n,description=f,emoji=i)
                                  for n,f,i in PAYMENT_METHODS if n!=parent.from_method])
        self.parent=parent
    async def callback(self,inter):
        self.parent.to_method=self.values[0]
        await inter.response.send_modal(AmountModal(self.parent))
class SetupView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.from_method=None;self.to_method=None
        self.add_item(PaymentFrom())

class AmountModal(ui.Modal,title="💰 Enter Amount ⚡"):
    amount=ui.TextInput(label="USD Amount",placeholder="e.g. 200.00",required=True)
    def __init__(self,parent:SetupView):super().__init__();self.parent=parent
    async def on_submit(self,inter):
        try:amt=float(self.amount.value.replace(',','').replace('$',''))
        except: return await inter.response.send_message("❌ Enter a valid number.",ephemeral=True)
        fee,net=calculate_fee(amt,self.parent.from_method)
        g=inter.guild;cat=discord.utils.get(g.categories,name=EXCHANGE_CATEGORY) or await g.create_category(EXCHANGE_CATEGORY)
        perms={g.default_role:discord.PermissionOverwrite(view_channel=False),
               inter.user:discord.PermissionOverwrite(view_channel=True,send_messages=True),
               g.me:discord.PermissionOverwrite(view_channel=True,send_messages=True)}
        exch=discord.utils.get(g.roles,name="Exchanger")
        if exch: perms[exch]=discord.PermissionOverwrite(view_channel=True,send_messages=True)
        suffix=str(int(amt)) if amt.is_integer() else f"{amt:.2f}".replace('.', '-')
        name=f"{self.parent.from_method.lower()}-{self.parent.to_method.lower()}-{suffix}"
        chan=await g.create_text_channel(name,category=cat,overwrites=perms)
        emb=discord.Embed(title="🆕 New Exchange Request",colour=BRAND_BLUE)
        emb.add_field(name="From → To",value=f"{self.parent.from_method} → {self.parent.to_method}",inline=False)
        emb.add_field(name="Amount",value=f"$ {amt:.2f}")
        emb.add_field(name="Fee",value=f"$ {fee:.2f}")
        emb.add_field(name="You Receive",value=f"$ {net:.2f}")
        emb.set_footer(text="Min $3 + 10% (5% crypto) fee • ⚡ Exchangers: Claim / Close")
        await chan.send("@everyone **New ticket!**",allowed_mentions=discord.AllowedMentions(everyone=True),embed=emb,view=TicketView(amt,fee,net))
        await inter.response.send_message(f"✅ Ticket created: {chan.mention}",ephemeral=True)
        await log_event(g,title="Ticket created",desc=f"{inter.user.mention} opened {chan.mention} for {self.parent.from_method} → {self.parent.to_method} at ${amt:.2f}")

class TicketView(ui.View):
    def __init__(self, amt:float, fee:float, net:float):
        super().__init__(timeout=None)
        self.amt,self.fee,self.net=amt,fee,net
    @ui.button(label="🏷️ Claim",style=discord.ButtonStyle.primary)
    async def claim(self, inter, _):
        if not has_exchanger(inter.user):
            return await inter.response.send_message("🚫 Exchanger role required.",ephemeral=True)
        limit=user_limit(inter.user)
        if limit is not None and self.amt>limit:
            over=self.amt-limit
            dr=discord.Embed(title="⚠️ Claim Request - Limit Exceeded",colour=discord.Color.gold(),
                description=(f"**{inter.user.mention}** wants to claim your **${self.amt:.2f}** ticket but exceeds their limit ${limit:.2f} (over by ${over:.2f}).\n\n"
                              "🚨 **RISK**: No refund if exchanger exits.\n\n"
                              "**Accept** or **Deny**."))
            view=ClaimRequestView(opener=inter.message.author,exchanger=inter.user,channel=inter.channel,amt=self.amt)
            msg=await inter.response.send_message(content=f"{inter.message.author.mention}, {inter.user.mention} requests claim:",embed=dr,view=view,ephemeral=False)
            view.msg=msg;return
        # within limit
        exch=discord.utils.get(inter.guild.roles,name="Exchanger")
        if exch: await inter.channel.set_permissions(exch,view_channel=False)
        await inter.channel.set_permissions(inter.user,view_channel=True,send_messages=True)
        emb=inter.message.embeds[0].copy()
        emb.add_field(name="🔒 Claimed by",value=inter.user.mention,inline=False)
        await inter.response.edit_message(embed=emb,view=ClaimedView(self.amt,self.fee,self.net,inter.message.embeds[0]))
        await log_event(inter.guild,title="Ticket claimed",desc=f"{inter.user.mention} claimed {inter.channel.mention}")
    @ui.button(label="✏️ Change Amount",style=discord.ButtonStyle.secondary)
    async def change_amount(self,inter,_):
        await inter.response.send_modal(ChangeAmountModal(self))
    @ui.button(label="⚙️ Change Fee",style=discord.ButtonStyle.secondary)
    async def change_fee(self,inter,_):
        await inter.response.send_modal(ChangeFeeModal(self))
    @ui.button(label="🗑️ Close",style=discord.ButtonStyle.danger)
    async def close(self,inter,_):
        if not has_exchanger(inter.user):return await inter.response.send_message("🚫 Exchangers only.",ephemeral=True)
        await inter.response.send_message("Sure? This will close.",view=ConfirmClose(inter.channel,inter.user),ephemeral=True)

class ChangeAmountModal(ui.Modal,title="Change Amount ⚡"):
    new_amount=ui.TextInput(label="New USD Amount",placeholder="e.g. 200.00",required=True)
    def __init__(self,parent):super().__init__();self.parent=parent
    async def on_submit(self,inter):
        try:na=float(self.new_amount.value)
        except: return await inter.response.send_message("❌ Invalid amount.",ephemeral=True)
        fee,net=calculate_fee(na,"")
        self.parent.amt, self.parent.fee, self.parent.net=na,fee,net
        emb=inter.message.embeds[0]
        emb.set_field_at(1,name="Amount",value=f"$ {na:.2f}")
        emb.set_field_at(2,name="Fee",value=f"$ {fee:.2f}")
        emb.set_field_at(3,name="You Receive",value=f"$ {net:.2f}")
        await inter.response.edit_message(embed=emb,view=self.parent)

class ChangeFeeModal(ui.Modal,title="Change Fee ⚙️"):
    new_fee=ui.TextInput(label="New Fee",placeholder="e.g. 5.00",required=True)
    def __init__(self,parent):super().__init__();self.parent=parent
    async def on_submit(self,inter):
        try:nf=float(self.new_fee.value)
        except:return await inter.response.send_message("❌ Invalid fee.",ephemeral=True)
        self.parent.fee, self.parent.net = nf, round(self.parent.amt-nf,2)
        emb=inter.message.embeds[0]
        emb.set_field_at(2,name="Fee",value=f"$ {nf:.2f}")
        emb.set_field_at(3,name="You Receive",value=f"$ {self.parent.net:.2f}")
        await inter.response.edit_message(embed=emb,view=self.parent)

class ClaimRequestView(ui.View):
    def __init__(self, opener, exchanger, channel, amt):
        super().__init__(timeout=86400)
        self.opener, self.exchanger, self.channel, self.amt = opener, exchanger, channel, amt
    @ui.button(label="✅ Accept",style=discord.ButtonStyle.success)
    async def accept(self,inter,_):
        exch=discord.utils.get(inter.guild.roles,name="Exchanger")
        if exch: await self.channel.set_permissions(exch,view_channel=False)
        await self.channel.set_permissions(self.exchanger,view_channel=True,send_messages=True)
        emb=self.channel.last_message.embeds[0].copy()
        emb.add_field(name="🔒 Claimed by",value=self.exchanger.mention,inline=False)
        await inter.response.edit_message(embed=emb,view=ClaimedView(self.amt, *()),)
    @ui.button(label="❌ Deny",style=discord.ButtonStyle.danger)
    async def deny(self,inter,_):
        await inter.response.edit_message(content="❌ Denied—ticket remains open.",view=None)

class ClaimedView(ui.View):
    def __init__(self, amt, fee, net, orig_embed):
        super().__init__(timeout=None)
        self.amt,self.fee,self.net,self.orig=amt,fee,net,orig_embed
    @ui.button(label="🔄 Unclaim",style=discord.ButtonStyle.secondary)
    async def unclaim(self,inter,_):
        exch=discord.utils.get(inter.guild.roles,name="Exchanger")
        if exch: await inter.channel.set_permissions(exch,view_channel=True)
        await inter.channel.set_permissions(inter.user,overwrite=None)
        await inter.response.edit_message(embed=self.orig,view=TicketView(self.orig.fields[1].value, self.orig.fields[2].value, self.orig.fields[3].value))
    @ui.button(label="✅ Complete",style=discord.ButtonStyle.success)
    async def complete(self,inter,_):
        view=ConfirmCompleteTicket(inter.message.channel,inter.user,self.amt)
        await inter.response.send_message("Really mark complete?",view=view,ephemeral=True)

class ConfirmClose(ui.View):
    def __init__(self, channel, user):
        super().__init__(timeout=30);self.chan,self.user=channel,user
    @ui.button(label="Yes, close",style=discord.ButtonStyle.danger)
    async def yes(self,inter,_):
        if inter.user!=self.user: return await inter.response.send_message("Not authorized.",ephemeral=True)
        await log_event(inter.guild,title="Ticket closed",desc=f"{self.user.mention} closed {self.chan.mention}",colour=0xFF4500)
        await inter.response.edit_message(content="Closed 🔒",view=None);await self.chan.delete()
    @ui.button(label="Cancel",style=discord.ButtonStyle.secondary)
    async def no(self,inter,_): await inter.response.edit_message(content="Cancel.",view=None)

class ConfirmCompleteTicket(ui.View):
    def __init__(self, channel, exchanger, amt): super().__init__(timeout=30);self.chan, self.exchanger, self.amt = channel, exchanger, amt
    @ui.button(label="Yes, complete",style=discord.ButtonStyle.success)
    async def yes(self,inter,_):
        # log history
        fee,net=calculate_fee(self.amt,"")
        emb=make_history_embed(exchanger=self.exchanger.mention,client_sent=f"**$ {self.amt:.2f}**",client_received=f"**$ {net:.2f}**",thumb_url=self.exchanger.display_avatar.url)
        await inter.guild.get_channel(HISTORY_CHANNEL).send(embed=emb)
        # update DB
        await add_exchange(self.exchanger.id, self.chan.category.members[0].id if self.chan.category.members else 0, self.amt)
        # update voice channel
        total=await get_global_total();vc=inter.guild.get_channel(VC_TOTAL_ID)
        if vc and isinstance(vc,discord.VoiceChannel): await vc.edit(name=f"Total Converted: ${total:,.2f}")
        await log_event(inter.guild,title="Exchange completed",desc=f"{self.chan.mention} by {self.exchanger.mention}",colour=0x00C853)
        await inter.response.edit_message(content="Logged ✅ — closing…",view=None)
        await self.chan.delete()
    @ui.button(label="Cancel",style=discord.ButtonStyle.secondary)
    async def cancel(self,inter,_): await inter.response.edit_message(content="Canceled.",view=None)

# ───────────── BOT STARTUP ─────────────
intents=discord.Intents.default();intents.members=True
bot=commands.Bot(command_prefix="!",intents=intents)

@bot.event
async def setup_hook():
    await init_db()
    bot.loop.create_task(start_health_server())
    bot.add_view(SetupView())

@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"🔌 Logged in as {bot.user}")
    # refresh panel
    chan=bot.get_channel(EXCHANGE_CHANNEL)
    if chan:
        async for m in chan.history(limit=50):
            if m.author==bot.user and m.embeds and m.embeds[0].title=="Convert":
                await m.delete()
        await chan.send(embed=setup_embed(),view=SetupView())
    # start leaderboard loop
    update_leaderboards.start()

@tasks.loop(minutes=5)
async def update_leaderboards():
    g=bot.get_guild(GUILD_ID)
    exch_ch, cust_ch = g.get_channel(LB_EXCH_ID), g.get_channel(LB_CUST_ID)
    top_ex=await fetch_leaderboard("as_exchanger",5)
    top_cu=await fetch_leaderboard("as_customer",5)
    emb_ex=discord.Embed(title="🏆 All-Time Top Exchangers",colour=BRAND_BLUE,
        description="\n".join(f"**{i+1}.** <@{uid}> — ${amt:,.2f}" for i,(uid,amt) in enumerate(top_ex)) or "No data.")
    emb_cu=discord.Embed(title="🥇 All-Time Top Customers",colour=BRAND_BLUE,
        description="\n".join(f"**{i+1}.** <@{uid}> — ${amt:,.2f}" for i,(uid,amt) in enumerate(top_cu)) or "No data.")
    async def edit_or_send(ch,emb):
        msg=None
        async for m in ch.history(limit=10):
            if m.author==bot.user and m.embeds and m.embeds[0].title==emb.title:
                msg=m;break
        if msg: await msg.edit(embed=emb)
        else: await ch.send(embed=emb)
    if exch_ch: await edit_or_send(exch_ch,emb_ex)
    if cust_ch: await edit_or_send(cust_ch,emb_cu)

@bot.tree.command(name="exchange",description="Show the Convert panel",guild=discord.Object(id=GUILD_ID))
async def exchange_cmd(inter):
    await inter.response.send_message(embed=setup_embed(),view=SetupView(),ephemeral=True)

bot.run(TOKEN)