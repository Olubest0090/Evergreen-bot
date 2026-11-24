"""
Interview tickets for new recruits.

/ticket setup  (admin only)
    Posts the "EVERGREEN Interview ticket" embed with a Create Ticket
    button in the channel where the command is run.

Create Ticket button
    Creates a private channel named interview-<username> in the same
    category as the ticket room. Only the applicant, the staff roles
    (Everlord, Coordinator, Steward, Seed Bearer) and the bot can see it.
    The bot then posts the welcome / induction embed with the interview
    questions. Each person can only have one open ticket at a time.

Closing a ticket: staff simply delete the channel in Discord.

No database is used. A ticket is recognised by its channel topic
("evergreen-ticket:<discord user id>"), so nothing needs to be stored
and nothing needs migrating.
"""

import re

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds

CREATE_BUTTON_ID = "evergreen_ticket:create"
TICKET_TOPIC_PREFIX = "evergreen-ticket:"
EDIT_URL = "https://politicsandwar.com/nation/edit/"

# Compared after lower-casing and stripping everything except letters
# and digits, so "Seed Bearer", "seedbearer" and "🌱 Seed-Bearer" all match.
STAFF_ROLE_KEYS = {"everlord", "coordinator", "steward", "seedbearer"}
# Roles pinged when a ticket opens (same as the Locutus welcome message).
PING_ROLE_KEYS = {"everlord", "seedbearer"}

QUESTIONS = """Q1. Are you able to check Politics & War and Discord on a daily basis?

Q2. What is your timezone?

Q3. Are you willing to take part in raiding and military actions as part of learning the game and growing your nation?

Q4. What made you choose EVERGREEN over other alliances?

Q5. Have you played Politics and War before? If so, which alliances were you part of and for how long? If not, what about the game interests you most?

Q6. What strengths, skills, or activity can you contribute to EVERGREEN?

Q7. If required, are you willing to fight alliance enemies and follow war plans during conflicts?

Q8. Alliance wars may require sacrificing money or infrastructure for the greater good. Are you prepared to prioritize alliance objectives when necessary?

Q9. **OPSEC (Operational Security)** refers to sensitive alliance information. All content within this Discord is considered OPSEC and must not be shared outside the alliance. Do you understand and accept this rule?

Q10. Are you willing to follow leadership decisions and orders, particularly during wartime?

Q11. If you choose to leave the alliance in the future, do you agree to clear any outstanding obligations first?"""


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _roles_by_key(guild: discord.Guild, keys: set[str]) -> list[discord.Role]:
    return [r for r in guild.roles if _key(r.name) in keys]


def _find_existing_ticket(guild: discord.Guild, user_id: int) -> discord.TextChannel | None:
    topic = f"{TICKET_TOPIC_PREFIX}{user_id}"
    for channel in guild.text_channels:
        if channel.topic == topic:
            return channel
    return None


def _channel_name(user: discord.abc.User) -> str:
    clean = re.sub(r"[^a-z0-9-]", "", user.name.lower())[:80]
    return f"interview-{clean or user.id}"


async def _alliance_url(guild_id: int) -> str | None:
    try:
        config = await database.get_alerts_config(guild_id)
    except Exception as e:
        print(f"[tickets] could not read alliance id: {e}")
        return None
    alliance_id = config.get("alliance_id") if config else None
    if not alliance_id:
        return None
    return f"https://politicsandwar.com/alliance/id={alliance_id}"


def build_welcome_embed(
    applicant: discord.abc.User,
    alliance_url: str | None,
    reviewer_mention: str,
) -> discord.Embed:
    alliance_link = f"[Alliance page]({alliance_url})" if alliance_url else "the alliance page"
    description = (
        "# Welcome to the Evergreen induction process!\n\n"
        "If you haven't yet, please apply to the alliance in game by going to the "
        f"alliance page, and clicking Join: {alliance_link}\n\n"
        "After doing so please complete the following steps:\n\n"
        f"1. Go to \"[Edit]({EDIT_URL})\" in-game. At the bottom you'll find a place to enter "
        "your discord ID. Paste it in there and press \"Save Changes\".\n\n"
        "2. Return here and type the following command into chat:\n"
        "`/register nation:Your nation name`\n"
        "This links your nation to our system.\n\n"
        "^ This command must be typed manually and exactly. **Do not copy-paste it**, "
        "and ensure your nation name is spelled correctly.\n\n"
        "3. Answer these questions here to complete your interview:\n"
        f"```\n{QUESTIONS}\n```\n"
        f"Once you complete all steps, {reviewer_mention} will review your application "
        "and proceed with the next stage.\n\n"
        "**Welcome to the Evergreen.**"
    )
    embed = discord.Embed(
        title=f"Welcome to Evergreen 🍃 {applicant.name}",
        description=description,
        color=discord.Color.green(),
    )
    embed.set_footer(text=embeds.FOOTER_TEXT)
    return embed


async def create_ticket(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    user = interaction.user

    existing = _find_existing_ticket(guild, user.id)
    if existing:
        return await interaction.followup.send(
            f"You already have an open ticket: {existing.mention}", ephemeral=True
        )

    staff_roles = _roles_by_key(guild, STAFF_ROLE_KEYS)
    if not staff_roles:
        print(f"[tickets] no staff roles found in guild {guild.id} (looked for {sorted(STAFF_ROLE_KEYS)})")
        return await interaction.followup.send(
            "I couldn't find the staff roles (Everlord, Coordinator, Steward, Seed Bearer), "
            "so I didn't open a ticket. Please tell an admin.",
            ephemeral=True,
        )

    visible = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
        embed_links=True,
    )
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: visible,
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, manage_channels=True
        ),
    }
    for role in staff_roles:
        overwrites[role] = visible

    category = getattr(interaction.channel, "category", None)

    try:
        channel = await guild.create_text_channel(
            _channel_name(user),
            category=category,
            overwrites=overwrites,
            topic=f"{TICKET_TOPIC_PREFIX}{user.id}",
            reason=f"Interview ticket for {user}",
        )
    except discord.HTTPException as e:
        print(f"[tickets] could not create channel: {e}")
        return await interaction.followup.send(
            "I couldn't create your ticket channel. Please tell an admin to check "
            "my Manage Channels permission.",
            ephemeral=True,
        )

    ping_roles = [r for r in staff_roles if _key(r.name) in PING_ROLE_KEYS]
    seed_bearer = next((r for r in staff_roles if _key(r.name) == "seedbearer"), None)
    reviewer = seed_bearer.mention if seed_bearer else "Seedbearer"

    alliance_url = await _alliance_url(guild.id)
    embed = build_welcome_embed(user, alliance_url, reviewer)
    mentions = " ".join([user.mention] + [r.mention for r in ping_roles])

    try:
        await channel.send(
            content=mentions,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
    except discord.HTTPException as e:
        print(f"[tickets] could not post welcome embed in #{channel.name}: {e}")

    await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)


class TicketCreateView(discord.ui.View):
    """Persistent view: one fixed custom_id, so the button keeps working
    after every restart/redeploy."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id=CREATE_BUTTON_ID)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await create_ticket(interaction)
        except Exception as e:
            import traceback

            traceback.print_exc()
            msg = f"Something went wrong opening your ticket: `{str(e)[:150]}`"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(TicketCreateView())

    ticket_group = app_commands.Group(
        name="ticket",
        description="Interview ticket tools",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @ticket_group.command(name="setup", description="Post the Create Ticket button in this channel")
    async def setup_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="EVERGREEN Interview ticket",
            description="click the button below to create a ticket",
            color=discord.Color.green(),
        )
        embed.set_footer(text=embeds.FOOTER_TEXT)
        try:
            await interaction.channel.send(embed=embed, view=TicketCreateView())
        except discord.HTTPException as e:
            return await interaction.response.send_message(
                f"I couldn't post here: `{e}`. Check that I can send messages in this channel.",
                ephemeral=True,
            )
        await interaction.response.send_message("Ticket panel posted.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
