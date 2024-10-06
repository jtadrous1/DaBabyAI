import nextcord
from nextcord.ext import commands
import yt_dlp as youtube_dl
import random
import asyncio
import os
import webserver

DISCORD_TOKEN = os.environ["discordkey"]
# Bot configuration
intents = nextcord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Song queue for each guild (server)
queues = {}

# Track the last message with controls and "Now playing"
now_playing_messages = {}

# Track "Added to the queue" messages for each song in a guild
queue_messages = {}

# Timeout setting in seconds (e.g., 300 seconds = 5 minutes)
VOICE_TIMEOUT = 300  # Adjust as needed

# FFmpeg options to ensure reconnection on stream failures
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# UI class with music controls
class MusicControls(nextcord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    # Shuffle button
    @nextcord.ui.button(label="Shuffle", style=nextcord.ButtonStyle.primary)
    async def shuffle(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        if self.guild_id in queues and queues[self.guild_id]["songs"]:
            random.shuffle(queues[self.guild_id]["songs"])
            await interaction.response.send_message("The queue has been shuffled!", ephemeral=True)
        else:
            await interaction.response.send_message("No songs to shuffle!", ephemeral=True)

    # Skip button
    @nextcord.ui.button(label="Skip", style=nextcord.ButtonStyle.secondary)
    async def skip(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        if self.guild_id in queues and is_playing(self.guild_id):
            queues[self.guild_id]["voice_client"].stop()
            await interaction.response.send_message("Skipped the current song.", ephemeral=True)
        else:
            await interaction.response.send_message("No music is currently playing.", ephemeral=True)

    # Pause button
    @nextcord.ui.button(label="Pause", style=nextcord.ButtonStyle.danger)
    async def pause(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        voice_client = queues[self.guild_id]["voice_client"]
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            await interaction.response.send_message("Paused the music.", ephemeral=True)
        else:
            await interaction.response.send_message("No music is currently playing.", ephemeral=True)

    # Resume button
    @nextcord.ui.button(label="Resume", style=nextcord.ButtonStyle.success)
    async def resume(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        voice_client = queues[self.guild_id]["voice_client"]
        if voice_client and voice_client.is_paused():
            voice_client.resume()
            await interaction.response.send_message("Resumed the music.", ephemeral=True)
        else:
            await interaction.response.send_message("No music is currently paused.", ephemeral=True)

# Helper function to check if a song is currently playing
def is_playing(guild_id):
    return guild_id in queues and queues[guild_id]["voice_client"] and queues[guild_id]["voice_client"].is_playing()

# Function to delete all bot messages in the music chat
async def delete_all_bot_messages(guild_id):
    try:
        channel = queues[guild_id]["text_channel"]  # Get the music chat
        messages = await channel.history(limit=100).flatten()  # Fetch the last 100 messages (adjust if necessary)

        for message in messages:
            if message.author == bot.user:
                await message.delete()  # Delete only bot messages

    except Exception as e:
        print(f"Error deleting bot messages: {e}")

# Function to update the Now Playing message with controls
async def update_now_playing_message(guild_id, channel, song_title):
    if guild_id in now_playing_messages:
        try:
            # Edit the existing "Now playing" message with the new song title and controls
            message = now_playing_messages[guild_id]
            await message.edit(content=f"Now playing: {song_title}", view=MusicControls(guild_id))
        except Exception as e:
            print(f"Error updating Now Playing message: {e}")
    else:
        # Send a new "Now playing" message if none exists
        msg = await channel.send(f"Now playing: {song_title}", view=MusicControls(guild_id))
        now_playing_messages[guild_id] = msg

# Function to delete "Added to the queue" message when song starts
async def delete_queue_message(guild_id):
    if guild_id in queue_messages and len(queue_messages[guild_id]) > 0:
        try:
            # Get the first queued message (FIFO) and delete it
            message = queue_messages[guild_id].pop(0)
            await message.delete()
        except Exception as e:
            print(f"Error deleting queue message: {e}")

# Function to disconnect bot after a timeout if no activity
async def disconnect_after_timeout(guild_id):
    await asyncio.sleep(VOICE_TIMEOUT)  # Wait for the timeout period
    if guild_id in queues and not is_playing(guild_id):
        # Check if there is still no music playing and disconnect the bot
        await queues[guild_id]["voice_client"].disconnect()
        del queues[guild_id]
        # Delete the Now Playing message when bot disconnects
        if guild_id in now_playing_messages:
            await now_playing_messages[guild_id].delete()
            del now_playing_messages[guild_id]
        # Delete all bot messages in the music chat
        await delete_all_bot_messages(guild_id)

# Helper function to play the next song in the queue
async def play_next(guild_id):
    try:
        if len(queues[guild_id]["songs"]) > 0:
            # Get the next song
            next_song = queues[guild_id]["songs"].pop(0)
            voice_client = queues[guild_id]["voice_client"]

            # Play the song with reconnection options for FFmpeg
            voice_client.play(
                nextcord.FFmpegPCMAudio(executable="ffmpeg", source=next_song['url'], **FFMPEG_OPTIONS), 
                after=lambda e: asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
            )

            # Delete the correct "Added to the queue" message when song starts
            await delete_queue_message(guild_id)

            # Announce the song with controls
            channel = queues[guild_id]["text_channel"]
            await update_now_playing_message(guild_id, channel, next_song['title'])
        else:
            # Start a timeout for inactivity if the queue is empty
            await disconnect_after_timeout(guild_id)
    except Exception as e:
        print(f"Error while playing next song: {e}")

# Command to play music by searching for a song
@bot.slash_command(name="play", description="Plays music by searching YouTube")
async def play(interaction: nextcord.Interaction, song: str):
    # Defer the response if the process takes longer
    await interaction.response.defer()

    # Join the voice channel
    if not interaction.user.voice:
        await interaction.followup.send("You are not in a voice channel!", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    guild_id = interaction.guild.id
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await channel.connect()

    # Set up yt-dlp to search for the song on YouTube
    ydl_opts = {
        'format': 'bestaudio',
        'noplaylist': 'True',
        'default_search': 'ytsearch',  # Search YouTube for the song
        'quiet': True
    }

    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch:{song}", download=False)
        if not info['entries']:
            await interaction.followup.send(f"No results found for {song}.", ephemeral=True)
            return
        
        video_info = info['entries'][0]  # Get the first search result
        video_url = video_info['url']
        song = {'title': video_info['title'], 'url': video_url}

        # Initialize queue if it's the first time for the guild
        if guild_id not in queues:
            queues[guild_id] = {
                "songs": [],
                "voice_client": voice_client,
                "text_channel": interaction.channel
            }
            queue_messages[guild_id] = []  # Initialize queue messages list for the guild
        
        # Add the song to the queue
        queues[guild_id]["songs"].append(song)

        # Play the song if nothing is playing
        if not is_playing(guild_id):
            await play_next(guild_id)
        else:
            # Announce that the song has been added to the queue (if music is already playing)
            msg = await interaction.followup.send(f"Added to the queue: {video_info['title']}")
            # Track the "Added to the queue" message so it can be deleted when the song starts
            queue_messages[guild_id].append(msg)  # Track the message in a FIFO manner for each guild

# Command to stop the music
@bot.slash_command(name="stop", description="Stops the music and clears the queue")
async def stop(interaction: nextcord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in queues:
        # Stop the music and clear the queue
        queues[guild_id]["voice_client"].stop()
        queues[guild_id]["songs"].clear()
        await queues[guild_id]["voice_client"].disconnect()
        del queues[guild_id]
        # Delete the "Now Playing" message
        if guild_id in now_playing_messages:
            await now_playing_messages[guild_id].delete()
            del now_playing_messages[guild_id]
        # Delete all bot messages in the music chat
        await delete_all_bot_messages(guild_id)
        await interaction.response.send_message("Stopped the music and cleared the queue.")
    else:
        await interaction.response.send_message("No music is currently playing.")

# Command to skip the current song
@bot.slash_command(name="skip", description="Skips the current song")
async def skip(interaction: nextcord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in queues and is_playing(guild_id):
        # Skip the current song
        queues[guild_id]["voice_client"].stop()

        # Send the confirmation and delete the message after 5 seconds
        await interaction.response.send_message("Skipped the current song.", ephemeral=False, delete_after=5)
    else:
        await interaction.response.send_message("No music is currently playing.", ephemeral=True)


webserver.keep_alive()

