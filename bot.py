import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import os
import json
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────────

TOKEN                   = os.getenv("DISCORD_TOKEN")
NOTIFICATION_CHANNEL_ID = int(os.getenv("NOTIFICATION_CHANNEL_ID", 0))
PING_MODE               = os.getenv("PING_MODE", "everyone")  # everyone | here | role | none
ROLE_ID                 = os.getenv("ROLE_ID", "")
CHECK_INTERVAL          = int(os.getenv("CHECK_INTERVAL", 30))

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")

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
previous_states: dict   = {}  # { user_id (int): presence_type (int) }
previous_game_ids: dict = {}  # { user_id (int): game_id (str|None) }
session_starts: dict    = {}  # { user_id (int): datetime } — when they came online
offline_ticks: dict     = {}  # { user_id (int): int } — consecutive offline poll count
OFFLINE_CONFIRM = 2           # how many consecutive offline polls before notifying

# Track when each user was last seen online (persisted so restarts don't lose it)
LAST_SEEN_FILE = os.path.join(BASE_DIR, "last_seen.json")

def load_last_seen() -> dict:
    if os.path.exists(LAST_SEEN_FILE):
        with open(LAST_SEEN_FILE) as f:
            return json.load(f)
    return {}

def save_last_seen(data: dict):
    with open(LAST_SEEN_FILE, "w") as f:
        json.dump(data, f, indent=2)

last_seen: dict = load_last_seen()  # { str(user_id): ISO timestamp }

# ─── Group shout persistence ──────────────────────────────────────────────────

GROUPS_FILE      = os.path.join(BASE_DIR, "groups.json")
LAST_SHOUT_FILE  = os.path.join(BASE_DIR, "last_shouts.json")

def load_groups() -> dict:
    if os.path.exists(GROUPS_FILE):
        with open(GROUPS_FILE) as f:
            return json.load(f)
    return {}

def save_groups(data: dict):
    with open(GROUPS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_last_shouts() -> dict:
    if os.path.exists(LAST_SHOUT_FILE):
        with open(LAST_SHOUT_FILE) as f:
            return json.load(f)
    return {}

def save_last_shouts(data: dict):
    with open(LAST_SHOUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

# groups format: { "group_id": "group_name" }
groups: dict     = load_groups()
# last_shouts: { "group_id": "shout_body" } — tracks last known shout to detect changes
last_shouts: dict = load_last_shouts()

# ─── Bot setup ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── Roblox API helpers ───────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.roblox.com",
    "Referer": "https://www.roblox.com/",
}

def _rblx_get(url: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
        print(f"[rblx_get] HTTP {r.status_code} {url}")
    except Exception as e:
        print(f"[rblx_get] Error {url}: {e}")
    return None

def _rblx_post(url: str, payload: dict) -> dict | None:
    try:
        r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
        print(f"[rblx_post] HTTP {r.status_code} {url}")
    except Exception as e:
        print(f"[rblx_post] Error {url}: {e}")
    return None

async def roblox_get_presence(user_ids: list[int]) -> list[dict]:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _rblx_post, "https://presence.roblox.com/v1/presence/users", {"userIds": user_ids})
    return data.get("userPresences", []) if data else []

async def roblox_get_user(user_id: int) -> dict | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _rblx_get, f"https://users.roblox.com/v1/users/{user_id}")

async def roblox_search_user(username: str) -> dict | None:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _rblx_post, "https://users.roblox.com/v1/usernames/users", {"usernames": [username], "excludeBannedUsers": False})
    if data and data.get("data"):
        return data["data"][0]
    return None

async def roblox_get_game_name(universe_id: int | None = None, place_id: int | None = None) -> str:
    """Resolve a universe ID or place ID to a human-readable game name."""
    loop = asyncio.get_event_loop()
    if not universe_id and place_id:
        uni_data = await loop.run_in_executor(None, _rblx_get, f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
        universe_id = uni_data.get("universeId") if uni_data else None
    if not universe_id:
        return "Unknown Game"
    game_data = await loop.run_in_executor(None, _rblx_get, f"https://games.roblox.com/v1/games?universeIds={universe_id}")
    if game_data and game_data.get("data"):
        return game_data["data"][0].get("name", "Unknown Game")
    return "Unknown Game"

async def roblox_get_avatar_url(user_id: int) -> str | None:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _rblx_get, f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=420x420&format=Png")
    if data and data.get("data"):
        return data["data"][0].get("imageUrl")
    return None

async def roblox_get_friend_count(user_id: int) -> int | None:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _rblx_get, f"https://friends.roblox.com/v1/users/{user_id}/friends/count")
    return data.get("count") if data else None

async def roblox_get_follower_count(user_id: int) -> int | None:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _rblx_get, f"https://friends.roblox.com/v1/users/{user_id}/followers/count")
    return data.get("count") if data else None

async def roblox_get_following_count(user_id: int) -> int | None:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _rblx_get, f"https://friends.roblox.com/v1/users/{user_id}/followings/count")
    return data.get("count") if data else None

async def roblox_get_game_players(universe_id: int) -> int | None:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _rblx_get, f"https://games.roblox.com/v1/games?universeIds={universe_id}")
    if data and data.get("data"):
        return data["data"][0].get("playing")
    return None


# ─── RoPro API helper ─────────────────────────────────────────────────────────

async def ropro_get_user_info(user_id: int) -> dict | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _rblx_get, f"https://api.ropro.io/getUserInfoTest.php?userid={user_id}")

# ─── Auto-delete helper ───────────────────────────────────────────────────────

DELETE_AFTER = 300  # seconds (5 minutes)
_delete_tasks: set = set()  # keeps task references alive until they complete

async def delete_after(msg: discord.Message, delay: int = DELETE_AFTER):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

def schedule_delete(msg: discord.Message):
    task: asyncio.Task = asyncio.create_task(delete_after(msg))
    _delete_tasks.add(task)
    task.add_done_callback(_delete_tasks.discard)

# ─── Build notification embed ─────────────────────────────────────────────────

def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem  = divmod(seconds, 3600)
    m, s    = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def build_mention() -> str:
    if PING_MODE == "everyone":
        return "@everyone"
    if PING_MODE == "here":
        return "@here"
    if PING_MODE == "role" and ROLE_ID:
        return f"<@&{ROLE_ID}>"
    return ""


async def build_online_embed(presence: dict) -> discord.Embed:
    user_id     = presence["userId"]
    ptype       = presence.get("userPresenceType", 0)
    root_place  = presence.get("rootPlaceId")
    game_id     = presence.get("gameId")
    universe_id = presence.get("universeId")
    place_id    = presence.get("placeId")
    last_online = last_seen.get(str(user_id))

    print(f"[presence] user={user_id} type={ptype} universe={universe_id} place={place_id} root={root_place} game={game_id}")

    label, color = PRESENCE_TYPES.get(ptype, ("Online", discord.Color.green()))

    in_game = ptype == 2 and (universe_id or root_place or place_id)
    user_info, thumbnail_url, ropro_info, game_name = await asyncio.gather(
        roblox_get_user(user_id),
        roblox_get_avatar_url(user_id),
        ropro_get_user_info(user_id),
        roblox_get_game_name(universe_id=universe_id, place_id=root_place or place_id) if in_game else asyncio.sleep(0),
    )
    if not in_game:
        game_name = None

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
        embed.add_field(name="Playing", value=game_name or "Unknown Game", inline=False)

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

async def build_game_join_embed(presence: dict) -> discord.Embed:
    user_id     = presence["userId"]
    root_place  = presence.get("rootPlaceId") or presence.get("placeId")
    game_id     = presence.get("gameId")
    universe_id = presence.get("universeId")

    user_info, thumbnail_url, game_name = await asyncio.gather(
        roblox_get_user(user_id),
        roblox_get_avatar_url(user_id),
        roblox_get_game_name(universe_id=universe_id, place_id=root_place) if (universe_id or root_place) else asyncio.sleep(0),
    )
    if not universe_id and not root_place:
        game_name = "Unknown Game"

    username     = user_info.get("name",        f"User {user_id}") if user_info else f"User {user_id}"
    display_name = user_info.get("displayName", username)          if user_info else username

    embed = discord.Embed(
        title=f"{display_name} joined a game!",
        url=f"https://www.roblox.com/users/{user_id}/profile",
        color=discord.Color.green(),
    )
    embed.add_field(name="Username", value=f"@{username}", inline=True)
    embed.add_field(name="Playing",  value=game_name,      inline=True)

    if root_place and game_id:
        app_link = f"roblox://experiences/start?placeId={root_place}&gameInstanceId={game_id}"
        web_link = f"https://www.roblox.com/games/{root_place}"
        embed.add_field(
            name="Join Game \u2705",
            value=f"[Open in Roblox App]({app_link})\n[View on Web]({web_link})",
            inline=False,
        )
    elif root_place:
        web_link = f"https://www.roblox.com/games/{root_place}"
        embed.add_field(name="Game (Joins Off \U0001f512)", value=f"[View on Web]({web_link})", inline=False)

    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    embed.set_footer(text=f"Roblox ID: {user_id}  •  checks every {CHECK_INTERVAL}s")
    return embed

async def build_offline_embed(user_id: int, session_duration: str | None = None) -> discord.Embed:
    went_offline_ts = int(datetime.now(timezone.utc).timestamp())

    user_info, thumbnail_url, ropro_info = await asyncio.gather(
        roblox_get_user(user_id),
        roblox_get_avatar_url(user_id),
        ropro_get_user_info(user_id),
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
    if session_duration:
        embed.add_field(name="Session Duration", value=session_duration, inline=True)

    prev_last_online = last_seen.get(str(user_id))
    if prev_last_online:
        try:
            dt   = datetime.fromisoformat(prev_last_online.replace("Z", "+00:00"))
            unix = int(dt.timestamp())
            embed.add_field(name="Last Online", value=f"<t:{unix}:R>", inline=True)
        except Exception:
            pass

    if ropro_info:
        ropro_lines = []
        tier = ropro_info.get("tier") or ropro_info.get("subscription")
        if tier:
            ropro_lines.append(f"**Tier:** {tier}")
        rap = ropro_info.get("rap")
        if rap is not None:
            ropro_lines.append(f"**RAP:** {rap:,}")
        value = ropro_info.get("value")
        if value is not None:
            ropro_lines.append(f"**Value:** {value:,}")
        ban = ropro_info.get("banStatus") or ropro_info.get("ban_status")
        if ban and ban.lower() not in ("none", "ok", ""):
            ropro_lines.append(f"**Ban Status:** {ban}")
        linked = ropro_info.get("linked")
        if linked is not None:
            ropro_lines.append(f"**RoPro Linked:** {'Yes' if linked else 'No'}")
        if ropro_lines:
            embed.add_field(name="RoPro Info", value="\n".join(ropro_lines), inline=False)

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

    presences = await roblox_get_presence(user_ids)

    for presence in presences:
        user_id  = presence.get("userId")
        if user_id is None:
            continue
        ptype    = presence.get("userPresenceType", 0)
        game_id  = presence.get("gameId")
        prev     = previous_states.get(user_id, 0)
        prev_gid = previous_game_ids.get(user_id)

        # ── offline → online ──────────────────────────────────────────────────
        if prev == 0 and ptype != 0:
            offline_ticks[user_id]  = 0
            session_starts[user_id] = datetime.now(timezone.utc)
            try:
                embed   = await build_online_embed(presence)
                mention = build_mention()
                msg = await channel.send(mention, embed=embed)
                schedule_delete(msg)
            except Exception as e:
                print(f"[loop] Failed to send online notification for {user_id}: {e}")

        # ── joined / switched game while already online ────────────────────────
        elif ptype == 2 and (prev != 2 or prev_gid != game_id):
            offline_ticks[user_id] = 0
            try:
                embed   = await build_game_join_embed(presence)
                mention = build_mention()
                msg = await channel.send(mention, embed=embed)
                schedule_delete(msg)
            except Exception as e:
                print(f"[loop] Failed to send game-join notification for {user_id}: {e}")

        # ── online → offline (with anti-flicker debounce) ─────────────────────
        if prev != 0 and ptype == 0:
            ticks = offline_ticks.get(user_id, 0) + 1
            offline_ticks[user_id] = ticks
            if ticks >= OFFLINE_CONFIRM:
                # Calculate session duration
                start    = session_starts.pop(user_id, None)
                duration = format_duration((datetime.now(timezone.utc) - start).total_seconds()) if start else None
                try:
                    embed = await build_offline_embed(user_id, session_duration=duration)
                    msg   = await channel.send(embed=embed)
                    schedule_delete(msg)
                except Exception as e:
                    print(f"[loop] Failed to send offline notification for {user_id}: {e}")
                last_seen[str(user_id)] = datetime.now(timezone.utc).isoformat()
                save_last_seen(last_seen)
                offline_ticks[user_id] = 0
        else:
            offline_ticks[user_id] = 0

        previous_states[user_id]   = ptype
        previous_game_ids[user_id] = game_id


@check_presence.before_loop
async def before_check():
    await bot.wait_until_ready()

# ─── Group shout polling ──────────────────────────────────────────────────────

@tasks.loop(seconds=60)
async def check_group_shouts():
    if not groups:
        return

    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    loop = asyncio.get_event_loop()

    for group_id, group_name in list(groups.items()):
        data = await loop.run_in_executor(None, _rblx_get, f"https://groups.roblox.com/v1/groups/{group_id}")
        if not data:
            continue

        shout = data.get("shout")
        if not shout:
            continue

        body = shout.get("body", "").strip()
        if not body:
            continue

        prev_body = last_shouts.get(group_id)

        if body != prev_body:
            last_shouts[group_id] = body
            save_last_shouts(last_shouts)

            # Don't notify on first run (just seed the stored shout)
            if prev_body is None:
                continue

            poster      = shout.get("poster", {})
            poster_name = poster.get("displayName") or poster.get("username") or "Unknown"
            updated     = shout.get("updated", "")
            try:
                dt   = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                unix = int(dt.timestamp())
                time_str = f"<t:{unix}:R>"
            except Exception:
                time_str = updated

            embed = discord.Embed(
                title=f"New shout in {group_name}!",
                url=f"https://www.roblox.com/groups/{group_id}",
                description=body,
                color=discord.Color.orange(),
            )
            embed.add_field(name="Posted by", value=poster_name, inline=True)
            embed.add_field(name="When",      value=time_str,    inline=True)
            embed.set_footer(text=f"Group ID: {group_id}")

            mention = build_mention()
            msg = await channel.send(mention, embed=embed)
            schedule_delete(msg)

@check_group_shouts.before_loop
async def before_shout_check():
    await bot.wait_until_ready()

STATUS_ICONS = {"none": "✅", "minor": "⚠️", "major": "🔴", "critical": "🚨"}

# ─── Slash / prefix commands ──────────────────────────────────────────────────

@bot.hybrid_command(name="watch", description="Add a Roblox user to the watchlist")
@commands.has_permissions(manage_guild=True)
async def cmd_watch(ctx: commands.Context, username: str):
    await ctx.defer()
    try:
        user = await roblox_search_user(username)
        if not user:
            await ctx.send(f"Could not find Roblox user **{username}**.")
            return
        uid  = str(user["id"])
        name = user["name"]
        watchlist[uid] = name
        save_watchlist(watchlist)
        await ctx.send(f"Now watching **{name}** (ID: `{uid}`)")
    except Exception as e:
        await ctx.send(f"Something went wrong: `{e}`")


@bot.hybrid_command(name="unwatch", description="Remove a Roblox user from the watchlist")
@commands.has_permissions(manage_guild=True)
async def cmd_unwatch(ctx: commands.Context, username: str):
    try:
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
    except Exception as e:
        await ctx.send(f"Something went wrong: `{e}`")


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

    presences = await roblox_get_presence([uid])
    if not presences:
        await ctx.send("Failed to fetch presence data. Try again in a moment.")
        return

    embed = await build_online_embed(presences[0]) if presences[0].get("userPresenceType", 0) != 0 else None

    if embed:
        await ctx.send(embed=embed)
    else:
        label, color = PRESENCE_TYPES[0]
        e = discord.Embed(title=f"{username} is {label}", color=color)
        await ctx.send(embed=e)

@bot.hybrid_command(name="profile", description="Show a Roblox user's full profile")
async def cmd_profile(ctx: commands.Context, username: str):
    await ctx.defer()
    try:
        user = await roblox_search_user(username)
        if not user:
            await ctx.send(f"Could not find **{username}**.")
            return
        uid = user["id"]

        user_info, thumbnail_url, friends, followers, following, ropro_info = await asyncio.gather(
            roblox_get_user(uid),
            roblox_get_avatar_url(uid),
            roblox_get_friend_count(uid),
            roblox_get_follower_count(uid),
            roblox_get_following_count(uid),
            ropro_get_user_info(uid),
        )
        if not user_info:
            await ctx.send("Could not fetch profile data.")
            return

        display  = user_info.get("displayName", user_info["name"])
        name     = user_info["name"]
        desc     = user_info.get("description", "").strip()
        created  = user_info.get("created", "")
        verified = user_info.get("hasVerifiedBadge", False)

        embed = discord.Embed(
            title=f"{display} (@{name})" + (" ✅" if verified else ""),
            url=f"https://www.roblox.com/users/{uid}/profile",
            description=desc or "*No description*",
            color=discord.Color.blurple(),
        )
        if created:
            try:
                dt   = datetime.fromisoformat(created.replace("Z", "+00:00"))
                unix = int(dt.timestamp())
                embed.add_field(name="Joined Roblox", value=f"<t:{unix}:D>", inline=True)
            except Exception:
                pass
        if friends   is not None: embed.add_field(name="Friends",   value=f"{friends:,}",   inline=True)
        if followers  is not None: embed.add_field(name="Followers", value=f"{followers:,}",  inline=True)
        if following  is not None: embed.add_field(name="Following", value=f"{following:,}",  inline=True)

        if ropro_info:
            tier = ropro_info.get("tier") or ropro_info.get("subscription")
            rap  = ropro_info.get("rap")
            if tier: embed.add_field(name="RoPro Tier", value=tier, inline=True)
            if rap is not None: embed.add_field(name="RAP", value=f"{rap:,}", inline=True)

        embed.set_footer(text=f"User ID: {uid}")
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Something went wrong: `{e}`")


@bot.hybrid_command(name="rblxstatus", description="Check current Roblox platform status")
async def cmd_rblxstatus(ctx: commands.Context):
    embed = discord.Embed(
        title="Roblox Platform Status",
        description="Check live status at [status.roblox.com](https://status.roblox.com)",
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


@bot.hybrid_command(name="watchgroup", description="Add a Roblox group to watch for shouts")
@commands.has_permissions(manage_guild=True)
async def cmd_watchgroup(ctx: commands.Context, group_id: str):
    await ctx.defer()
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _rblx_get, f"https://groups.roblox.com/v1/groups/{group_id}")
        if not data or "id" not in data:
            await ctx.send(f"Could not find group `{group_id}`. Check the ID and try again.")
            return
        name = data.get("name", f"Group {group_id}")
        groups[group_id] = name
        save_groups(groups)
        # Seed the current shout so we don't fire on first check
        shout = data.get("shout")
        if shout and shout.get("body"):
            last_shouts[group_id] = shout["body"].strip()
            save_last_shouts(last_shouts)
        await ctx.send(f"Now watching shouts for **{name}** (ID: `{group_id}`)")
    except Exception as e:
        await ctx.send(f"Something went wrong: `{e}`")


@bot.hybrid_command(name="unwatchgroup", description="Stop watching a Roblox group for shouts")
@commands.has_permissions(manage_guild=True)
async def cmd_unwatchgroup(ctx: commands.Context, group_id: str):
    try:
        if group_id not in groups:
            await ctx.send(f"Group `{group_id}` is not being watched.")
            return
        name = groups.pop(group_id)
        save_groups(groups)
        last_shouts.pop(group_id, None)
        save_last_shouts(last_shouts)
        await ctx.send(f"Stopped watching **{name}**.")
    except Exception as e:
        await ctx.send(f"Something went wrong: `{e}`")


@bot.hybrid_command(name="groups", description="List all watched Roblox groups")
async def cmd_groups(ctx: commands.Context):
    if not groups:
        await ctx.send("No groups being watched. Use `/watchgroup <group_id>` to add one.")
        return
    lines = [f"• **{name}** — ID: `{gid}`" for gid, name in groups.items()]
    embed = discord.Embed(
        title="Watched Groups",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    await ctx.send(embed=embed)


# ─── Bot events ───────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}  (ID: {bot.user.id if bot.user else 'unknown'})")
    total = 0
    for guild in bot.guilds:
        try:
            synced = await bot.tree.sync(guild=guild)
            total += len(synced)
            print(f"Synced {len(synced)} command(s) to {guild.name}")
        except Exception as e:
            print(f"Failed to sync to {guild.name}: {e}")
    print(f"Total synced: {total} command(s)")
    check_presence.start()
    check_group_shouts.start()


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
