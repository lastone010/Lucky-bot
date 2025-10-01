"""
Discord Bets Bot (Python / discord.py)

Features:
- Watches a channel named "bets" for messages with the format "1. X vs 2. Y" and auto-adds 1️⃣ and 2️⃣ reactions.
- Users may react (places a default small stake) or use a slash command to place a custom stake.
- Balances and bets stored in a local SQLite file (async via aiosqlite).
- Payouts use a parimutuel-style distribution with a small underdog bonus if the less-popular side wins.
- Commands: /balance, /resolve, /addcoins, /help and others
- Permissions: only message author, server admins (Manage Messages) or the configured owner can resolve.

Requirements:
  Python 3.8+
  pip install -U discord.py aiosqlite

Environment variables (or edit constants below):
  OWNER_ID       = your user id (optional but recommended)
  GUILD_ID       = optional for guild-scoped command registration during testing

Run:
  python discord_bets_bot.py

Notes:
- New users start with DEFAULT_START_BALANCE coins.
- Default reaction stake is DEFAULT_REACTION_STAKE coins.
- Balances cannot go below zero.

"""

import os
import re
import asyncio
import math
import aiosqlite
import discord
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import commands

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "file.env"))
TOKEN = os.getenv("DISCORD_TOKEN")

# Keeps track of users waiting to enter stake amounts in DMs
pending_stakes = {}

# --- Configuration ---
OWNER_ID = int(os.environ.get('578947145768370198')) if os.environ.get('OWNER_ID') else None
GUILD_ID = int(os.environ.get('GUILD_ID')) if os.environ.get('GUILD_ID') else None
DB_PATH = os.environ.get('DB_PATH', 'bets.db')
BETS_CHANNEL_NAME = '🎰〡bets'
DEFAULT_START_BALANCE = 100
DEFAULT_REACTION_STAKE = 1

# --- Database helpers (async) ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                balance INTEGER NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bets (
                message_id TEXT,
                user_id TEXT,
                choice INTEGER,
                amount INTEGER,
                resolved INTEGER DEFAULT 0,
                PRIMARY KEY(message_id, user_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS highest_bet (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                user_id TEXT,
                message_id TEXT,
                choice INTEGER,
                amount INTEGER
            )
        ''')
        await db.commit()

async def get_user_balance(user_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            # create default balance
            await set_user_balance(user_id, DEFAULT_START_BALANCE)
            return DEFAULT_START_BALANCE
        return int(row[0])

async def set_user_balance(user_id: str, new_balance: int) -> int:
    safe_bal = max(1, int(math.floor(new_balance)))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT INTO users(user_id, balance) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance', (user_id, safe_bal))
        await db.commit()
    return safe_bal

async def place_bet(message_id: str, user_id: str, choice: int, amount: int):
    amount = int(math.floor(amount))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT amount FROM bets WHERE message_id = ? AND user_id = ?', (message_id, user_id))
        row = await cur.fetchone()
        await cur.close()
        if row is not None:
            raise ValueError('You already placed a bet on this message.')

        # Insert new bet
        await db.execute(
            'INSERT INTO bets(message_id, user_id, choice, amount) VALUES(?, ?, ?, ?)',
            (message_id, user_id, choice, amount)
        )

        # Update highest bet record
        cur = await db.execute('SELECT amount FROM highest_bet WHERE id = 1')
        record = await cur.fetchone()
        await cur.close()
        if record is None or amount > record[0]:
            await db.execute(
                '''INSERT INTO highest_bet (id, user_id, message_id, choice, amount)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       user_id=excluded.user_id,
                       message_id=excluded.message_id,
                       choice=excluded.choice,
                       amount=excluded.amount''',
                (user_id, message_id, choice, amount)
            )

        await db.commit()

async def get_bets_for_message(message_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT message_id, user_id, choice, amount, resolved FROM bets WHERE message_id = ?', (message_id,))
        rows = await cur.fetchall()
        await cur.close()
        return [dict(message_id=r[0], user_id=r[1], choice=int(r[2]), amount=int(r[3]), resolved=int(r[4])) for r in rows]

async def mark_bets_resolved(message_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE bets SET resolved=1 WHERE message_id = ?', (message_id,))
        await db.commit()

# --- Bot setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or("?"), intents=intents)  # no prefix usage
bot.remove_command("help")  # remove default help
tree = bot.tree

BET_PATTERN = re.compile(r'^\s*1\.\s*(.+?)\s+vs\s+2\.\s*(.+?)\s*$', re.IGNORECASE)

@bot.event
async def on_ready():
    await init_db()
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    # sync commands to a guild for faster development if provided, else global
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            await tree.sync(guild=guild)
            print(f'Synced commands to guild {GUILD_ID}')
        else:
            await tree.sync()
            print('Synced global commands (may take up to 1 hour to appear)')
    except Exception as e:
        print('Failed to sync commands:', e)

# Utility to find bets channel
def find_bets_channel(guild: discord.Guild):
    for ch in guild.text_channels:
        if ch.name == BETS_CHANNEL_NAME:
            return ch
    return None

# Auto-react when a message in #bets matches the pattern
@bot.event
@bot.event
async def on_message(message: discord.Message):
    # no process_commands call, we’re slash-only
    if message.author.bot:
        return

    # Auto reactions in #bets
    if message.guild and message.channel.name == BETS_CHANNEL_NAME and BET_PATTERN.match(message.content):
        try:
            await message.add_reaction("1️⃣")
            await message.add_reaction("2️⃣")
        except Exception as e:
            print("Failed to add reactions:", e)
        return

    # DM stake input
    if isinstance(message.channel, discord.DMChannel):
        user_id = message.author.id
        if user_id not in pending_stakes:
            return

        try:
            amount = int(message.content.strip())
        except ValueError:
            await message.channel.send("Please enter a valid number for your bet.")
            return

        msg_id, choice = pending_stakes.pop(user_id)  # remove pending after valid reply

        # Check balance
        bal = await get_user_balance(str(user_id))
        if amount > bal:
            await message.channel.send(f"You only have {bal} coins, you cannot bet {amount}.")
            # put stake back so they can try again
            pending_stakes[user_id] = (msg_id, choice)
            return
        if amount < 1:
            await message.channel.send("Minimum bet is 1 coin.")
            pending_stakes[user_id] = (msg_id, choice)
            return

        # Deduct and place bet
        await set_user_balance(str(user_id), bal - amount)
        try:
            await place_bet(msg_id, str(user_id), choice, amount)
        except ValueError as e:
            await message.channel.send(str(e))
            return

        await message.channel.send(f"✅ Your bet of {amount} coins on option {choice} has been placed!")

# Reaction handling: placing default stake on reaction
@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    # ignore removals — bets are permanent once confirmed
    return

pending_stakes = {}

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    user = guild.get_member(payload.user_id)

    if not channel or channel.name != BETS_CHANNEL_NAME:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return

    if not BET_PATTERN.match(message.content):
        return

    emoji = str(payload.emoji)
    if emoji not in ("1️⃣", "2️⃣"):
        return

    choice = 1 if emoji == "1️⃣" else 2

    # --- Step 1: Check confirmed bets in DB ---
    bets = await get_bets_for_message(str(message.id))
    confirmed_bet = next((b for b in bets if b['user_id'] == str(user.id) and b['resolved'] == 0), None)

    if confirmed_bet:
        if confirmed_bet['choice'] != choice:
            try:
                await message.remove_reaction(emoji, user)
            except Exception:
                pass
            try:
                dm = await user.create_dm()
                await dm.send(f"❌ You already placed a bet on choice {confirmed_bet['choice']} for this match. You can’t switch sides.")
            except Exception:
                pass
        return

    # --- Step 2: Check pending stake ---
    if user.id in pending_stakes:
        msg_id, prev_choice = pending_stakes[user.id]
        if msg_id == str(message.id):
            if prev_choice == choice:
                # User is trying to react again with the same choice
                try:
                    await message.remove_reaction(emoji, user)
                except Exception:
                    pass
                try:
                    dm = await user.create_dm()
                    await dm.send(f"ℹ️ You already placed a bet on choice {choice} for this match.")
                except Exception:
                    pass
                return
            else:
                # User is trying to change their reaction to a different choice
                try:
                    await message.remove_reaction(emoji, user)
                except Exception:
                    pass
                try:
                    dm = await user.create_dm()
                    await dm.send(f"❌ You already picked choice {prev_choice} for this bet. Remove that reaction if you want to cancel.")
                except Exception:
                    pass
                return

    # --- Step 3: New pending stake ---
    pending_stakes[user.id] = (str(message.id), choice)
    try:
        dm = await user.create_dm()
        await dm.send(
            f"You reacted with **{emoji}** for bet:\n{message.content}\n\n"
            f"Please reply with the number of coins you want to bet.\n"
            f"(Tip: remove your reaction if you want to cancel before confirming.)"
        )
    except Exception:
        pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    if not channel or channel.name != BETS_CHANNEL_NAME:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return

    if not BET_PATTERN.match(message.content):
        return

    user = guild.get_member(payload.user_id)

    # If user had a pending stake for this message, cancel it
    if user.id in pending_stakes:
        msg_id, _ = pending_stakes[user.id]
        if msg_id == str(message.id):
            pending_stakes.pop(user.id, None)
            try:
                dm = await user.create_dm()
                await dm.send("ℹ️ Your pending bet has been cancelled since you removed your reaction.")
            except Exception:
                pass
# --- Slash commands ---
@tree.command(name='leaderboard', description='Show the top balances of all users who have ever bet')
async def cmd_leaderboard(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 20')
        rows = await cur.fetchall()
        await cur.close()

    if not rows:
        return await interaction.response.send_message('No users have placed bets yet.', ephemeral=True)

    lines = []
    rank = 1
    for uid, bal in rows:
        try:
            user = await bot.fetch_user(int(uid))
            if not user or user.bot:  # skip bots (including this bot itself)
                continue
            name = user.display_name
        except Exception:
            continue  # skip if we can’t fetch

        lines.append(f"**{rank}. {name}** — {bal:,} coins")  # format with commas
        rank += 1

    if not lines:
        return await interaction.response.send_message('No valid human users with bets yet.', ephemeral=True)

    leaderboard_text = "**🏆 Leaderboard — Top Balances**\n\n" + "\n".join(lines)
    await interaction.response.send_message(leaderboard_text)

# --- /balance command ---
@tree.command(name='balance', description='Check your or someone else\'s balance')
@app_commands.describe(user='User to check')
async def cmd_balance(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    bal = await get_user_balance(str(target.id))
    await interaction.response.send_message(f"{target.display_name}'s balance: {bal:,} coins")  # Comma formatted balance

@tree.command(name='addcoins', description='Owner/admin: add coins to a user')
@app_commands.describe(user='User to credit', amount='Amount to add (can be negative)')
async def cmd_addcoins(interaction: discord.Interaction, user: discord.User, amount: int):
    # permission check
    invoker = interaction.user
    perms = interaction.user.guild_permissions if interaction.user else None
    is_admin = perms.manage_guild or perms.administrator if perms else False
    if OWNER_ID is not None:
        allowed = (invoker.id == OWNER_ID) or is_admin
    else:
        allowed = is_admin
    if not allowed:
        return await interaction.response.send_message('Only the bot owner or server administrators can use /addcoins.', ephemeral=True)

    cur = await get_user_balance(str(user.id))
    newbal = cur + amount
    await set_user_balance(str(user.id), newbal)
    await interaction.response.send_message(f'Adjusted {user.display_name}\'s balance by {amount} coins. New balance: {newbal}.', ephemeral=True)

@tree.command(name='resolve', description='Resolve a bet in #bets (owner, message author or moderator)')
@app_commands.describe(message_id='Message ID of the bet to resolve', winning_choice='Winning choice (1 or 2)')
async def cmd_resolve(interaction: discord.Interaction, message_id: str, winning_choice: int):
    await interaction.response.defer()
    if winning_choice not in (1, 2):
        return await interaction.followup.send('Winning choice must be 1 or 2.', ephemeral=True)

    channel = find_bets_channel(interaction.guild)
    if not channel:
        return await interaction.followup.send(f'Could not find a #{BETS_CHANNEL_NAME} channel.', ephemeral=True)

    try:
        message = await channel.fetch_message(int(message_id))
    except Exception:
        return await interaction.followup.send('Could not find that message in the #bets channel. Make sure you pasted the message ID.', ephemeral=True)

    # --- Permission check ---
    invoker = interaction.user
    perms = interaction.user.guild_permissions
    is_admin = perms.manage_messages or perms.administrator
    if invoker.id != message.author.id and invoker.id != OWNER_ID and not is_admin:
        return await interaction.followup.send(
            'Only the bet message author, a moderator (Manage Messages) or the bot owner can resolve this bet.',
            ephemeral=True
        )

    # --- Collect bets ---
    bets = await get_bets_for_message(str(message.id))
    if not bets:
        return await interaction.followup.send('No bets were placed on this message.', ephemeral=True)
    if any(b['resolved'] for b in bets):
        return await interaction.followup.send('This bet has already been resolved.', ephemeral=True)

    total_winning = sum(b['amount'] for b in bets if b['choice'] == winning_choice)
    total_losing = sum(b['amount'] for b in bets if b['choice'] != winning_choice)

    # Extract team names from message text
    m = BET_PATTERN.match(message.content)
    if m:
        team1, team2 = m.group(1), m.group(2)
    else:
        team1, team2 = "Choice 1", "Choice 2"
    winning_team = team1 if winning_choice == 1 else team2

    results = []

    # --- Payout calculation ---
    for b in bets:
        if b['choice'] == winning_choice:
            if total_winning == 0 or total_losing == 0:
                odds = 1.0
            else:
                ratio = total_losing / total_winning
                odds = max(0.25, min(1.0, ratio))

            payout = b['amount'] + int(b['amount'] * odds)

            cur_bal = await get_user_balance(b['user_id'])
            new_bal = cur_bal + payout
            await set_user_balance(b['user_id'], new_bal)

            results.append({'user_id': b['user_id'], 'outcome': 'won', 'stake': b['amount'], 'payout': payout})
        else:
            results.append({'user_id': b['user_id'], 'outcome': 'lost', 'stake': b['amount'], 'payout': 0})

    # mark resolved
    await mark_bets_resolved(str(message.id))

    # Remove bets for this message so betboard disappears
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bets WHERE message_id = ?", (str(message.id),))
        await db.commit()

    # --- Build summary ---
    lines = []
    lines.append(f'✅ Bet resolved — winning choice: **{winning_choice} ({winning_team})**')
    lines.append(f'Total staked on winner: {total_winning} coins')
    lines.append(f'Total staked on loser: {total_losing} coins\n')

    # Add betboard here
    lines.append("📊 **Bet Leaderboard Before Resolution**")
    for b in bets:
        try:
            user = await bot.fetch_user(int(b["user_id"]))
            uname = user.display_name if user else b["user_id"]
        except Exception:
            uname = b["user_id"]
        team = team1 if b["choice"] == 1 else team2
        lines.append(f"• {uname} bet {b['amount']} on **{team}**")

    lines.append("\n💰 **Results**")
    for r in results:
        user = await bot.fetch_user(int(r['user_id']))
        name = user.display_name if user else r['user_id']
        if r['outcome'] == 'won':
            lines.append(f'🏆 **{name}**: staked {r["stake"]}, payout {r["payout"]}')
        else:
            lines.append(f'❌ {name}: lost {r["stake"]}')

    summary = "\n".join(lines)
    await interaction.followup.send(summary)

@tree.command(name="livebets", description="View current bets on a specific bet message")
@app_commands.describe(message_id="Message ID of the bet")
async def cmd_livebets(interaction: discord.Interaction, message_id: str):
    bets = await get_bets_for_message(message_id)
    if not bets:
        return await interaction.response.send_message("No bets placed yet on this message.", ephemeral=True)

    # Try to fetch bet message text to display matchup
    team1, team2 = "Choice 1", "Choice 2"
    matchup_text = None
    for guild in bot.guilds:
        channel = find_bets_channel(guild)
        if channel:
            try:
                msg = await channel.fetch_message(int(message_id))
                matchup_text = msg.content
                m = BET_PATTERN.match(msg.content)
                if m:
                    team1, team2 = m.group(1), m.group(2)
                break
            except Exception:
                continue

    # Build leaderboard
    lines = []
    total_team1 = 0
    total_team2 = 0
    for b in bets:
        try:
            user = await bot.fetch_user(int(b["user_id"]))
            uname = user.display_name if user else b["user_id"]
        except Exception:
            uname = b["user_id"]

        if b["choice"] == 1:
            total_team1 += b["amount"]
            team = team1
        else:
            total_team2 += b["amount"]
            team = team2

        lines.append(f"• **{uname}** bet {b['amount']} on **{team}**")

    text = f"📊 **Current Bet Leaderboard for message {message_id}**\n"
    if matchup_text:
        text += f"Match: {matchup_text}\n\n"
    text += "\n".join(lines)
    text += f"\n\n💰 Totals: **{team1}** = {total_team1} coins | **{team2}** = {total_team2} coins"

    await interaction.response.send_message(text)

# --- /highestbet command ---
@tree.command(name="highestbet", description="Show the highest single bet ever placed")
async def cmd_highestbet(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT user_id, message_id, choice, amount FROM highest_bet WHERE id = 1')
        row = await cur.fetchone()
        await cur.close()

    if not row:
        return await interaction.response.send_message("No bets have been placed yet.", ephemeral=True)

    uid, msg_id, choice, amount = row
    try:
        user = await bot.fetch_user(int(uid))
        uname = user.display_name if user else uid
    except Exception:
        uname = uid

    # Try to fetch the bet message so we can display the matchup text
    matchup_text = None
    for guild in bot.guilds:
        channel = find_bets_channel(guild)
        if channel:
            try:
                msg = await channel.fetch_message(int(msg_id))
                matchup_text = msg.content
                break
            except Exception:
                continue

    formatted_amount = f"{amount:,}"  # <-- comma formatted

    if matchup_text:
        text = (
            f"🏆 **Highest Bet Ever**\n"
            f"User: **{uname}**\n"
            f"Amount: **{formatted_amount} coins**\n"
            f"Match: {matchup_text}\n"
            f"On choice: **{choice}**"
        )
    else:
        text = (
            f"🏆 **Highest Bet Ever**\n"
            f"User: **{uname}**\n"
            f"Amount: **{formatted_amount} coins**\n"
            f"Choice: {choice}\n"
            f"Bet Message ID: `{msg_id}`"
        )

    await interaction.response.send_message(text)

@tree.command(name="help", description="Show help for all betting commands")
async def cmd_help(interaction: discord.Interaction):
    help_text = (
        "**📖 Bets Bot Commands**\n\n"

        "• `/help` — Show this help message.\n\n"

        "• `/balance [user]` — Check your (or someone else’s) coin balance.\n\n"

        "• `/leaderboard` — See the top balances across all users.\n\n"

        "• `/livebets <message_id>` — View current bets on a specific bet in real time (who bet, how much, and totals).\n\n"

        "• `/resolve <message_id> <winning_choice>` — Resolve a bet and distribute winnings. "
        "Only the bet creator, admins, or the bot owner can do this.\n\n"

        "• `/addcoins <user> <amount>` — (Admins/owner only) Add or remove coins from a user’s balance.\n\n"

        "• `/highestbet` — Show the single biggest bet ever placed (who, how much, and on which match).\n\n"

        "💡 **How betting works:**\n"
        "1. Someone posts a message in #bets with the format: `1. Team A vs 2. Team B`.\n"
        "2. The bot adds 1️⃣ and 2️⃣ reactions automatically.\n"
        "3. React with your choice — the bot will DM you to enter your stake.\n"
        "4. Once you confirm your stake, your bet is locked.\n\n"

        "💵 **Odds and payouts:**\n"
        "• Base payout = your stake back + winnings from odds.\n"
        "• If total bets are even (50/50), payout doubles your stake.\n"
        "• If one side is heavily favored, odds go down (minimum x1.25).\n"
        "• Underdogs pay more, favorites pay less.\n\n"

        "⚖️ **Rules:**\n"
        "• You cannot switch sides once your bet is confirmed.\n"
        "• You cannot bet more than your balance.\n"
        "• Balances never drop below 1 coin.\n"
        "• New users start with the default balance.\n\n"

        "• Note: DMs need to be turned on for this server in order to make bets (usually on by default but if you dont receive a DM from bot after reacting to the message, check your discord settings and allow DMs from other server members for this server."
    )
    await interaction.response.send_message(help_text, ephemeral=True)

# Run the bot
if __name__ == '__main__':
    if not TOKEN:
        print("❌ Error: token was not found! Make sure that in .env there is a DISCORD_TOKEN=...")
    else:
        bot.run(TOKEN)
