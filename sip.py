import asyncio
import logging
import os
from dotenv import load_dotenv

from livekit import api, rtc
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.pipeline import VoicePipelineAgent 
from livekit.plugins import openai, silero

load_dotenv()

# --- CONFIGURATION ---
ZOOM_NUMBERS_RAW = os.getenv("ZOOM_NUMBER", "")
# Convert comma string to list: ["+1555...", "+1666..."]
AGENT_POOL = [num.strip() for num in ZOOM_NUMBERS_RAW.split(",") if num.strip()]

OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

# --- GLOBAL STATE (SHARED ACROSS CALLS) ---
# Stores numbers of agents currently in a call or ringing
BUSY_AGENTS = set()

async def entrypoint(ctx: JobContext):
    lk_api = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    
    # Signal to know when an agent actually picks up
    agent_connected_event = asyncio.Event()
    
    # Track which agent we successfully connected to (so we can free them later)
    connected_agent_number = None

    try:
        # 1. CONNECT SECURELY
        await ctx.connect(auto_subscribe=False)
        print(f"Secure Room Created: {ctx.room.name}")

        # 2. SETUP EVENT LISTENERS
        @ctx.room.on("track_published")
        def on_track_published(publication, participant):
            if publication.kind == rtc.TrackKind.KIND_AUDIO:
                publication.set_subscribed(True)
                asyncio.create_task(block_raw_audio_instantly(ctx.room, lk_api, publication.sid, participant.identity))

        @ctx.room.on("local_track_published")
        def on_local_track_published(publication, participant):
            asyncio.create_task(enable_ai_audio_for_all(ctx.room, lk_api, publication.sid))

        @ctx.room.on("participant_connected")
        def on_participant_connected(participant):
            if participant.identity.startswith("zoom-agent"):
                print(f"AGENT PICKED UP: {participant.identity}")
                agent_connected_event.set() # Stop the hunt!
                
                asyncio.create_task(start_agent_translator(ctx, participant, lk_api))
                asyncio.create_task(ensure_humans_hear_ai(ctx.room, lk_api))

            asyncio.create_task(continuous_firewall(ctx.room, lk_api))

        # 3. START HUNTING LOGIC
        # Run this in background so we can start User Translator immediately
        hunt_task = asyncio.create_task(run_sip_hunt(ctx, lk_api, agent_connected_event))

        # 4. START USER TRANSLATOR (Brain 1)
        await start_user_translator(ctx, lk_api)

        # Wait for the call to end
        await asyncio.Event().wait()

    finally:
        # CLEANUP: If we connected to an agent, mark them as FREE now
        # We need to retrieve the number from the hunt task if possible, 
        # or we rely on the global set logic handled inside run_sip_hunt logic cleanup.
        # (The run_sip_hunt function handles the 'BUSY' cleanup automatically)
        await lk_api.aclose()


# ============================================================
# SIP HUNTING LOGIC (THE BRAIN)
# ============================================================
async def run_sip_hunt(ctx, lk_api, connected_event):
    """
    Iterates through AGENT_POOL.
    Skips BUSY agents.
    Dials FREE agents.
    Waits 15s. If no answer -> Next.
    """
    global BUSY_AGENTS
    
    print(f"Starting Hunt. Agents available: {len(AGENT_POOL)}")
    print(f"Currently Busy: {BUSY_AGENTS}")

    agent_found = False

    for number in AGENT_POOL:
        # 1. Check if agent is busy
        if number in BUSY_AGENTS:
            print(f"Skipping {number} (Busy)")
            continue

        # 2. Mark as Busy (Lock the agent)
        BUSY_AGENTS.add(number)
        print(f"Dialing {number}...")

        try:
            # 3. Dial
            await dial_single_number(ctx.room.name, number, lk_api)

            # 4. Wait for Pickup (e.g., 15 seconds)
            # wait_for will throw TimeoutError if connected_event isn't set in time
            await asyncio.wait_for(connected_event.wait(), timeout=15.0)
            
            # If we get here, they picked up!
            print(f"Connection Established with {number}")
            agent_found = True
            
            # Keep them marked as BUSY until the call ends (ctx disconnect)
            # We wait here until the whole room closes
            try:
                while not ctx.room.is_disconnected:
                    await asyncio.sleep(1)
            finally:
                # Once call ends, free the agent
                print(f"Call ended. Freeing Agent {number}")
                BUSY_AGENTS.remove(number)
                return

        except asyncio.TimeoutError:
            print(f"No answer from {number} in 15s. Trying next...")
            
            # CRITICAL: We must hang up on this agent so their phone stops ringing
            # before we dial the next one.
            try:
                await lk_api.room.remove_participant(
                    api.RoomParticipantIdentity(room=ctx.room.name, identity=f"zoom-agent-{number}")
                )
            except Exception: pass

            # They didn't answer, so they are technically "Free" again
            BUSY_AGENTS.remove(number)
            # Loop continues to next number...
        
        except Exception as e:
            print(f"Error dialing {number}: {e}")
            if number in BUSY_AGENTS: BUSY_AGENTS.remove(number)

    if not agent_found:
        print("All agents are busy or unavailable.")
        # Optional: Play a "Sorry" message to user via TTS


async def dial_single_number(room_name, number, lk_api):
    await lk_api.sip.create_sip_participant(
        api.CreateSIPParticipantRequest(
            sip_trunk_id=OUTBOUND_TRUNK_ID,
            sip_call_to=number,
            room_name=room_name,
            participant_identity=f"zoom-agent-{number}",
            participant_name=f"Agent {number}"
        )
    )


# ============================================================
# TRANSLATOR LOGIC (SAME AS BEFORE)
# ============================================================
async def start_user_translator(ctx: JobContext, lk_api):
    initial_ctx = ChatContext(messages=[ChatMessage(role="system", content=("You are a translator. Listen to the User (English/Tamil). Translate to Tamil/English. Just output the translation."))])
    agent = VoicePipelineAgent(vad=silero.VAD.load(min_silence_duration=0.4), stt=openai.STT(), llm=openai.LLM(model="gpt-4o"), tts=openai.TTS(), chat_ctx=initial_ctx)
    agent.start(ctx.room)

    @agent.on("agent_started_speaking")
    def route_audio_to_agent():
        asyncio.create_task(route_ai_audio_exclusively(ctx.room, lk_api, listener="agent"))

    @agent.on("agent_stopped_speaking")
    def reset_audio():
        asyncio.create_task(reset_ai_hearing_for_all(ctx.room, lk_api))

    await asyncio.sleep(1)
    # Use a generic greeting until agent connects
    await agent.say("Welcome to Neurealm. Please hold while I find an agent...", allow_interruptions=True)

async def start_agent_translator(ctx: JobContext, participant, lk_api):
    initial_ctx = ChatContext(messages=[ChatMessage(role="system", content=("You are a translator. Listen to the Agent (Tamil). Translate to English. Just output the translation."))])
    agent = VoicePipelineAgent(vad=silero.VAD.load(min_silence_duration=0.4), stt=openai.STT(), llm=openai.LLM(model="gpt-4o"), tts=openai.TTS(), chat_ctx=initial_ctx)
    agent.start(ctx.room, participant=participant)

    @agent.on("agent_started_speaking")
    def route_audio_to_user():
        asyncio.create_task(route_ai_audio_exclusively(ctx.room, lk_api, listener="user"))

    @agent.on("agent_stopped_speaking")
    def reset_audio():
        asyncio.create_task(reset_ai_hearing_for_all(ctx.room, lk_api))


# ============================================================
# AUDIO CONTROL & FIREWALL (PRESERVED)
# ============================================================

async def block_raw_audio_instantly(room, lk_api, track_sid, source_identity):
    try:
        if source_identity.startswith("neurealm") or source_identity.startswith("agent-neurealm"): return
        user_id, agent_id = get_identities(room)
        if source_identity == user_id and agent_id:
            await lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=[track_sid], subscribe=False))
        elif source_identity == agent_id and user_id:
            await lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=[track_sid], subscribe=False))
    except Exception: pass

async def continuous_firewall(room, lk_api):
    while True:
        try:
            if room.is_disconnected: break
            user_id, agent_id = get_identities(room)
            if user_id and agent_id:
                if user_id in room.remote_participants:
                    u_tracks = [t.sid for t in room.remote_participants[user_id].track_publications.values()]
                    if u_tracks: await lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=u_tracks, subscribe=False))
                if agent_id in room.remote_participants:
                    a_tracks = [t.sid for t in room.remote_participants[agent_id].track_publications.values()]
                    if a_tracks: await lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=a_tracks, subscribe=False))
        except Exception: pass
        await asyncio.sleep(2.0)

async def route_ai_audio_exclusively(room, lk_api, listener: str):
    try:
        ai_tracks = [t.sid for t in room.local_participant.track_publications.values()]
        if not ai_tracks: return
        user_id, agent_id = get_identities(room)
        tasks = []
        if listener == "agent" and agent_id and user_id:
            tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=ai_tracks, subscribe=True)))
            tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=ai_tracks, subscribe=False)))
        elif listener == "user" and user_id and agent_id:
            tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=ai_tracks, subscribe=True)))
            tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=ai_tracks, subscribe=False)))
        if tasks: await asyncio.gather(*tasks)
    except Exception: pass

async def reset_ai_hearing_for_all(room, lk_api):
    try:
        ai_tracks = [t.sid for t in room.local_participant.track_publications.values()]
        user_id, agent_id = get_identities(room)
        tasks = []
        if user_id: tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=ai_tracks, subscribe=True)))
        if agent_id: tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=ai_tracks, subscribe=True)))
        if tasks: await asyncio.gather(*tasks)
    except Exception: pass

async def ensure_humans_hear_ai(room, lk_api):
    asyncio.create_task(reset_ai_hearing_for_all(room, lk_api))

async def enable_ai_audio_for_all(room, lk_api, ai_track_sid):
    user_id, agent_id = get_identities(room)
    if user_id: await lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=[ai_track_sid], subscribe=True))
    if agent_id: await lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=[ai_track_sid], subscribe=True))

def get_identities(room):
    user_id = None
    agent_id = None
    for p in room.remote_participants.values():
        if p.identity.startswith("zoom-agent"): agent_id = p.identity
        elif not p.identity.startswith("agent"): user_id = p.identity
    return user_id, agent_id

if __name__ == "__main__":
    print("Starting Neurealm Hunting Agent...")
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="neurealm-bot"))