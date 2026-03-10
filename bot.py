import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import os
import json
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────────

TOKEN                  = os.getenv("DISCORD_TOKEN")
NOTIFICATION_CHANNEL_ID = int(os.getenv("NOTIFICATION_CHANNEL_ID", 0))
PING_MODE              = os.getenv("PING_MODE", "everyone")  # everyone | here | role | none
ROLE_ID                = os.getenv("ROLE_ID", "")            # only used if PING_MODE=role
CHECK_INTERVAL         = int(os.getenv("CHECK_INTERVAL", 30))

WATCHLIST_FILE = "watchlist.json"

# ─── Presence type labels & embed colors ─────────────────────────────────────

PRESENCE_TYPES = {
    0: ("Offline",          discord.Color.red()),
    1: ("Browsing Roblox",  discord.Color.yellow()),
    2: ("In Game",          discord.Color.green()),
    3: ("In Roblox Studio", discord.Color.blue()),
}

# ─── Watchlist persistence ───────────────────────────────────────────────────

def load_watchlist() -> dict:
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    return {}

def save_watchlist(data: dict):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

# watchlist format: { "roblox_user_id": "username", ... }
watchlist: dict = load_watchlist()

# Track last-known presence per user so we only notify on change
previous_states: dict = {}  # { user_id (int): presence_type (int) }

# Track when each user was last seen online (persisted so restarts don't lose it)
LAST_SEEN_FILE = "last_seen.json"

def load_last_seen() -> dict:
    if os.path.exists(LAST_SEEN_FILE):
        with open(LAST_SEEN_FILE) as f:
            return json.load(f)
    return {}

def save_last_seen(data: dict):
    with open(LAST_SEEN_FILE, "w") as f:
        json.dump(data, f, indent=2)

last_seen: dict = load_last_seen()  # { str(user_id): ISO timestamp }

# ─── Bot setup ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── Roblox API helpers ───────────────────────────────────────────────────────

async def roblox_get_presence(session: aiohttp.ClientSession, user_ids: list[int]) -> list[dict]:
    """Fetch presence data for a list of Roblox user IDs."""
    url = "https://presence.roblox.com/v1/presence/users"
    try:
        async with session.post(url, json={"userIds": user_ids}) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("userPresences", [])
    except Exception as e:
        print(f"[presence] Error: {e}")
    return []


async def roblox_get_user(session: aiohttp.ClientSession, user_id: int) -> dict | None:
    """Fetch Roblox user profile by ID."""
    url = f"https://users.roblox.com/v1/users/{user_id}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        print(f"[user] Error: {e}")
    return None


async def roblox_search_user(session: aiohttp.ClientSession, username: str) -> dict | None:
    """Look up a Roblox user by username. Returns the first match or None."""
    url = "https://users.roblox.com/v1/usernames/users"
    try:
        async with session.post(url, json={"usernames": [username], "excludeBannedUsers": False}) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("data"):
                    return data["data"][0]
    except Exception as e:
        print(f"[search] Error: {e}")
    return None


async def roblox_get_avatar_url(session: aiohttp.ClientSession, user_id: int) -> str | None:
    """Fetch a headshot thumbnail URL for a user."""
    url = (
        f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
        f"?userIds={user_id}&size=420x420&format=Png"
    )
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("data"):
                    return data["data"][0].get("imageUrl")
    except Exception as e:
        print(f"[avatar] Error: {e}")
    return None

# ─── RoPro API helper ─────────────────────────────────────────────────────────

async def ropro_get_user_info(session: aiohttp.ClientSession, user_id: int) -> dict | None:
    """
    Fetch RoPro extended user info.
    Endpoint: https://api.ropro.io/getUserInfoTest.php?userid=<id>

    Known response fields (may vary — check ropro.io if the structure changes):
      - linked          : bool   — whether the user has RoPro linked
      - tier            : str    — subscription tier (e.g. "Free", "Pro")
      - banStatus       : str    — any RoPro ban status
      - rap             : int    — recent average price of inventory
      - value           : int    — inventory value
      - joins_disabled  : bool   — whether user has game joins disabled (RoPro-tracked)
    """
    url = f"https://api.ropro.io/getUserInfoTest.php?userid={user_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception as e:
        print(f"[ropro] Error fetching info for {user_id}: {e}")
    return None

# ─── Auto-delete helper ───────────────────────────────────────────────────────

DELETE_AFTER = 300  # seconds (5 minutes)

async def delete_after(msg: discord.Message, delay: int = DELETE_AFTER):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

# ─── Build notification embed ─────────────────────────────────────────────────

def build_mention() -> str:
    if PING_MODE == "everyone":
        return "@everyone"
    if PING_MODE == "here":
        return "@here"
    if PING_MODE == "role" and ROLE_ID:
        return f"<@&{ROLE_ID}>"
    return ""


async def build_online_embed(
    session: aiohttp.ClientSession,
    presence: dict,
) -> discord.Embed:
    user_id       = presence["userId"]
    ptype         = presence.get("userPresenceType", 0)
    root_place    = presence.get("rootPlaceId")
    game_id       = presence.get("gameId")       # None when joins are off
    last_location = presence.get("lastLocation") or "Unknown Game"
    last_online   = last_seen.get(str(user_id))  # tracked by the bot itself

    label, color = PRESENCE_TYPES.get(ptype, ("Online", discord.Color.green()))

    # Parallel fetch of extra data
    user_info, thumbnail_url, ropro_info = await asyncio.gather(
        roblox_get_user(session, user_id),
        roblox_get_avatar_url(session, user_id),
        ropro_get_user_info(session, user_id),
    )

    username     = user_info.get("name",        f"User {user_id}") if user_info else f"User {user_id}"
    display_name = user_info.get("displayName", username)          if user_info else username

    embed = discord.Embed(
        title=f"{display_name} just came online!",
        url=f"https://www.roblox.com/users/{user_id}/profile",
        color=color,
    )
    embed.add_field(name="Username", value=f"@{username}", inline=True)
    embed.add_field(name="Status",   value=label,          inline=True)

    # ── Last online ───────────────────────────────────────────────────────────
    if last_online:
        # Roblox returns ISO 8601, e.g. "2026-03-10T12:00:00.000Z"
        # Discord timestamp format: <t:UNIX:R> → shows "2 hours ago" etc.
        try:
            dt = datetime.fromisoformat(last_online.replace("Z", "+00:00"))
            unix = int(dt.timestamp())
            embed.add_field(name="Last Online", value=f"<t:{unix}:R>", inline=True)
        except Exception:
            embed.add_field(name="Last Online", value=last_online, inline=True)

    # ── In-game fields ────────────────────────────────────────────────────────
    if ptype == 2:
        embed.add_field(name="Playing", value=last_location, inline=False)

        if root_place and game_id:
            # Joins are ON — deep link works
            app_link = f"roblox://experiences/start?placeId={root_place}&gameInstanceId={game_id}"
            web_link = f"https://www.roblox.com/games/{root_place}"
            embed.add_field(
                name="Join Game \u2705",
                value=f"[Open in Roblox App]({app_link})\n[View on Web]({web_link})",
                inline=False,
            )
        elif root_place:
            # Joins are OFF — only show game page
            web_link = f"https://www.roblox.com/games/{root_place}"
            embed.add_field(
                name="Game (Joins Off \U0001f512)",
                value=f"[View on Web]({web_link})",
                inline=False,
            )

    # ── RoPro fields ──────────────────────────────────────────────────────────
    if ropro_info:
        ropro_lines = []

        # Subscription tier
        tier = ropro_info.get("tier") or ropro_info.get("subscription")
        if tier:
            ropro_lines.append(f"**Tier:** {tier}")

        # RAP / inventory value
        rap = ropro_info.get("rap")
        if rap is not None:
            ropro_lines.append(f"**RAP:** {rap:,}")

        value = ropro_info.get("value")
        if value is not None:
            ropro_lines.append(f"**Value:** {value:,}")

        # Ban status
        ban = ropro_info.get("banStatus") or ropro_info.get("ban_status")
        if ban and ban.lower() not in ("none", "ok", ""):
            ropro_lines.append(f"**Ban Status:** {ban}")

        # Linked status
        linked = ropro_info.get("linked")
        if linked is not None:
            ropro_lines.append(f"**RoPro Linked:** {'Yes' if linked else 'No'}")

        if ropro_lines:
            embed.add_field(name="RoPro Info", value="\n".join(ropro_lines), inline=False)

    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    embed.set_footer(text=f"Roblox ID: {user_id}  •  checks every {CHECK_INTERVAL}s")
    return embed

async def build_offline_embed(
    session: aiohttp.ClientSession,
    user_id: int,
) -> discord.Embed:
    went_offline_ts = int(datetime.now(timezone.utc).timestamp())

    user_info, thumbnail_url = await asyncio.gather(
        roblox_get_user(session, user_id),
        roblox_get_avatar_url(session, user_id),
    )

    username     = user_info.get("name",        f"User {user_id}") if user_info else f"User {user_id}"
    display_name = user_info.get("displayName", username)          if user_info else username

    embed = discord.Embed(
        title=f"{display_name} went offline.",
        url=f"https://www.roblox.com/users/{user_id}/profile",
        color=discord.Color.red(),
    )
    embed.add_field(name="Username",     value=f"@{username}",             inline=True)
    embed.add_field(name="Went Offline", value=f"<t:{went_offline_ts}:R>", inline=True)

    # Last online before this session (from previous offline→online transition)
    prev_last_online = last_seen.get(str(user_id))
    if prev_last_online:
        try:
            dt   = datetime.fromisoformat(prev_last_online.replace("Z", "+00:00"))
            unix = int(dt.timestamp())
            embed.add_field(name="Previously Online", value=f"<t:{unix}:R>", inline=True)
        except Exception:
            pass

    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    embed.set_footer(text=f"Roblox ID: {user_id}  •  checks every {CHECK_INTERVAL}s")
    return embed

# ─── Presence polling loop ────────────────────────────────────────────────────

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_presence():
    if not watchlist:
        return

    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print("[loop] Notification channel not found or is not a text channel — check NOTIFICATION_CHANNEL_ID in .env")
        return

    user_ids = [int(uid) for uid in watchlist]

    async with aiohttp.ClientSession() as session:
        presences = await roblox_get_presence(session, user_ids)

        for presence in presences:
            user_id  = presence.get("userId")
            if user_id is None:
                continue
            ptype    = presence.get("userPresenceType", 0)
            prev     = previous_states.get(user_id, 0)

            # offline → online
            if prev == 0 and ptype != 0:
                try:
                    embed   = await build_online_embed(session, presence)
                    mention = build_mention()
                    msg = await channel.send(mention, embed=embed)
                    asyncio.create_task(delete_after(msg))
                except Exception as e:
                    print(f"[loop] Failed to send online notification for {user_id}: {e}")

            # online → offline
            if prev != 0 and ptype == 0:
                try:
                    embed = await build_offline_embed(session, user_id)
                    msg   = await channel.send(embed=embed)
                    asyncio.create_task(delete_after(msg))
                except Exception as e:
                    print(f"[loop] Failed to send offline notification for {user_id}: {e}")
                # Record last seen AFTER sending the offline embed
                last_seen[str(user_id)] = datetime.now(timezone.utc).isoformat()
                save_last_seen(last_seen)

            previous_states[user_id] = ptype


@check_presence.before_loop
async def before_check():
    await bot.wait_until_ready()

# ─── Slash / prefix commands ──────────────────────────────────────────────────

@bot.hybrid_command(name="watch", description="Add a Roblox user to the watchlist")
@commands.has_permissions(manage_guild=True)
async def cmd_watch(ctx: commands.Context, username: str):
    """!watch <roblox_username>  |  /watch"""
    await ctx.defer()
    async with aiohttp.ClientSession() as session:
        user = await roblox_search_user(session, username)
    if not user:
        await ctx.send(f"Could not find Roblox user **{username}**. Check the spelling and try again.")
        return

    uid  = str(user["id"])
    name = user["name"]
    watchlist[uid] = name
    save_watchlist(watchlist)
    await ctx.send(f"Now watching **{name}** (ID: `{uid}`)")


@bot.hybrid_command(name="unwatch", description="Remove a Roblox user from the watchlist")
@commands.has_permissions(manage_guild=True)
async def cmd_unwatch(ctx: commands.Context, username: str):
    """!unwatch <roblox_username>  |  /unwatch"""
    target = next(
        (uid for uid, name in watchlist.items() if name.lower() == username.lower()),
        None,
    )
    if target is None:
        await ctx.send(f"**{username}** is not in the watchlist.")
        return
    removed = watchlist.pop(target)
    save_watchlist(watchlist)
    await ctx.send(f"Stopped watching **{removed}**.")


@bot.hybrid_command(name="watchlist", description="List all watched Roblox users")
async def cmd_watchlist(ctx: commands.Context):
    """!watchlist  |  /watchlist"""
    if not watchlist:
        await ctx.send("The watchlist is empty. Use `/watch <username>` to add someone.")
        return
    lines = [f"• **{name}** — ID: `{uid}`" for uid, name in watchlist.items()]
    embed = discord.Embed(
        title="Roblox Watchlist",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


@bot.hybrid_command(name="status", description="Check current Roblox status of a watched user")
async def cmd_status(ctx: commands.Context, username: str):
    """!status <roblox_username>  |  /status"""
    await ctx.defer()
    uid = next(
        (int(uid) for uid, name in watchlist.items() if name.lower() == username.lower()),
        None,
    )
    if uid is None:
        await ctx.send(f"**{username}** is not in the watchlist. Use `/watch` first.")
        return

    async with aiohttp.ClientSession() as session:
        presences = await roblox_get_presence(session, [uid])
        if not presences:
            await ctx.send("Failed to fetch presence data. Try again in a moment.")
            return

        embed = await build_online_embed(session, presences[0]) if presences[0].get("userPresenceType", 0) != 0 else None

    if embed:
        await ctx.send(embed=embed)
    else:
        label, color = PRESENCE_TYPES[0]
        e = discord.Embed(title=f"{username} is {label}", color=color)
        await ctx.send(embed=e)

# ─── Bot events ───────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}  (ID: {bot.user.id if bot.user else 'unknown'})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
    check_presence.start()


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** permission to use that command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`")
    else:
        print(f"[error] {error}")

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")
    bot.run(TOKEN)
