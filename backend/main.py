import os
import json
import base64
import asyncio
import websockets
import uuid
import time
import io
import traceback
import hashlib
from pathlib import Path
from fastapi import FastAPI, WebSocket, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.websockets import WebSocketDisconnect
from datetime import datetime as dt, timedelta, timezone
import jwt
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream, Parameter
from dotenv import load_dotenv
from pydub import AudioSegment
import audioop
from contextlib import suppress
import httpx

from backend.services.prompts import build_system_message
from backend.logger.call_log_apis import *
from backend.workflow.customer_card_tools import (
    verify_customer_by_cnic,
    confirm_physical_custody,
    verify_tpin,
    verify_card_details,
    activate_card,
    update_customer_tpin,
    transfer_to_ivr_for_pin,
    transfer_to_agent,
    get_customer_status,
    get_account_balance,
    reset_verification_attempts,
)
from backend.workflow.registry import (
    get_initial_phase_for_workflow,
    get_next_phase_for_tool,
    WORKFLOW_SELECTOR_TOOL_NAME,
    get_all_tools_with_selector,
    get_workflow_policy_context,
    get_workflow_context,
    get_required_tool_for_phase,
    is_tool_allowed_in_phase,
    is_tool_allowed_for_workflow,
    is_valid_workflow,
)
from backend.services.rag_tools import search_knowledge_base, prewarm_embeddings
from backend.services.audio_transcription import transcribe_audio, analyze_call_with_llm
from backend.services.gemini_live import (
    GeminiLiveClient,
    GeminiLiveConfig,
    GeminiResponse,
    GEMINI_RECEIVE_SAMPLE_RATE,
    GEMINI_VOICES,
)
from backend.utils.audio_utils import (
    convert_browser_to_gemini,
    convert_gemini_to_browser,
    reset_audio_states,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "frontend" / "static"
RECORDINGS_DIR = REPO_ROOT / "recordings"

load_dotenv(override=True)

PORT = 6089  # Different port for UBL Digital

VOICE = 'echo'

LOG_EVENT_TYPES = [
    'response.content.done', 'input_audio_buffer.committed',
    'session.created', 'conversation.item.deleted', 'conversation.item.created'
]

WARNING_EVENT_TYPES = [
    'error', 'rate_limits.updated'
]

SHOW_TIMING_MATH = False
call_recordings = {}

app = FastAPI()


@app.on_event("startup")
async def startup_prewarm():
    asyncio.create_task(prewarm_embeddings())


JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "ubl-digital-ai-call-center-secret-key-2024")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

USERS_DB = {
    "admin": {
        "username": "admin",
        "password": "admin1234",
        "full_name": "Administrator"
    },
    "demo": {
        "username": "demouser",
        "password": "demouser1234",
        "full_name": "Demo User"
    },
    "ubldigital": {
        "username": "ubldigital",
        "password": "ubldigital1234",
        "full_name": "UBL Digital Team"
    }
}

from fastapi.staticfiles import StaticFiles
app.mount("/client", StaticFiles(directory=str(STATIC_DIR), html=True), name="client")

CHANNELS = 1
RATE = 8000

call_metadata: dict[str, dict] = {}


def _record_routing_event(call_id: str | None, event_type: str, payload: dict | None = None) -> None:
    if not call_id:
        return
    if call_id not in call_metadata:
        call_metadata[call_id] = {}
    call_metadata[call_id].setdefault("routing_events", [])
    event = {
        "ts": time.time(),
        "event": event_type,
        "payload": payload or {},
    }
    call_metadata[call_id]["routing_events"].append(event)
    print(f"📍 [ROUTING] call={call_id} event={event_type} payload={json.dumps(event['payload'])}")

@app.get("/", response_class=HTMLResponse)
async def index_page():
    with open(STATIC_DIR / "voice-client.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return html_content

from fastapi import Body

# Convert Gemini voices to UI format
AVAILABLE_VOICES = {
    voice_key: {
        'name': voice_data['name'],
        'age': voice_data['gender'],
        'personality': voice_data['description']
    }
    for voice_key, voice_data in GEMINI_VOICES.items()
}


@app.post("/start-browser-call")
async def start_browser_call(request: Request, payload: dict = Body(...)):
    token = get_token_from_request(request)
    user_data = verify_jwt_token(token)
    
    phone = payload.get("phone", "webclient")
    voice = payload.get("voice", "Charon")  # Default to Charon (Gemini's deep, informative voice)
    temperature = payload.get("temperature", 0.8)
    speed = payload.get("speed", 1.05)
    
    # Validate voice is available in Gemini voices
    if voice not in AVAILABLE_VOICES:
        voice = "Charon"
    
    temperature = max(0.0, min(1.2, float(temperature)))
    speed = max(0.5, min(2.0, float(speed)))
        
    print(f"🎙️ Voice selected: {voice} ({AVAILABLE_VOICES[voice]['name']})")
    print(f"🌡️ Temperature: {temperature}")
    print(f"⚡ Speed: {speed}x")
    
    call_id = await register_call(phone)
    call_id = str(call_id)
    call_recordings[call_id] = {"incoming": [], "outgoing": [], "start_time": time.time()}
    call_metadata[call_id] = {
        "phone": phone, 
        "language_id": payload.get("language_id", 1),
        "voice": voice,
        "temperature": temperature,
        "speed": speed
    }
    await update_call_status(int(call_id), "pick")
    return {
        "call_id": call_id, 
        "voice": voice,
        "temperature": temperature,
        "speed": speed
    }


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    form = await request.form()
    caller_number = form.get("From")
    print("Call is coming from", caller_number)  
    call_id = await register_call(caller_number)
    call_id = str(call_id)
    print("call id received is", call_id, type(call_id))

    call_recordings[call_id] = {"incoming": [], "outgoing": [], "start_time": time.time()}
    
    call_metadata[call_id] = {
        "phone": caller_number, 
        "language_id": 1,
        "voice": "echo",
        "temperature": 0.8,
        "speed": 1.05
    }
    
    response = VoiceResponse()
    response.say("This call may be recorded for quality purposes.", voice='Polly.Danielle-Generative', language='en-US')
    response.pause(length=1)
    host = request.url.hostname

    connect = Connect()
    stream = Stream(url=f"wss://{host}/media-stream")
    stream.parameter(name="call_id", value=call_id)
    connect.append(stream)
    response.append(connect)

    return HTMLResponse(content=str(response), media_type="application/xml")

    

import wave
import audioop
import io
import base64
import websockets as ws_client
from fastapi import WebSocket

USER_AUDIO_DIR = RECORDINGS_DIR / "user"
AGENT_AUDIO_DIR = RECORDINGS_DIR / "agent"
USER_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
AGENT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
import struct
import wave
import struct


last_agent_response_time = None

def generate_silence(duration_sec, sample_rate=8000):
    num_samples = int(duration_sec * sample_rate)
    silence_pcm = b'\x00\x00' * num_samples
    return silence_pcm


async def execute_function_call(func_name: str, func_args: dict, call_id: str | None = None) -> dict:
    try:
        if func_name == WORKFLOW_SELECTOR_TOOL_NAME:
            workflow_id = func_args.get("workflowId", "")
            reason = func_args.get("reason", "")
            if not is_valid_workflow(workflow_id):
                _record_routing_event(call_id, "workflow_selection_invalid", {"workflowId": workflow_id})
                return {
                    "success": False,
                    "error": "Invalid workflow",
                    "message": f"Unknown workflowId '{workflow_id}'. Please choose a valid workflow.",
                }

            if call_id and call_id in call_metadata:
                previous = call_metadata[call_id].get("active_workflow")
                call_metadata[call_id]["active_workflow"] = workflow_id
                call_metadata[call_id]["workflow_selection_reason"] = reason
                call_metadata[call_id]["workflow_phase"] = get_initial_phase_for_workflow(workflow_id)
                if previous and previous != workflow_id:
                    _record_routing_event(
                        call_id,
                        "workflow_reselected",
                        {"from": previous, "to": workflow_id, "reason": reason},
                    )
                else:
                    _record_routing_event(
                        call_id,
                        "workflow_selected",
                        {"workflow": workflow_id, "reason": reason},
                    )

            return {
                "success": True,
                "workflowId": workflow_id,
                "reason": reason,
                "workflowContext": get_workflow_context(workflow_id),
                "phase": get_initial_phase_for_workflow(workflow_id),
                "message": f"Workflow selected: {workflow_id}",
            }

        active_workflow = call_metadata.get(call_id, {}).get("active_workflow") if call_id else None
        if not is_tool_allowed_for_workflow(func_name, active_workflow):
            if call_id and call_id in call_metadata:
                call_metadata[call_id]["blocked_tool_attempts"] = call_metadata[call_id].get("blocked_tool_attempts", 0) + 1
            _record_routing_event(
                call_id,
                "tool_blocked_by_workflow",
                {"tool": func_name, "active_workflow": active_workflow},
            )
            return {
                "success": False,
                "error": "Tool not allowed for active workflow",
                "active_workflow": active_workflow,
                "message": (
                    "This tool is not allowed for the active workflow. "
                    f"Active workflow is '{active_workflow}'. "
                    "Do not request card details or IVR for balance inquiry. "
                    "Continue with the next allowed step for the current workflow."
                ),
            }

        current_phase = call_metadata.get(call_id, {}).get("workflow_phase") if call_id else None
        allowed_in_phase, phase_error = is_tool_allowed_in_phase(
            active_workflow or "",
            current_phase,
            func_name,
        )
        if not allowed_in_phase:
            if call_id and call_id in call_metadata:
                call_metadata[call_id]["blocked_tool_attempts"] = call_metadata[call_id].get("blocked_tool_attempts", 0) + 1
            required_tool = get_required_tool_for_phase(active_workflow or "", current_phase)
            _record_routing_event(
                call_id,
                "tool_blocked_by_phase",
                {
                    "tool": func_name,
                    "active_workflow": active_workflow,
                    "phase": current_phase,
                    "required_tool": required_tool,
                },
            )
            return {
                "success": False,
                "error": "Tool not allowed for current workflow phase",
                "active_workflow": active_workflow,
                "phase": current_phase,
                "required_tool": required_tool,
                "message": phase_error or "This step is out of sequence for the current workflow phase.",
            }

        result: dict
        if func_name == "searchKnowledgeBase":
            result = await search_knowledge_base(query=func_args.get("query", ""))
        
        elif func_name == "verifyCustomerByCnic":
            result = await verify_customer_by_cnic(cnic=func_args.get("cnic", ""))
        
        elif func_name == "confirmPhysicalCustody":
            has_card_str = str(func_args.get("hasCard", "false")).lower()
            has_card = has_card_str in ("true", "yes", "1")
            result = await confirm_physical_custody(
                cnic=func_args.get("cnic", ""),
                has_card=has_card
            )
        
        elif func_name == "verifyTpin":
            result = await verify_tpin(
                cnic=func_args.get("cnic", ""),
                tpin=func_args.get("tpin", "")
            )
            if active_workflow == "balance_inquiry" and result.get("success"):
                result["message"] = (
                    "TPIN verified successfully for balance inquiry. "
                    "Now ask whether the customer wants smart account, digital account, or both accounts. "
                    "Then call getAccountBalance using option number, account name, or both/dono."
                )
        
        elif func_name == "verifyCardDetails":
            result = await verify_card_details(
                cnic=func_args.get("cnic", ""),
                last_four_digits=func_args.get("lastFourDigits", ""),
                expiry_date=func_args.get("expiryDate", "")
            )
        
        elif func_name == "activateCard":
            result = await activate_card(cnic=func_args.get("cnic", ""))
        
        elif func_name == "updateCustomerTpin":
            result = await update_customer_tpin(
                cnic=func_args.get("cnic", ""),
                new_tpin=func_args.get("newTpin", "")
            )
        
        elif func_name == "transferToIvrForPin":
            result = await transfer_to_ivr_for_pin()
        
        elif func_name == "transferToAgent":
            result = await transfer_to_agent(
                cnic=func_args.get("cnic", ""),
                reason=func_args.get("reason", "")
            )
        
        elif func_name == "getCustomerStatus":
            result = await get_customer_status(cnic=func_args.get("cnic", ""))

        elif func_name == "getAccountBalance":
            result = await get_account_balance(
                cnic=func_args.get("cnic", ""),
                account_selector=func_args.get("accountSelector", ""),
            )
        
        else:
            result = {
                "success": False,
                "error": f"Unknown function: {func_name}",
                "message": "Function not found in the system."
            }

        if call_id and call_id in call_metadata and active_workflow:
            previous_phase = call_metadata[call_id].get("workflow_phase")
            next_phase = get_next_phase_for_tool(
                active_workflow,
                previous_phase,
                func_name,
                result,
            )
            if next_phase != previous_phase:
                call_metadata[call_id]["workflow_phase"] = next_phase
                _record_routing_event(
                    call_id,
                    "workflow_phase_advanced",
                    {
                        "workflow": active_workflow,
                        "from": previous_phase,
                        "to": next_phase,
                        "tool": func_name,
                    },
                )

        return result
    
    except Exception as e:
        print(f"❌ Error executing function {func_name}: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "message": f"An error occurred while executing {func_name}."
        }

@app.websocket("/media-stream-browser")
async def media_stream_browser(websocket: WebSocket):
    """
    WebSocket endpoint for browser-based voice calls using Gemini Live API.
    
    Handles:
    - Browser audio streaming (8kHz PCM) -> Gemini (16kHz PCM)
    - Gemini responses (24kHz PCM) -> Browser (8kHz PCM)
    - Function calling for RAG and customer verification
    """
    await websocket.accept()
    
    session_initialized = False
    call_id = None
    stream_sid = None
    gemini_client = None
    
    user_pcm_buffer = io.BytesIO()
    agent_pcm_buffer = io.BytesIO()
    
    function_call_completed_time = None
    FUNCTION_CALL_GRACE_PERIOD = 3.0
    
    _tool_call_received_at = None
    _tool_func_name = None
    _tool_response_sent_at = None
    _first_audio_after_tool = True
    
    try:
        # Wait for the start event with authentication
        start_msg = await websocket.receive_text()
        start_data = json.loads(start_msg)
        
        if start_data.get("event") != "start":
            print("❌ Expected 'start' event as first message")
            await websocket.close(code=1008, reason="Expected start event")
            return
        
        # Authenticate
        token = start_data["start"]["customParameters"].get("token")
        if not token:
            print("❌ No token provided in WebSocket connection")
            await websocket.close(code=1008, reason="Authentication required")
            return
        
        try:
            user_data = verify_jwt_token(token)
            print(f"✅ WebSocket authenticated for user: {user_data['username']}")
        except HTTPException as e:
            print(f"❌ Invalid token in WebSocket: {e.detail}")
            await websocket.close(code=1008, reason="Invalid or expired token")
            return
        
        call_id = start_data["start"]["customParameters"].get("call_id")
        stream_sid = start_data["start"].get("streamSid", "browser-stream")
        meta = call_metadata.get(call_id, {})
        
        # Build Gemini configuration
        instructions = meta.get("instructions", "")
        caller = meta.get("phone", "")
        gemini_voice = meta.get("voice", "Charon")  # Now using Gemini voice names directly
        temperature = meta.get("temperature", 0.8)
        
        workflow_context = get_workflow_policy_context()
        call_metadata.setdefault(call_id, {})
        call_metadata[call_id]["active_workflow"] = None
        call_metadata[call_id]["workflow_phase"] = None
        call_metadata[call_id]["workflow_selection_reason"] = ""
        call_metadata[call_id]["routing_events"] = []
        call_metadata[call_id]["blocked_tool_attempts"] = 0
        all_tools = get_all_tools_with_selector()

        SYSTEM_MESSAGE = build_system_message(
            instructions=instructions,
            caller=caller,
            voice=gemini_voice,
            workflow_context=workflow_context
        )
        
        print(
            f"🔧 Initializing Gemini session with voice: {gemini_voice}, temp: {temperature}, "
            f"workflow: dynamic-selection, tools: {len(all_tools)}"
        )
        
        config = GeminiLiveConfig(
            system_instruction=SYSTEM_MESSAGE,
            tools=all_tools,
            voice=gemini_voice,
            temperature=temperature
        )
        
        # Connect to Gemini Live API
        gemini_client = GeminiLiveClient(config)
        
        # Reset audio conversion states for clean session
        reset_audio_states()
        
        await gemini_client.connect()
        session_initialized = True
        
        # Trigger initial greeting - send text to make agent speak first
        print("🎤 Triggering initial greeting...")
        await gemini_client.send_text("Start the conversation by greeting the customer warmly.")
        
        async def receive_from_browser():
            """Receive audio from browser and send to Gemini."""
            nonlocal session_initialized
            try:
                async for msg in websocket.iter_text():
                    try:
                        data = json.loads(msg)
                        
                        if data.get("event") == "media" and session_initialized:
                            # Browser now sends 16kHz PCM (Gemini's native input format)
                            payload_b64 = data["media"]["payload"]
                            pcm_data = base64.b64decode(payload_b64)
                            user_pcm_buffer.write(pcm_data)
                            
                            # Passthrough to Gemini (16kHz -> 16kHz, no conversion needed)
                            # This eliminates resampling overhead for lower latency
                            pcm_16khz = convert_browser_to_gemini(pcm_data, input_rate=16000)
                            
                            # Send to Gemini immediately
                            await gemini_client.send_audio(pcm_16khz)
                        
                        elif data.get("event") == "stop":
                            print(f"🛑 Browser sent stop event for call {call_id}")
                            break
                    
                    except json.JSONDecodeError as je:
                        print(f"⚠️ Failed to parse browser message: {je}")
                        continue
                    except Exception as inner_e:
                        print(f"⚠️ Error processing browser message: {inner_e}")
                        traceback.print_exc()
                        continue
                
                print(f"🔚 Browser WebSocket stream ended normally for call {call_id}")
                
            except WebSocketDisconnect:
                print(f"🔌 Browser WebSocket disconnected for call {call_id}")
            except Exception as e:
                print(f"❌ Unexpected error in browser receive loop: {e}")
                traceback.print_exc()
        
        async def receive_from_gemini_and_forward():
            """Receive responses from Gemini and forward to browser."""
            nonlocal function_call_completed_time
            nonlocal _tool_call_received_at, _tool_func_name, _tool_response_sent_at, _first_audio_after_tool
            
            try:
                async for response in gemini_client.receive():
                    try:
                        if response.type == 'audio':
                            if function_call_completed_time is not None:
                                function_call_completed_time = None

                            if _tool_response_sent_at and _first_audio_after_tool:
                                _first_audio_after_tool = False
                                first_audio_delay = (time.time() - _tool_response_sent_at) * 1000
                                total_delay = (time.time() - _tool_call_received_at) * 1000
                                print(f"⏱️ [GEMINI TIMING] {_tool_func_name} | first_audio_after_tool_response: {first_audio_delay:.0f}ms | total_tool_to_audio: {total_delay:.0f}ms")
                            
                            pcm_24khz = response.audio_data
                            agent_pcm_buffer.write(pcm_24khz)
                            
                            pcm_b64 = base64.b64encode(pcm_24khz).decode('utf-8')
                            out = {
                                "event": "media",
                                "media": {
                                    "payload": pcm_b64,
                                    "format": "raw_pcm",
                                    "sampleRate": 24000,
                                    "channels": 1,
                                    "bitDepth": 16
                                }
                            }
                            await websocket.send_json(out)
                        
                        elif response.type == 'tool_call':
                            for tool_call in response.tool_calls:
                                func_name = tool_call.get("name")
                                func_id = tool_call.get("id")
                                func_args = tool_call.get("arguments", {})
                                
                                _tool_call_received_at = time.time()
                                _tool_func_name = func_name
                                _first_audio_after_tool = True
                                
                                print(f"🔧 Function call: {func_name} with args: {func_args}")
                                
                                exec_start = time.time()
                                try:
                                    result = await asyncio.wait_for(
                                        execute_function_call(func_name, func_args, call_id=call_id),
                                        timeout=30.0
                                    )
                                except asyncio.TimeoutError:
                                    print(f"⚠️ Function call {func_name} timed out after 30 seconds")
                                    result = {
                                        "success": False,
                                        "error": "timeout",
                                        "message": f"The operation timed out. Please try again."
                                    }
                                exec_ms = (time.time() - exec_start) * 1000
                                
                                print(f"✅ Function result: {result}")
                                
                                send_start = time.time()
                                await gemini_client.send_tool_response([{
                                    "id": func_id,
                                    "name": func_name,
                                    "response": result
                                }])
                                _tool_response_sent_at = time.time()
                                send_ms = (_tool_response_sent_at - send_start) * 1000
                                
                                function_call_completed_time = _tool_response_sent_at
                                print(f"⏱️ [GEMINI TIMING] {func_name} | exec: {exec_ms:.0f}ms | send_tool_response: {send_ms:.0f}ms | waiting for audio...")
                                
                                outgoing_func_result = {
                                    "event": "function_result",
                                    "name": func_name,
                                    "arguments": json.dumps(func_args),
                                    "result": result
                                }
                                await websocket.send_json(outgoing_func_result)
                        
                        elif response.type == 'interrupted':
                            current_time = time.time()
                            if function_call_completed_time is not None:
                                time_since_function_call = current_time - function_call_completed_time
                                if time_since_function_call < FUNCTION_CALL_GRACE_PERIOD:
                                    print(f"⚠️ Ignoring interruption {time_since_function_call:.2f}s after function call")
                                    continue
                            
                            await websocket.send_json({"event": "clear"})
                        
                        elif response.type == 'turn_complete':
                            if _tool_call_received_at:
                                turn_total = (time.time() - _tool_call_received_at) * 1000
                                print(f"⏱️ [GEMINI TIMING] {_tool_func_name} | turn_complete total: {turn_total:.0f}ms")
                                _tool_call_received_at = None
                                _tool_response_sent_at = None
                            print(f"📋 Gemini turn complete")
                            if function_call_completed_time is not None:
                                print(f"✅ Response completed, clearing function call flag")
                                function_call_completed_time = None
                        
                        elif response.type == 'input_transcription':
                            print(f"🎤 User said: {response.transcription}")
                        
                        elif response.type == 'output_transcription':
                            print(f"🔊 Agent said: {response.transcription}")
                        
                        elif response.type == 'tool_call_cancelled':
                            print(f"⚠️ Tool calls cancelled")
                            _tool_call_received_at = None
                            _tool_response_sent_at = None
                            continue
                    
                    except Exception as inner_e:
                        print(f"⚠️ Error processing Gemini message: {inner_e}")
                        traceback.print_exc()
                        continue
            
            except Exception as e:
                print(f"❌ Unexpected error in Gemini receive loop: {e}")
                traceback.print_exc()
                try:
                    await websocket.send_json({
                        "event": "error",
                        "message": "An unexpected error occurred. Please try again."
                    })
                except:
                    pass
        
        # Run both tasks concurrently
        recv_task = asyncio.create_task(receive_from_browser())
        send_task = asyncio.create_task(receive_from_gemini_and_forward())
        
        try:
            done, pending = await asyncio.wait(
                [recv_task, send_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in done:
                if task == recv_task:
                    print(f"🔚 Browser receive task completed for call {call_id}")
                elif task == send_task:
                    print(f"🔚 Gemini send task completed for call {call_id}")
                
                if task.exception():
                    print(f"❌ Task exception: {task.exception()}")
            
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        
        except Exception as e:
            print(f"❌ Error in main task loop: {e}")
            traceback.print_exc()
    
    except Exception as e:
        print(f"❌ Error during WebSocket setup: {e}")
        traceback.print_exc()
    
    finally:
        # Close Gemini connection
        if gemini_client:
            await gemini_client.close()
        
        # Save recordings
        if call_id:
            print(f"💾 Saving recordings for call {call_id}...")
            
            user_file_path = str(USER_AUDIO_DIR / f"{call_id}_user.wav")
            agent_file_path = str(AGENT_AUDIO_DIR / f"{call_id}_agent.wav")
            
            def save_wav_file(path: str, pcm_data: bytes, sample_rate: int = 8000):
                with wave.open(path, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(pcm_data)
            
            # User audio at 16kHz (browser mic rate), agent at 24kHz (Gemini output rate)
            save_wav_file(user_file_path, user_pcm_buffer.getvalue(), sample_rate=16000)
            save_wav_file(agent_file_path, agent_pcm_buffer.getvalue(), sample_rate=24000)
            
            print(f"✅ Saved user audio: {user_file_path}")
            print(f"✅ Saved agent audio: {agent_file_path}")
            
            try:
                user_transcript = await transcribe_audio(user_file_path)
            except Exception as e:
                print(f"⚠️ Could not transcribe user audio: {e}")
                user_transcript = ""
            
            try:
                agent_transcript = await transcribe_audio(agent_file_path)
            except Exception as e:
                print(f"⚠️ Could not transcribe agent audio: {e}")
                agent_transcript = ""
            
            transcripts_output = {
                "call_id": call_id,
                "user_transcript": user_transcript,
                "agent_transcript": agent_transcript
            }
            
            print(f"📝 Transcripts saved for call {call_id}")
            
            analysis_result = await analyze_call_with_llm(call_id, user_transcript, agent_transcript)
            print(f"📊 Call analysis complete: {analysis_result}")
            
            with open(RECORDINGS_DIR / f"{call_id}_transcript.json", "w", encoding="utf-8") as f:
                json.dump(transcripts_output, f, ensure_ascii=False, indent=2)
        
        try:
            await websocket.close()
        except:
            pass



@app.get("/call-analysis/{call_id}")
async def get_call_analysis(call_id: str, request: Request):
    token = get_token_from_request(request)
    user_data = verify_jwt_token(token)
    
    analysis_file_path = RECORDINGS_DIR / "analysis" / f"{call_id}_analysis.json"

    if not analysis_file_path.exists():
        raise HTTPException(status_code=404, detail=f"Analysis not found for call_id: {call_id}")
    
    try:
        with open(analysis_file_path, "r", encoding="utf-8") as f:
            analysis_data = json.load(f)
        return analysis_data
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Error reading analysis file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving analysis: {str(e)}")


@app.get("/call-analysis/{call_id}/download")
async def download_call_analysis(call_id: str, request: Request):
    token = get_token_from_request(request)
    verify_jwt_token(token)
    analysis_file_path = RECORDINGS_DIR / "analysis" / f"{call_id}_analysis.json"
    if not analysis_file_path.exists():
        raise HTTPException(status_code=404, detail=f"Analysis not found for call_id: {call_id}")
    return FileResponse(
        str(analysis_file_path),
        media_type="application/json",
        filename=f"{call_id}_analysis.json",
    )


@app.get("/call-transcript/{call_id}/download")
async def download_call_transcript(call_id: str, request: Request):
    token = get_token_from_request(request)
    verify_jwt_token(token)
    transcript_path = RECORDINGS_DIR / f"{call_id}_transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=404, detail=f"Transcript not found for call_id: {call_id}")
    return FileResponse(
        str(transcript_path),
        media_type="application/json",
        filename=f"{call_id}_transcript.json",
    )


@app.get("/available-voices")
async def get_available_voices(request: Request):
    token = get_token_from_request(request)
    user_data = verify_jwt_token(token)
    
    return {
        "voices": AVAILABLE_VOICES
    }


def create_jwt_token(username: str, full_name: str) -> str:
    now = dt.now(timezone.utc)
    payload = {
        "username": username,
        "full_name": full_name,
        "exp": now + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": now
    }
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return token


def verify_jwt_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_token_from_request(request: Request) -> str:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    return auth_header.replace("Bearer ", "")


@app.post("/auth/login")
async def login(credentials: dict = Body(...)):
    username = credentials.get("username", "").strip()
    password = credentials.get("password", "")
    
    if username in USERS_DB:
        user = USERS_DB[username]
        if user["password"] == password:
            token = create_jwt_token(username, user["full_name"])
            
            return {
                "success": True,
                "message": "Login successful",
                "token": token,
                "user": {
                    "username": username,
                    "full_name": user["full_name"]
                }
            }
    
    raise HTTPException(status_code=401, detail="Invalid username or password")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=PORT)
