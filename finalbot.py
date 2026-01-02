import asyncio
import os
import json
import webbrowser
import pathlib
import sys
import jwt  # Standard PyJWT library
from dotenv import load_dotenv

# 1. LOAD CONFIGURATION
load_dotenv()

# ============================================================
# 🩺 THE TOKEN SURGEON (Fixes 401 & Attribute Errors)
# ============================================================
from livekit import api

# Save the original method so we can use it to generate the payload
_original_to_jwt = api.AccessToken.to_jwt

def patched_to_jwt(self):
    """
    1. Let the library generate the token (so it gets grants/identity right).
    2. Decode it.
    3. FORCE the dates to be safe (2025).
    4. Re-sign it.
    """
    # Step 1: Generate the bad (2026) token using the original method
    # This might fail if the library checks time internally, but usually it doesn't throw here.
    try:
        raw_token = _original_to_jwt(self)
    except Exception:
        # If original fails, we construct a fallback payload manually
        raw_token = None

    # Step 2: Prepare safe timestamps (Jan 1, 2024 to Jan 1, 2030)
    SAFE_IAT = 1704067200
    SAFE_EXP = 1893456000

    if raw_token:
        # Decode the token to get the payload (grants, identity, etc.)
        payload = jwt.decode(raw_token, options={"verify_signature": False})
    else:
        # Fallback if original method completely failed
        payload = {
            "iss": self.api_key,
            "sub": self.identity,
            "video": {"room": "neurealm-room", "roomJoin": True} # Fallback grants
        }

    # Step 3: SURGERY - Overwrite the time fields
    payload['iat'] = SAFE_IAT
    payload['nbf'] = SAFE_IAT
    payload['exp'] = SAFE_EXP
    
    # Step 4: Re-sign with the API Secret
    return jwt.encode(payload, self.api_secret, algorithm="HS256")

# Apply the Patch
api.AccessToken.to_jwt = patched_to_jwt
print("🩺 TOKEN SURGEON APPLIED: All tokens are now being rewritten with 2024-2030 timestamps.")

# ============================================================
# IMPORTS (After patch)
# ============================================================
from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.pipeline import VoicePipelineAgent 
from livekit.plugins import openai, silero

# CONFIGURATION VARIABLES
LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
ZOOM_NUMBER = os.getenv("ZOOM_NUMBER")
OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID")
TWILIO_CALLER_ID = os.getenv("TWILIO_CALLER_ID")
FIXED_ROOM_NAME = "neurealm-room"

BOT_ENABLED = True 

# ============================================================
# MAIN BOT LOGIC
# ============================================================
async def entrypoint(ctx: JobContext):
    global BOT_ENABLED
    lk_api = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)

    try:
        await ctx.connect(auto_subscribe=False)
        print(f"✅ Bot Connected to {ctx.room.name}!")

        @ctx.room.on("data_received")
        def on_data_received(data_packet: rtc.DataPacket):
            global BOT_ENABLED
            try:
                text_data = data_packet.data.decode('utf-8')
                payload = json.loads(text_data)
                
                if payload.get("action") == "toggle_bot":
                    new_state = payload.get("enabled", True)
                    if BOT_ENABLED != new_state:
                        BOT_ENABLED = new_state
                        status = "ON 🟢" if BOT_ENABLED else "OFF 🔴"
                        print(f"--- SWITCH: {status} ---")
                        asyncio.create_task(apply_bot_toggle_state(ctx.room))
            except Exception: pass

        @ctx.room.on("track_published")
        def on_track_published(publication, participant):
            if publication.kind == rtc.TrackKind.KIND_AUDIO:
                publication.set_subscribed(BOT_ENABLED)
                asyncio.create_task(block_raw_audio_instantly(ctx.room, lk_api, publication.sid, participant.identity))

        @ctx.room.on("participant_connected")
        def on_participant_connected(participant):
            if participant.identity.startswith("zoom-agent"):
                print(f"Zoom Agent Joined.")
                asyncio.create_task(start_agent_translator(ctx, participant, lk_api))
            asyncio.create_task(continuous_firewall(ctx.room, lk_api))

        await start_user_translator(ctx, lk_api)
        
        if ZOOM_NUMBER:
            print(f"📞 Dialing {ZOOM_NUMBER}...")
            asyncio.create_task(dial_zoom_agent(ctx.room.name, ZOOM_NUMBER, lk_api))
        else:
            print("⚠️ No ZOOM_NUMBER found in .env, skipping dial-out.")

        await asyncio.Event().wait()

    finally:
        await lk_api.aclose()

# ============================================================
# HELPERS
# ============================================================
async def apply_bot_toggle_state(room):
    global BOT_ENABLED
    for p in room.remote_participants.values():
        for track_pub in p.track_publications.values():
            if track_pub.kind == rtc.TrackKind.KIND_AUDIO:
                track_pub.set_subscribed(BOT_ENABLED)

async def block_raw_audio_instantly(room, lk_api, track_sid, source_identity):
    try:
        if source_identity.startswith("neurealm"): return
        user_id, agent_id = get_identities(room)
        if source_identity == user_id and agent_id:
            await lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=agent_id, track_sids=[track_sid], subscribe=False))
        elif source_identity == agent_id and user_id:
            await lk_api.room.update_subscriptions(api.UpdateSubscriptionsRequest(room=room.name, identity=user_id, track_sids=[track_sid], subscribe=False))
    except Exception: pass

async def continuous_firewall(room, lk_api):
    while True:
        try:
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

def get_identities(room):
    user_id = None
    agent_id = None
    for p in room.remote_participants.values():
        if p.identity.startswith("zoom-agent"): agent_id = p.identity
        elif not p.identity.startswith("agent") and not p.identity.startswith("controller"): user_id = p.identity
    return user_id, agent_id

async def dial_zoom_agent(room_name: str, phone_number: str, lk_api):
    try:
        await lk_api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=OUTBOUND_TRUNK_ID, sip_call_to=phone_number, room_name=room_name,
                participant_identity=f"zoom-agent-{phone_number}", participant_name="Neurealm Agent",
                sip_cid=TWILIO_CALLER_ID
            )
        )
    except Exception as e: print(f"Dial Error: {e}")

async def start_user_translator(ctx: JobContext, lk_api):
    initial_ctx = ChatContext(messages=[ChatMessage(role="system", content=("You are a translator. Listen to the User (English). Translate to Tamil."))])
    agent = VoicePipelineAgent(vad=silero.VAD.load(min_silence_duration=0.4), stt=openai.STT(), llm=openai.LLM(model="gpt-4o"), tts=openai.TTS(), chat_ctx=initial_ctx)
    agent.start(ctx.room)
    @agent.on("agent_started_speaking")
    def route_audio_to_agent():
        if BOT_ENABLED: asyncio.create_task(route_ai_audio_exclusively(ctx.room, lk_api, listener="agent"))
    @agent.on("agent_stopped_speaking")
    def reset_audio():
        if BOT_ENABLED: asyncio.create_task(reset_ai_hearing_for_all(ctx.room, lk_api))
    await asyncio.sleep(1)
    await agent.say("Translator ready.", allow_interruptions=True)

async def start_agent_translator(ctx: JobContext, participant, lk_api):
    initial_ctx = ChatContext(messages=[ChatMessage(role="system", content=("You are a translator. Listen to the Agent (Tamil). Translate to English."))])
    agent = VoicePipelineAgent(vad=silero.VAD.load(min_silence_duration=0.4), stt=openai.STT(), llm=openai.LLM(model="gpt-4o"), tts=openai.TTS(), chat_ctx=initial_ctx)
    agent.start(ctx.room, participant=participant)
    @agent.on("agent_started_speaking")
    def route_audio_to_user():
        if BOT_ENABLED: asyncio.create_task(route_ai_audio_exclusively(ctx.room, lk_api, listener="user"))
    @agent.on("agent_stopped_speaking")
    def reset_audio():
        if BOT_ENABLED: asyncio.create_task(reset_ai_hearing_for_all(ctx.room, lk_api))

# ============================================================
# LAUNCHER
# ============================================================
def open_controller():
    try:
        # Generate controller token (which will also get surgically repaired automatically)
        token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
            .with_identity("controller-user") \
            .with_name("Controller") \
            .with_grants(api.VideoGrants(room_join=True, room=FIXED_ROOM_NAME)) \
            .to_jwt()

        current_dir = pathlib.Path(__file__).parent.resolve()
        file_path = current_dir / "index.html"
        if file_path.exists():
            webbrowser.open(f"{file_path.as_uri()}?url={LIVEKIT_URL}&token={token}")
            print(f"🚀 Controller Opened.")
    except Exception: pass

if __name__ == "__main__":
    open_controller()
    
    # Force "start" mode
    sys.argv = ["finalbot.py", "start"]
    
    # Run the App
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="neurealm-bot"))