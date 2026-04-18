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
    get_smart_initial_phase,
    advance_phase_skipping_verified,
    build_verification_context,
)
from backend.services.rag_tools import search_knowledge_base, prewarm_embeddings
from backend.services.audio_transcription import transcribe_audio, analyze_call_with_llm
from backend.services.speaker_verification import (
    SpeakerVerifier,
    SpeakerCheckResult,
    warm_encoder as warm_speaker_encoder,
)
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


class TokenTracker:
    """
    Estimates token usage per turn and cumulative for a Gemini Live API call.

    Gemini Live API does not expose usage_metadata, so we estimate from
    observable data: audio duration, transcription text, tool payloads,
    and the system prompt.

    Rates (from Google docs / empirical):
      - Audio input:  ~25 tokens/sec at 16 kHz mono 16-bit PCM
      - Audio output: ~25 tokens/sec at 24 kHz mono 16-bit PCM
      - Text:         ~1 token per 4 characters (mixed EN/UR average)
      - JSON payload: ~1 token per 4 characters
    """

    AUDIO_INPUT_TOKENS_PER_SEC = 25      # 16 kHz, 16-bit mono
    AUDIO_OUTPUT_TOKENS_PER_SEC = 25     # 24 kHz, 16-bit mono
    CHARS_PER_TOKEN = 4
    CONTEXT_WINDOW = 8192                # matches sliding_window target_tokens

    def __init__(self, call_id: str, system_prompt: str, tools_json: list):
        self.call_id = call_id
        self.turn_number = 0

        # One-time system cost
        tools_text = json.dumps(tools_json)
        self.system_prompt_tokens = len(system_prompt) // self.CHARS_PER_TOKEN
        self.tools_tokens = len(tools_text) // self.CHARS_PER_TOKEN
        self.base_tokens = self.system_prompt_tokens + self.tools_tokens

        # Per-turn accumulators (reset each turn)
        self._turn_input_audio_bytes = 0
        self._turn_output_audio_bytes = 0
        self._turn_input_text = ""
        self._turn_output_text = ""
        self._turn_tool_calls: list[dict] = []

        # Cumulative totals
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_tool_tokens = 0
        self.total_turns = 0

        # Per-turn history for post-call dump
        self.turn_history: list[dict] = []

        print(
            f"🔢 [TOKENS] call={call_id} session_init | "
            f"system_prompt: ~{self.system_prompt_tokens} | "
            f"tools: ~{self.tools_tokens} | "
            f"base_context: ~{self.base_tokens} / {self.CONTEXT_WINDOW}"
        )

    # -- Accumulate events within a turn --

    def add_input_audio(self, pcm_bytes: int) -> None:
        self._turn_input_audio_bytes += pcm_bytes

    def add_output_audio(self, pcm_bytes: int) -> None:
        self._turn_output_audio_bytes += pcm_bytes

    def set_input_transcription(self, text: str) -> None:
        self._turn_input_text = text

    def set_output_transcription(self, text: str) -> None:
        self._turn_output_text = text

    def add_tool_call(self, func_name: str, args: dict, result: dict) -> None:
        args_text = json.dumps(args)
        result_text = json.dumps(result)
        tokens = (len(args_text) + len(result_text)) // self.CHARS_PER_TOKEN
        self._turn_tool_calls.append({
            "function": func_name,
            "args_tokens": len(args_text) // self.CHARS_PER_TOKEN,
            "result_tokens": len(result_text) // self.CHARS_PER_TOKEN,
            "total_tokens": tokens,
        })

    # -- Audio duration helpers --

    def _audio_seconds(self, pcm_bytes: int, sample_rate: int) -> float:
        # 16-bit mono = 2 bytes per sample
        return pcm_bytes / (sample_rate * 2) if pcm_bytes else 0.0

    # -- Finalize a turn --

    def finalize_turn(self) -> dict:
        self.turn_number += 1
        self.total_turns += 1

        input_audio_sec = self._audio_seconds(self._turn_input_audio_bytes, 16000)
        output_audio_sec = self._audio_seconds(self._turn_output_audio_bytes, 24000)

        input_audio_tokens = int(input_audio_sec * self.AUDIO_INPUT_TOKENS_PER_SEC)
        output_audio_tokens = int(output_audio_sec * self.AUDIO_OUTPUT_TOKENS_PER_SEC)
        input_text_tokens = len(self._turn_input_text) // self.CHARS_PER_TOKEN
        output_text_tokens = len(self._turn_output_text) // self.CHARS_PER_TOKEN

        turn_input = input_audio_tokens + input_text_tokens
        turn_output = output_audio_tokens + output_text_tokens
        turn_tool = sum(tc["total_tokens"] for tc in self._turn_tool_calls)

        self.total_input_tokens += turn_input
        self.total_output_tokens += turn_output
        self.total_tool_tokens += turn_tool

        cumulative = self.base_tokens + self.total_input_tokens + self.total_output_tokens + self.total_tool_tokens
        utilization = (cumulative / self.CONTEXT_WINDOW * 100) if self.CONTEXT_WINDOW else 0

        turn_data = {
            "turn": self.turn_number,
            "input": {
                "audio_sec": round(input_audio_sec, 2),
                "audio_tokens": input_audio_tokens,
                "text_tokens": input_text_tokens,
                "total": turn_input,
            },
            "output": {
                "audio_sec": round(output_audio_sec, 2),
                "audio_tokens": output_audio_tokens,
                "text_tokens": output_text_tokens,
                "total": turn_output,
            },
            "tool_calls": self._turn_tool_calls.copy(),
            "tool_tokens": turn_tool,
            "turn_total": turn_input + turn_output + turn_tool,
            "cumulative": {
                "input": self.total_input_tokens,
                "output": self.total_output_tokens,
                "tools": self.total_tool_tokens,
                "base": self.base_tokens,
                "total": cumulative,
                "context_window": self.CONTEXT_WINDOW,
                "utilization_pct": round(utilization, 1),
            },
        }

        self.turn_history.append(turn_data)

        # Build tool line
        tool_line = ""
        if self._turn_tool_calls:
            tool_names = ", ".join(tc["function"] for tc in self._turn_tool_calls)
            tool_line = f"\n   tool: ~{turn_tool} tokens ({tool_names})"

        print(
            f"🔢 [TOKENS] call={self.call_id} turn={self.turn_number}\n"
            f"   input: ~{turn_input} tokens (audio: ~{input_audio_tokens} [{input_audio_sec:.1f}s], text: ~{input_text_tokens})\n"
            f"   output: ~{turn_output} tokens (audio: ~{output_audio_tokens} [{output_audio_sec:.1f}s], text: ~{output_text_tokens})"
            f"{tool_line}\n"
            f"   turn_total: ~{turn_data['turn_total']} | cumulative: ~{cumulative} / {self.CONTEXT_WINDOW} ({utilization:.0f}%)"
        )

        # Reset per-turn accumulators
        self._turn_input_audio_bytes = 0
        self._turn_output_audio_bytes = 0
        self._turn_input_text = ""
        self._turn_output_text = ""
        self._turn_tool_calls = []

        return turn_data

    def get_summary(self) -> dict:
        cumulative = self.base_tokens + self.total_input_tokens + self.total_output_tokens + self.total_tool_tokens
        return {
            "call_id": self.call_id,
            "total_turns": self.total_turns,
            "base_tokens": self.base_tokens,
            "system_prompt_tokens": self.system_prompt_tokens,
            "tools_tokens": self.tools_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tool_tokens": self.total_tool_tokens,
            "total_tokens": cumulative,
            "context_window": self.CONTEXT_WINDOW,
            "utilization_pct": round(cumulative / self.CONTEXT_WINDOW * 100, 1) if self.CONTEXT_WINDOW else 0,
            "turn_history": self.turn_history,
        }

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
    asyncio.create_task(warm_speaker_encoder())


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


async def _handle_speaker_mismatch(
    websocket: WebSocket,
    gemini_client,
    call_id: str | None,
    result: SpeakerCheckResult,
) -> None:
    """Flag the call, notify the frontend, and close the WebSocket with
    code 4001 when the primary speaker has changed mid-call."""
    similarity = result.similarity
    print(
        f"🚨 [SECURITY] Primary speaker change detected for call {call_id} "
        f"(similarity={similarity:.3f})"
    )
    if call_id:
        call_metadata.setdefault(call_id, {})
        call_metadata[call_id]["speaker_mismatch_detected"] = True
        call_metadata[call_id]["speaker_similarity_at_mismatch"] = similarity
    _record_routing_event(
        call_id,
        "speaker_changed",
        {"similarity": similarity, "threshold_breached": True},
    )

    try:
        await websocket.send_json({
            "event": "speaker_changed",
            "message": "Primary speaker change detected. Ending call.",
            "similarity": similarity,
            "severity": "critical",
        })
    except Exception as e:
        print(f"⚠️ Failed to send speaker_changed event: {e}")

    if gemini_client is not None:
        try:
            await gemini_client.close()
        except Exception as e:
            print(f"⚠️ Failed to close Gemini session cleanly: {e}")

    try:
        await websocket.close(code=4001, reason="speaker_changed")
    except Exception:
        pass


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


def _init_conversation_state(call_id: str) -> None:
    call_metadata.setdefault(call_id, {})
    call_metadata[call_id].setdefault("conversation_memory", [])
    call_metadata[call_id].setdefault("question_queue", [])
    call_metadata[call_id].setdefault("answered_questions", [])
    call_metadata[call_id].setdefault("conversation_summary", "")
    call_metadata[call_id].setdefault("call_verifications", {
        "cnic_verified": False,
        "verified_cnic": None,
        "tpin_verified": False,
        "physical_custody_confirmed": False,
        "card_details_verified": False,
        "card_activated": False,
    })
    call_metadata[call_id].setdefault("speaker_mismatch_detected", False)
    call_metadata[call_id].setdefault("speaker_similarity_at_mismatch", None)


def _log_conversation_state(call_id: str, operation: str) -> None:
    state = call_metadata.get(call_id, {})
    pending = state.get("question_queue", [])
    answered = state.get("answered_questions", [])
    topics = state.get("conversation_memory", [])
    summary = state.get("conversation_summary", "")
    print(f"📝 [CONV STATE] call={call_id} op={operation}")
    print(f"   pending_questions ({len(pending)}): {[q.get('question', '') for q in pending]}")
    print(f"   answered_questions ({len(answered)}): {[q.get('question', '') for q in answered]}")
    print(f"   topics ({len(topics)}): {topics}")
    print(f"   summary: {summary[:120]}{'...' if len(summary) > 120 else ''}")


def _is_duplicate_question(existing_questions: list, new_question: str) -> bool:
    new_lower = new_question.lower()
    new_words = set(new_lower.split())
    for item in existing_questions:
        existing_lower = str(item.get("question", "")).strip().lower()
        if existing_lower == new_lower:
            return True
        if existing_lower in new_lower or new_lower in existing_lower:
            return True
        existing_words = set(existing_lower.split())
        if not new_words or not existing_words:
            continue
        overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
        if overlap >= 0.7:
            return True
    return False


def _fuzzy_match_question(pending_text: str, answered_text: str) -> bool:
    p = pending_text.lower()
    a = answered_text.lower()
    if p == a:
        return True
    if p in a or a in p:
        return True
    p_words = set(p.split())
    a_words = set(a.split())
    if not p_words or not a_words:
        return False
    overlap = len(p_words & a_words) / max(len(p_words), len(a_words))
    return overlap >= 0.6


def _update_conversation_state(call_id: str, operation: str, payload: dict) -> dict:
    _init_conversation_state(call_id)
    state = call_metadata[call_id]
    payload = payload or {}

    if operation == "get_state":
        _log_conversation_state(call_id, "get_state")
        return {"success": True, "message": "Current conversation state retrieved."}

    if operation == "add_pending_questions":
        questions = payload.get("questions", [])
        if not isinstance(questions, list):
            return {"success": False, "error": "Invalid payload", "message": "questions must be a list."}
        added = []
        skipped = []
        for q in questions:
            if isinstance(q, str) and q.strip():
                q_clean = q.strip()
                if _is_duplicate_question(state["question_queue"], q_clean):
                    skipped.append(q_clean)
                else:
                    state["question_queue"].append({"question": q_clean, "status": "pending"})
                    added.append(q_clean)
        _log_conversation_state(call_id, "add_pending_questions")
        return {
            "success": True,
            "added": added,
            "skipped_duplicates": skipped,
            "message": f"Added {len(added)} question(s), skipped {len(skipped)} duplicate(s).",
        }

    if operation == "mark_answered":
        answered = payload.get("answered_questions", [])
        if not isinstance(answered, list):
            return {"success": False, "error": "Invalid payload", "message": "answered_questions must be a list."}
        matched = []
        remaining = []
        for item in state["question_queue"]:
            q_text = str(item.get("question", "")).strip()
            found = False
            for a in answered:
                if isinstance(a, str) and a.strip() and _fuzzy_match_question(q_text, a.strip()):
                    state["answered_questions"].append({"question": q_text, "status": "answered"})
                    matched.append(q_text)
                    found = True
                    break
            if not found:
                remaining.append(item)
        state["question_queue"] = remaining
        _log_conversation_state(call_id, "mark_answered")
        return {
            "success": True,
            "matched": matched,
            "still_pending": [q.get("question", "") for q in remaining],
            "message": f"Marked {len(matched)} question(s) as answered. {len(remaining)} still pending.",
        }

    if operation == "set_summary":
        summary = str(payload.get("summary", "")).strip()
        topics = payload.get("topics_discussed", [])
        state["conversation_summary"] = summary
        if isinstance(topics, list):
            existing_lower = {t.lower() for t in state["conversation_memory"]}
            for topic in topics:
                if isinstance(topic, str) and topic.strip() and topic.strip().lower() not in existing_lower:
                    state["conversation_memory"].append(topic.strip())
                    existing_lower.add(topic.strip().lower())
        _log_conversation_state(call_id, "set_summary")
        return {"success": True, "message": "Conversation summary updated."}

    return {"success": False, "error": "Unknown operation", "message": f"Unsupported operation: {operation}"}

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
    _init_conversation_state(call_id)
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
    _init_conversation_state(call_id)
    
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

                call_vers = call_metadata[call_id].get("call_verifications", {})
                smart_phase, skipped = get_smart_initial_phase(workflow_id, call_vers)
                call_metadata[call_id]["workflow_phase"] = smart_phase

                if skipped:
                    _record_routing_event(
                        call_id,
                        "phases_skipped_already_verified",
                        {"workflow": workflow_id, "skipped": skipped, "starting_at": smart_phase},
                    )
                    print(
                        f"⏭️ [PHASE SKIP] call={call_id} workflow={workflow_id} "
                        f"skipped={skipped} starting_at={smart_phase}"
                    )

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

            verification_context = build_verification_context(
                call_metadata.get(call_id, {}).get("call_verifications", {})
            ) if call_id else ""

            effective_phase = smart_phase if call_id else get_initial_phase_for_workflow(workflow_id)
            skipped_list = skipped if call_id else []

            if skipped_list and verification_context:
                message = (
                    f"Workflow selected: {workflow_id}. "
                    f"Phases {skipped_list} were already completed — start directly from phase '{effective_phase}'. "
                    f"Do NOT re-ask the customer for any previously verified information. "
                    f"IMPORTANT: You already spoke to the customer before this tool call. "
                    f"Do NOT repeat what you just said. Continue naturally from where you left off."
                )
            else:
                message = (
                    f"Workflow selected: {workflow_id}. "
                    f"IMPORTANT: You already spoke to the customer before this tool call. "
                    f"Do NOT repeat what you just said. Continue naturally from where you left off."
                )

            return {
                "success": True,
                "workflowId": workflow_id,
                "reason": reason,
                "workflowContext": get_workflow_context(workflow_id),
                "phase": effective_phase,
                "skipped_phases": skipped_list,
                "verification_status": verification_context,
                "message": message,
            }

        if func_name == "updateConversationState":
            if not call_id:
                return {
                    "success": False,
                    "error": "Missing call_id",
                    "message": "Cannot update conversation state without call context.",
                }
            operation = str(func_args.get("operation", "")).strip()
            payload = func_args.get("payload", {})
            update_result = _update_conversation_state(call_id, operation, payload)
            state = call_metadata.get(call_id, {})
            return {
                **update_result,
                "state": {
                    "conversation_summary": state.get("conversation_summary", ""),
                    "topics_discussed": state.get("conversation_memory", []),
                    "pending_questions": state.get("question_queue", []),
                    "answered_questions": state.get("answered_questions", []),
                },
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
                    f"This tool is not allowed for the active workflow '{active_workflow}'. "
                    "If the customer's request has changed, call selectWorkflow first to switch "
                    "to the correct workflow, then retry the tool. "
                    "Previously verified information (CNIC, TPIN, etc.) will be preserved across workflows."
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
            if result.get("success") and call_id and call_id in call_metadata:
                vers = call_metadata[call_id].setdefault("call_verifications", {})
                vers["cnic_verified"] = True
                vers["verified_cnic"] = func_args.get("cnic", "")
                print(f"✅ [VERIFY STATE] call={call_id} cnic_verified=True cnic={vers['verified_cnic']}")

        elif func_name == "confirmPhysicalCustody":
            has_card_str = str(func_args.get("hasCard", "false")).lower()
            has_card = has_card_str in ("true", "yes", "1")
            result = await confirm_physical_custody(
                cnic=func_args.get("cnic", ""),
                has_card=has_card
            )
            if result.get("success") and call_id and call_id in call_metadata:
                vers = call_metadata[call_id].setdefault("call_verifications", {})
                vers["physical_custody_confirmed"] = True
                print(f"✅ [VERIFY STATE] call={call_id} physical_custody_confirmed=True")

        elif func_name == "verifyTpin":
            result = await verify_tpin(
                cnic=func_args.get("cnic", ""),
                tpin=func_args.get("tpin", "")
            )
            if result.get("success"):
                if call_id and call_id in call_metadata:
                    vers = call_metadata[call_id].setdefault("call_verifications", {})
                    vers["tpin_verified"] = True
                    print(f"✅ [VERIFY STATE] call={call_id} tpin_verified=True")
                if active_workflow == "balance_inquiry":
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
            if result.get("success") and call_id and call_id in call_metadata:
                vers = call_metadata[call_id].setdefault("call_verifications", {})
                vers["card_details_verified"] = True
                print(f"✅ [VERIFY STATE] call={call_id} card_details_verified=True")

        elif func_name == "activateCard":
            result = await activate_card(cnic=func_args.get("cnic", ""))
            if result.get("success") and call_id and call_id in call_metadata:
                vers = call_metadata[call_id].setdefault("call_verifications", {})
                vers["card_activated"] = True
                print(f"✅ [VERIFY STATE] call={call_id} card_activated=True")
        
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
                # Check if we can skip further ahead past already-verified phases
                call_vers = call_metadata[call_id].get("call_verifications", {})
                final_phase = advance_phase_skipping_verified(
                    active_workflow, next_phase, call_vers
                )
                # If we skipped additional phases beyond the normal advance,
                # override the result message so Gemini knows the actual next step
                if final_phase != next_phase:
                    skipped_names = []
                    walk = next_phase
                    phase_map = None
                    if active_workflow == "card_activation":
                        from backend.workflow.registry import CARD_ACTIVATION_PHASES as _pm
                        phase_map = _pm
                    elif active_workflow == "balance_inquiry":
                        from backend.workflow.registry import BALANCE_INQUIRY_PHASES as _pm
                        phase_map = _pm
                    if phase_map:
                        while walk and walk != final_phase:
                            skipped_names.append(walk)
                            p = phase_map.get(walk)
                            walk = p.next_phase if p else None

                    next_tool = get_required_tool_for_phase(active_workflow, final_phase)
                    result["message"] = (
                        f"{result.get('message', '')} "
                        f"NOTE: Phases {skipped_names} were already verified earlier in this call and have been skipped. "
                        f"Current phase is now '{final_phase}'. "
                        f"Proceed directly with '{next_tool or 'the next step'}'. "
                        f"Do NOT ask the customer for previously verified information."
                    )
                    _record_routing_event(
                        call_id,
                        "phases_auto_skipped",
                        {
                            "workflow": active_workflow,
                            "normal_next": next_phase,
                            "skipped_to": final_phase,
                        },
                    )
                    print(
                        f"⏭️ [PHASE SKIP] call={call_id} after {func_name}: "
                        f"{previous_phase} → {next_phase} → skipped to {final_phase}"
                    )
                call_metadata[call_id]["workflow_phase"] = final_phase
                _record_routing_event(
                    call_id,
                    "workflow_phase_advanced",
                    {
                        "workflow": active_workflow,
                        "from": previous_phase,
                        "to": final_phase,
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
    cleanup_done = False

    user_pcm_buffer = io.BytesIO()
    agent_pcm_buffer = io.BytesIO()

    speaker_verifier: SpeakerVerifier | None = None

    function_call_completed_time = None
    FUNCTION_CALL_GRACE_PERIOD = 3.0

    _tool_call_received_at = None
    _tool_func_name = None
    _tool_response_sent_at = None
    _first_audio_after_tool = True
    _audio_sent_before_tool = False  # True if audio was already forwarded before a tool call in this turn
    _suppress_post_tool_audio = False  # True = drop all post-tool audio until turn_complete
    _turn_audio_bytes = 0  # Track audio bytes sent to browser in current turn

    token_tracker: TokenTracker | None = None
    
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
        speaker_verifier = SpeakerVerifier(call_id=call_id)
        print(f"🔒 [SECURITY] SpeakerVerifier initialized for call {call_id}")
        meta = call_metadata.get(call_id, {})
        
        # Build Gemini configuration
        instructions = meta.get("instructions", "")
        caller = meta.get("phone", "")
        gemini_voice = meta.get("voice", "Charon")  # Now using Gemini voice names directly
        temperature = meta.get("temperature", 0.8)
        
        workflow_context = get_workflow_policy_context()
        call_metadata.setdefault(call_id, {})
        _init_conversation_state(call_id)
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
        
        token_tracker = TokenTracker(call_id, SYSTEM_MESSAGE, all_tools)

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

                            if token_tracker:
                                token_tracker.add_input_audio(len(pcm_data))

                            # Passthrough to Gemini (16kHz -> 16kHz, no conversion needed)
                            # This eliminates resampling overhead for lower latency
                            pcm_16khz = convert_browser_to_gemini(pcm_data, input_rate=16000)

                            # Send to Gemini immediately
                            await gemini_client.send_audio(pcm_16khz)

                            # Append to speaker-verification buffer (cheap — just
                            # a numpy concat). The actual embedding + comparison
                            # runs in the separate speaker_monitor task so it
                            # never blocks audio forwarding to Gemini.
                            try:
                                speaker_verifier.add_audio(pcm_data)
                            except Exception as sv_err:
                                print(f"❌ [SECURITY] add_audio error: {sv_err}")
                        
                        elif data.get("event") == "stop":
                            print(f"🛑 Browser sent stop event for call {call_id}")
                            break
                    
                    except json.JSONDecodeError as je:
                        print(f"⚠️ Failed to parse browser message: {je}")
                        continue
                    except Exception as inner_e:
                        err_str = str(inner_e).lower()
                        if "closed" in err_str or "1011" in err_str or not gemini_client.is_connected:
                            print(f"🔌 Gemini connection lost, stopping browser receive loop for call {call_id}")
                            break
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
            nonlocal _tool_call_received_at, _tool_func_name, _tool_response_sent_at, _first_audio_after_tool, _audio_sent_before_tool, _suppress_post_tool_audio, _turn_audio_bytes
            
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
                                # If audio was already sent before the tool call,
                                # suppress ALL post-tool audio to avoid duplicate speech
                                if _audio_sent_before_tool:
                                    _suppress_post_tool_audio = True
                                    print(f"🔇 Suppressing post-tool audio — already spoke before tool call")

                            pcm_24khz = response.audio_data
                            agent_pcm_buffer.write(pcm_24khz)

                            if token_tracker:
                                token_tracker.add_output_audio(len(pcm_24khz))

                            # Track that audio was sent in this turn (before any tool call)
                            if not _tool_call_received_at:
                                _audio_sent_before_tool = True

                            # Drop audio chunks when suppressing post-tool duplicates
                            if _suppress_post_tool_audio:
                                continue

                            _turn_audio_bytes += len(pcm_24khz)
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

                                if token_tracker:
                                    token_tracker.add_tool_call(func_name, func_args, result)

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

                            # Detect empty/silent turns and nudge Gemini to retry
                            if _turn_audio_bytes == 0 and not _suppress_post_tool_audio:
                                print(f"⚠️ Empty audio turn detected — nudging Gemini to respond")
                                await gemini_client.send_text(
                                    "You did not produce any audio response. "
                                    "The customer is waiting. Please respond now based on the conversation so far."
                                )

                            _audio_sent_before_tool = False
                            _suppress_post_tool_audio = False
                            _turn_audio_bytes = 0

                            if token_tracker:
                                token_tracker.finalize_turn()

                        elif response.type == 'input_transcription':
                            print(f"🎤 User said: {response.transcription}")
                            if token_tracker and response.transcription:
                                token_tracker.set_input_transcription(response.transcription)

                        elif response.type == 'output_transcription':
                            print(f"🔊 Agent said: {response.transcription}")
                            if token_tracker and response.transcription:
                                token_tracker.set_output_transcription(response.transcription)
                        
                        elif response.type == 'tool_call_cancelled':
                            print(f"⚠️ Tool calls cancelled")
                            _tool_call_received_at = None
                            _tool_response_sent_at = None
                            continue

                        elif response.type == 'usage_metadata' and response.usage_metadata:
                            meta = response.usage_metadata
                            total = meta.get("total_token_count")
                            details = meta.get("response_tokens_details", [])
                            detail_str = ", ".join(
                                f"{d['modality']}: {d['token_count']}" for d in details
                            ) if details else ""
                            print(f"🔢 [GEMINI TOKENS] call={call_id} total={total}{' | ' + detail_str if detail_str else ''}")

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
        
        async def speaker_monitor():
            """Periodically run speaker verification off the audio hot path.
            Exits (and thereby triggers call cleanup) when a mismatch is
            confirmed."""
            while True:
                await asyncio.sleep(0.5)
                if speaker_verifier is None:
                    continue
                try:
                    result = await speaker_verifier.maybe_check()
                except Exception as e:
                    print(f"❌ [SECURITY] speaker_monitor error: {e}")
                    traceback.print_exc()
                    continue
                if result is not None and result.mismatch_confirmed:
                    await _handle_speaker_mismatch(
                        websocket, gemini_client, call_id, result
                    )
                    return

        # Run tasks concurrently
        recv_task = asyncio.create_task(receive_from_browser())
        send_task = asyncio.create_task(receive_from_gemini_and_forward())
        monitor_task = asyncio.create_task(speaker_monitor())

        try:
            done, pending = await asyncio.wait(
                [recv_task, send_task, monitor_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            for task in done:
                if task == recv_task:
                    print(f"🔚 Browser receive task completed for call {call_id}")
                elif task == send_task:
                    print(f"🔚 Gemini send task completed for call {call_id}")
                elif task == monitor_task:
                    print(f"🔚 Speaker monitor task completed for call {call_id}")

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
        if cleanup_done:
            return
        cleanup_done = True

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

            token_summary = token_tracker.get_summary() if token_tracker else {}
            if token_summary:
                ts = token_summary
                print(
                    f"🔢 [TOKENS] call={call_id} FINAL SUMMARY\n"
                    f"   turns: {ts['total_turns']} | base: ~{ts['base_tokens']} "
                    f"(prompt: ~{ts['system_prompt_tokens']}, tools: ~{ts['tools_tokens']})\n"
                    f"   input: ~{ts['total_input_tokens']} | output: ~{ts['total_output_tokens']} "
                    f"| tools: ~{ts['total_tool_tokens']}\n"
                    f"   total: ~{ts['total_tokens']} / {ts['context_window']} "
                    f"({ts['utilization_pct']}% utilization)"
                )

            call_meta_snapshot = call_metadata.get(call_id, {})
            transcripts_output = {
                "call_id": call_id,
                "user_transcript": user_transcript,
                "agent_transcript": agent_transcript,
                "conversation_summary": call_meta_snapshot.get("conversation_summary", ""),
                "topics_discussed": call_meta_snapshot.get("conversation_memory", []),
                "pending_questions": call_meta_snapshot.get("question_queue", []),
                "answered_questions": call_meta_snapshot.get("answered_questions", []),
                "token_usage": token_summary,
                "speaker_mismatch_detected": call_meta_snapshot.get("speaker_mismatch_detected", False),
                "speaker_similarity_at_mismatch": call_meta_snapshot.get("speaker_similarity_at_mismatch"),
                "routing_events": call_meta_snapshot.get("routing_events", []),
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
