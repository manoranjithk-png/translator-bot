import asyncio
import logging
import os
from dotenv import load_dotenv

from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.pipeline import VoicePipelineAgent 
from livekit.plugins import openai, silero
from livekit import api

load_dotenv()

# --- CONFIGURATION ---
ZOOM_NUMBER = os.getenv("ZOOM_NUMBER")
OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

async def entrypoint(ctx: JobContext):
    # 1. Initialize API Client INSIDE the function (Fixes the error)
    lk_api = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)

    try:
        await ctx.connect()
        print(f"Room Created: {ctx.room.name}")

        # 2. Start the USER Translator (Listens to You)
        # This agent starts immediately.
        await start_user_translator(ctx)

        # 3. Listen for the Zoom Agent to Join
        @ctx.room.on("participant_connected")
        def on_participant_connected(participant):
            if participant.identity.startswith("zoom-agent"):
                print(f"Zoom Agent Joined ({participant.identity}). Starting Second Translator...")
                # Start the AGENT Translator (Listens to Zoom)
                asyncio.create_task(start_agent_translator(ctx, participant))
                
            # Enforce Privacy (Pass lk_api)
            asyncio.create_task(enforce_isolation(ctx.room, lk_api))

        @ctx.room.on("track_published")
        def on_track_published(publication, participant):
            asyncio.create_task(enforce_isolation(ctx.room, lk_api))

        # 4. Dial Zoom Immediately (Pass lk_api)
        print(f"Dialing Zoom Agent at {ZOOM_NUMBER}...")
        asyncio.create_task(dial_zoom_agent(ctx.room.name, ZOOM_NUMBER, lk_api))

        # Keep the process alive
        await asyncio.Event().wait()

    finally:
        # Close the API connection cleanly when the call ends
        await lk_api.aclose()

async def start_user_translator(ctx: JobContext):
    """
    Brain #1: Listens to the USER (RingCentral).
    Translates English/Tamil -> Tamil/English.
    """
    initial_ctx = ChatContext(
        messages=[
            ChatMessage(
                role="system",
                content=(
                    "You are a translator for the USER. "
                    "1. Listen to the User's voice ONLY. "
                    "2. If they speak English, translate to Tamil. "
                    "3. If they speak Tamil, translate to English. "
                    "4. Do not add filler words. Just speak the translation."
                ),
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
    
    # Attach to the Room (Defaults to the User)
    agent.start(ctx.room)
    
    await asyncio.sleep(1)
    await agent.say("I am your translator. Connecting to agent...", allow_interruptions=True)

async def start_agent_translator(ctx: JobContext, participant):
    """
    Brain #2: Listens to the AGENT (Zoom).
    Translates English/Tamil -> Tamil/English.
    """
    initial_ctx = ChatContext(
        messages=[
            ChatMessage(
                role="system",
                content=(
                    "You are a translator for the AGENT. "
                    "1. Listen to the Agent's voice ONLY. "
                    "2. If they speak English, translate to Tamil. "
                    "3. If they speak Tamil, translate to English. "
                    "4. Do not add filler words. Just speak the translation."
                ),
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
    
    # Attach SPECIFICALLY to the Zoom Agent
    agent.start(ctx.room, participant=participant)

async def enforce_isolation(room, lk_api):
    """
    Ensures User and Agent hear ONLY the AI, not each other.
    """
    try:
        user_identity = None
        agent_identity = None
        
        for p in room.remote_participants.values():
            if p.identity.startswith("zoom-agent"):
                agent_identity = p.identity
            elif not p.identity.startswith("agent"): 
                user_identity = p.identity

        if user_identity and agent_identity:
            # Mute Agent for User
            agent_tracks = [t.sid for t in room.remote_participants.values() if t.identity == agent_identity for t in t.track_publications.values()]
            if agent_tracks:
                await lk_api.room.update_subscriptions(
                    api.UpdateSubscriptionsRequest(
                        room=room.name,
                        identity=user_identity,
                        track_sids=agent_tracks,
                        subscribe=False
                    )
                )

            # Mute User for Agent
            user_tracks = [t.sid for t in room.remote_participants.values() if t.identity == user_identity for t in t.track_publications.values()]
            if user_tracks:
                await lk_api.room.update_subscriptions(
                    api.UpdateSubscriptionsRequest(
                        room=room.name,
                        identity=agent_identity,
                        track_sids=user_tracks,
                        subscribe=False
                    )
                )
    except Exception:
        pass

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
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))