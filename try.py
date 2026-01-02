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
ZOOM_NUMBER = os.getenv("ZOOM_NUMBER")
OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

async def entrypoint(ctx: JobContext):
    # Initialize API Client
    lk_api = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)

    try:
        # 1. CONNECT
        await ctx.connect()
        print(f"Room Created: {ctx.room.name}")

        # 2. THE FIREWALL (Critical Fix)
        # This blocks the User and Agent from hearing each other's raw audio
        @ctx.room.on("track_published")
        def on_track_published(publication, participant):
            asyncio.create_task(enforce_firewall(ctx.room, lk_api))

        @ctx.room.on("participant_connected")
        def on_participant_connected(participant):
            # If Zoom Agent joins, start Brain #2
            if participant.identity.startswith("zoom-agent"):
                print(f"Zoom Agent Joined. Starting Agent Translator...")
                asyncio.create_task(start_agent_translator(ctx, participant, lk_api))
            
            # Run firewall to block new connections
            asyncio.create_task(enforce_firewall(ctx.room, lk_api))

        # 3. Start Brain #1 (User Translator)
        await start_user_translator(ctx, lk_api)

        # 4. Dial Zoom
        print(f"Dialing Zoom Agent at {ZOOM_NUMBER}...")
        asyncio.create_task(dial_zoom_agent(ctx.room.name, ZOOM_NUMBER, lk_api))

        await asyncio.Event().wait()

    finally:
        await lk_api.aclose()


# ============================================================
# BRAIN #1: USER TRANSLATOR (English -> Tamil)
# ============================================================
async def start_user_translator(ctx: JobContext, lk_api):
    initial_ctx = ChatContext(
        messages=[
            ChatMessage(
                role="system",
                content=("You are a translator. Listen to the User (English/Tamil). Translate to Tamil/English. Just output the translation.")
            )
        ]
    )
    agent = VoicePipelineAgent(
        vad=silero.VAD.load(),
        stt=openai.STT(),
        llm=openai.LLM(model="gpt-4o"),
        tts=openai.TTS(),
        chat_ctx=initial_ctx,
    )
    agent.start(ctx.room)

    # --- ROUTING: MUTE USER WHEN AI SPEAKS ---
    # When translating the User, ONLY the Agent should hear the AI.
    @agent.on("agent_started_speaking")
    def route_audio_to_agent():
        asyncio.create_task(route_ai_audio_exclusively(ctx.room, lk_api, listener="agent"))

    @agent.on("agent_stopped_speaking")
    def reset_audio():
        # When done, let everyone hear AI again (ready for next turn)
        asyncio.create_task(reset_ai_hearing_for_all(ctx.room, lk_api))

    await asyncio.sleep(1)
    await agent.say("I am your translator. Connecting...", allow_interruptions=True)


# ============================================================
# BRAIN #2: AGENT TRANSLATOR (Tamil -> English)
# ============================================================
async def start_agent_translator(ctx: JobContext, participant, lk_api):
    initial_ctx = ChatContext(
        messages=[
            ChatMessage(
                role="system",
                content=("You are a translator. Listen to the Agent (English/Tamil). Translate to Tamil/English. Just output the translation.")
            )
        ]
    )
    agent = VoicePipelineAgent(
        vad=silero.VAD.load(),
        stt=openai.STT(),
        llm=openai.LLM(model="gpt-4o"),
        tts=openai.TTS(),
        chat_ctx=initial_ctx,
    )
    agent.start(ctx.room, participant=participant)

    # --- ROUTING: MUTE AGENT WHEN AI SPEAKS ---
    # When translating the Agent, ONLY the User should hear the AI.
    @agent.on("agent_started_speaking")
    def route_audio_to_user():
        asyncio.create_task(route_ai_audio_exclusively(ctx.room, lk_api, listener="user"))

    @agent.on("agent_stopped_speaking")
    def reset_audio():
        asyncio.create_task(reset_ai_hearing_for_all(ctx.room, lk_api))


# ============================================================
# FIREWALL & ROUTING LOGIC
# ============================================================

async def enforce_firewall(room, lk_api):
    """
    The Firewall: Ensures User and Agent are PERMANENTLY MUTED from each other.
    """
    try:
        user_id, agent_id = get_identities(room)
        if not (user_id and agent_id): return

        # Get all audio tracks in the room
        all_tracks = []
        for p in room.remote_participants.values():
            for t in p.track_publications.values():
                if t.kind == rtc.TrackKind.KIND_AUDIO:
                    all_tracks.append((p.identity, t.sid))

        for owner_id, track_sid in all_tracks:
            # If Track belongs to Agent -> MUTE for User
            if owner_id == agent_id:
                await lk_api.room.update_subscriptions(
                    api.UpdateSubscriptionsRequest(
                        room=room.name, identity=user_id, track_sids=[track_sid], subscribe=False
                    )
                )
            
            # If Track belongs to User -> MUTE for Agent
            elif owner_id == user_id:
                await lk_api.room.update_subscriptions(
                    api.UpdateSubscriptionsRequest(
                        room=room.name, identity=agent_id, track_sids=[track_sid], subscribe=False
                    )
                )
    except Exception:
        pass

async def route_ai_audio_exclusively(room, lk_api, listener: str):
    """
    Controls who hears the AI.
    """
    try:
        ai_tracks = [t.sid for t in room.local_participant.track_publications.values()]
        if not ai_tracks: return

        user_id, agent_id = get_identities(room)
        
        tasks = []
        if listener == "agent" and agent_id and user_id:
            # Agent hears AI, User is blocked
            tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=ai_tracks, subscribe=True)))
            tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=ai_tracks, subscribe=False)))

        elif listener == "user" and user_id and agent_id:
            # User hears AI, Agent is blocked
            tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=ai_tracks, subscribe=True)))
            tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=ai_tracks, subscribe=False)))

        if tasks: await asyncio.gather(*tasks)
    except Exception: pass

async def reset_ai_hearing_for_all(room, lk_api):
    """
    Unmutes AI for everyone (Ready state).
    """
    try:
        ai_tracks = [t.sid for t in room.local_participant.track_publications.values()]
        user_id, agent_id = get_identities(room)
        tasks = []
        if user_id: tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=ai_tracks, subscribe=True)))
        if agent_id: tasks.append(lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=ai_tracks, subscribe=True)))
        if tasks: await asyncio.gather(*tasks)
    except Exception: pass

def get_identities(room):
    user_id = None
    agent_id = None
    for p in room.remote_participants.values():
        if p.identity.startswith("zoom-agent"): agent_id = p.identity
        elif not p.identity.startswith("agent"): user_id = p.identity
    return user_id, agent_id

async def dial_zoom_agent(room_name: str, phone_number: str, lk_api):
    try:
        await lk_api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=OUTBOUND_TRUNK_ID,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity=f"zoom-agent-{phone_number}",
                participant_name="Neurealm Agent"
            )
        )
    except Exception as e:
        print(f"Failed to dial agent: {e}")

if __name__ == "__main__":
    print("Starting Neurealm Agent...")
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="neurealm-bot"  
    ))