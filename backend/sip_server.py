"""
AudioSocket TCP server bridging Asterisk SIP calls to Gemini Live.

Runs as a separate process alongside the existing FastAPI backend. Asterisk's
`AudioSocket()` dialplan application connects here; per connection this
server:
  - Receives 8 kHz SLIN audio from Asterisk, upsamples to 16 kHz, forwards
    to Gemini Live.
  - Receives 24 kHz SLIN audio from Gemini, downsamples to 8 kHz, writes
    back over the same TCP socket as SLIN frames.
  - Buffers DTMF digits (keypad input, e.g. TPIN) and forwards them to
    Gemini as system-tagged text when the buffer is "ready"
    (4 digits collected, `#` pressed, or short pause after last digit).
  - Executes workflow tool calls via the shared `execute_function_call`
    imported from `backend.main`, so all existing verification/workflow
    logic is reused with zero duplication.
  - Saves recordings + transcripts + analysis JSON in the exact same
    format as the browser path.

Run with:  python -m backend.sip_server
Defaults:  listens on 127.0.0.1:6090  (override via SIP_SERVER_HOST/PORT env)

AudioSocket protocol reference (Asterisk `app_audiosocket`):
  Header = 1 byte kind + 2 bytes length (big-endian uint16)
  Kinds:
    0x00 HANGUP
    0x01 ID      (payload = 16-byte channel UUID, sent once on connect)
    0x03 DTMF    (payload = 1 ASCII byte, '0'-'9' '*' '#')
    0x10 SLIN    (payload = 16-bit signed linear PCM, 8 kHz mono, little-endian)
    0xff ERROR   (payload = 1 byte error code)
"""

import asyncio
import audioop
import io
import json
import os
import struct
import time
import traceback
import uuid as uuid_lib
import wave
from pathlib import Path

from dotenv import load_dotenv

from backend.services.prompts import build_system_message
from backend.services.gemini_live import GeminiLiveClient, GeminiLiveConfig
from backend.services.audio_transcription import transcribe_audio, analyze_call_with_llm
from backend.workflow.registry import (
    get_all_tools_with_selector,
    get_workflow_policy_context,
)
from backend.logger.call_log_apis import register_call, update_call_status

# Shared logic from the main backend module. Importing it creates a second
# in-memory FastAPI app object in this process, but nothing starts serving
# HTTP here — we only reuse the helpers and per-process state dicts.
from backend.main import (
    execute_function_call,
    _init_conversation_state,
    call_metadata,
    call_recordings,
    TokenTracker,
    USER_AUDIO_DIR,
    AGENT_AUDIO_DIR,
    RECORDINGS_DIR,
)

load_dotenv(override=True)

SIP_SERVER_HOST = os.getenv("SIP_SERVER_HOST", "127.0.0.1")
SIP_SERVER_PORT = int(os.getenv("SIP_SERVER_PORT", "6090"))

KIND_HANGUP = 0x00
KIND_ID     = 0x01
KIND_DTMF   = 0x03
KIND_SLIN   = 0x10
KIND_ERROR  = 0xff

ASTERISK_SAMPLE_RATE = 8000
ASTERISK_FRAME_MS    = 20
ASTERISK_FRAME_BYTES = ASTERISK_SAMPLE_RATE * 2 * ASTERISK_FRAME_MS // 1000  # 320

DTMF_TPIN_LENGTH   = 4
DTMF_FLUSH_SECONDS = 2.0


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            raise ConnectionError("audiosocket: peer closed mid-frame")
        buf += chunk
    return buf


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await _read_exact(reader, 3)
    kind = header[0]
    length = struct.unpack(">H", header[1:3])[0]
    payload = await _read_exact(reader, length) if length else b""
    return kind, payload


def _frame(kind: int, payload: bytes) -> bytes:
    return bytes([kind]) + struct.pack(">H", len(payload)) + payload


def _save_wav(path: str, pcm: bytes, rate: int) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)


class DtmfBuffer:
    """Collect DTMF digits and emit them to Gemini under known patterns.

    Emits when:
      - `#` pressed (strip it, flush the rest)
      - 4 digits collected (likely TPIN)
      - 2 seconds pass with no new digit (timeout flush)
    """

    def __init__(self) -> None:
        self.digits = ""
        self.last_digit_ts: float | None = None

    def add(self, digit: str) -> None:
        self.digits += digit
        self.last_digit_ts = time.time()

    def pop_if_ready(self) -> str | None:
        if not self.digits:
            return None
        if "#" in self.digits:
            out = self.digits.replace("#", "")
            self.reset()
            return out
        if len(self.digits) >= DTMF_TPIN_LENGTH:
            out = self.digits[:DTMF_TPIN_LENGTH]
            self.digits = self.digits[DTMF_TPIN_LENGTH:]
            if not self.digits:
                self.last_digit_ts = None
            return out
        return None

    def pop_if_timeout(self) -> str | None:
        if self.digits and self.last_digit_ts is not None:
            if time.time() - self.last_digit_ts > DTMF_FLUSH_SECONDS:
                out = self.digits
                self.reset()
                return out
        return None

    def reset(self) -> None:
        self.digits = ""
        self.last_digit_ts = None


async def handle_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    print(f"📞 [SIP] Connection from {peer}")

    asterisk_uuid: str | None = None
    call_id: str | None = None
    gemini_client: GeminiLiveClient | None = None
    token_tracker: TokenTracker | None = None
    dtmf = DtmfBuffer()

    user_pcm_buffer = io.BytesIO()    # caller audio, 8 kHz
    agent_pcm_buffer = io.BytesIO()   # Gemini output, 24 kHz
    agent_pcm_8k_buffer = io.BytesIO()  # what we actually send to Asterisk after 24k->8k resample

    up_state = None    # 8 kHz -> 16 kHz resample state (per call)
    down_state = None  # 24 kHz -> 8 kHz resample state (per call)

    hangup_received = False

    async def send_audio_to_asterisk(pcm_24khz: bytes) -> None:
        nonlocal down_state
        if not pcm_24khz:
            return
        pcm_8khz, down_state = audioop.ratecv(pcm_24khz, 2, 1, 24000, 8000, down_state)
        agent_pcm_8k_buffer.write(pcm_8khz)
        for i in range(0, len(pcm_8khz), ASTERISK_FRAME_BYTES):
            chunk = pcm_8khz[i:i + ASTERISK_FRAME_BYTES]
            if not chunk:
                continue
            writer.write(_frame(KIND_SLIN, chunk))
        try:
            await writer.drain()
        except ConnectionError:
            pass

    try:
        kind, payload = await _read_frame(reader)
        if kind != KIND_ID or len(payload) != 16:
            print(f"❌ [SIP] Expected ID frame, got kind={hex(kind)} len={len(payload)}")
            return
        asterisk_uuid = str(uuid_lib.UUID(bytes=payload))
        print(f"📞 [SIP] Asterisk channel UUID: {asterisk_uuid}")

        caller_number = "sip-incoming"
        reg = await register_call(caller_number)
        call_id = str(reg) if reg else asterisk_uuid
        call_recordings[call_id] = {"incoming": [], "outgoing": [], "start_time": time.time()}
        call_metadata[call_id] = {
            "phone": caller_number,
            "language_id": 1,
            "voice": "Charon",
            "temperature": 0.8,
            "speed": 1.0,
            "asterisk_uuid": asterisk_uuid,
            "source": "sip",
        }
        _init_conversation_state(call_id)
        call_metadata[call_id]["active_workflow"] = None
        call_metadata[call_id]["workflow_phase"] = None
        call_metadata[call_id]["workflow_selection_reason"] = ""
        call_metadata[call_id]["routing_events"] = []
        call_metadata[call_id]["blocked_tool_attempts"] = 0
        if reg:
            try:
                await update_call_status(int(call_id), "pick")
            except Exception as e:
                print(f"⚠️ [SIP] Could not update call status: {e}")

        all_tools = get_all_tools_with_selector()
        # Strip the RAG tool from the list — not used on the SIP path.
        all_tools = [t for t in all_tools if t.get("name") != "searchKnowledgeBase"]
        workflow_context = get_workflow_policy_context()
        system_message = build_system_message(
            instructions="",
            caller=caller_number,
            voice="Charon",
            workflow_context=workflow_context,
        )
        token_tracker = TokenTracker(call_id, system_message, all_tools)

        config = GeminiLiveConfig(
            system_instruction=system_message,
            tools=all_tools,
            voice="Charon",
            temperature=0.8,
        )
        gemini_client = GeminiLiveClient(config)
        await gemini_client.connect()

        await gemini_client.send_text("Start the conversation by greeting the customer warmly.")

        async def from_asterisk() -> None:
            nonlocal up_state, hangup_received
            while not hangup_received:
                try:
                    kind, payload = await _read_frame(reader)
                except (ConnectionError, asyncio.IncompleteReadError):
                    hangup_received = True
                    return

                if kind == KIND_SLIN:
                    user_pcm_buffer.write(payload)
                    pcm_16k, up_state = audioop.ratecv(payload, 2, 1, 8000, 16000, up_state)
                    if token_tracker:
                        token_tracker.add_input_audio(len(pcm_16k))
                    await gemini_client.send_audio(pcm_16k)

                elif kind == KIND_DTMF:
                    digit = payload.decode("ascii", errors="ignore")
                    print(f"☎️  [SIP] DTMF: {digit!r}")
                    dtmf.add(digit)
                    ready = dtmf.pop_if_ready()
                    if ready:
                        await gemini_client.send_text(
                            f"[SYSTEM] Customer entered keypad digits: {ready}. "
                            f"Use these digits as their input for the current step "
                            f"(e.g. call verifyTpin if the TPIN was being requested)."
                        )

                elif kind == KIND_HANGUP:
                    print(f"📞 [SIP] Hangup from Asterisk for call {call_id}")
                    hangup_received = True
                    return

                elif kind == KIND_ERROR:
                    err = payload[0] if payload else -1
                    print(f"❌ [SIP] Asterisk error frame: {err}")
                    hangup_received = True
                    return

                elif kind == KIND_ID:
                    # Extra ID frames are unusual; ignore.
                    pass
                else:
                    print(f"⚠️ [SIP] Unknown frame kind: {hex(kind)}")

        async def dtmf_timeout_watcher() -> None:
            while not hangup_received:
                await asyncio.sleep(0.5)
                pending = dtmf.pop_if_timeout()
                if pending and gemini_client and gemini_client.is_connected:
                    await gemini_client.send_text(
                        f"[SYSTEM] Customer entered keypad digits: {pending}."
                    )

        async def to_asterisk() -> None:
            nonlocal hangup_received
            try:
                async for response in gemini_client.receive():
                    if hangup_received:
                        return

                    if response.type == "audio":
                        pcm_24khz = response.audio_data
                        agent_pcm_buffer.write(pcm_24khz)
                        if token_tracker:
                            token_tracker.add_output_audio(len(pcm_24khz))
                        await send_audio_to_asterisk(pcm_24khz)

                    elif response.type == "tool_call":
                        for tc in response.tool_calls:
                            func_name = tc.get("name")
                            func_id = tc.get("id")
                            func_args = tc.get("arguments", {})
                            print(f"🔧 [SIP] Tool call: {func_name} args={func_args}")
                            try:
                                result = await asyncio.wait_for(
                                    execute_function_call(func_name, func_args, call_id=call_id),
                                    timeout=30.0,
                                )
                            except asyncio.TimeoutError:
                                result = {
                                    "success": False,
                                    "error": "timeout",
                                    "message": "The operation timed out. Please try again.",
                                }
                            print(f"✅ [SIP] Tool result: {func_name} -> success={result.get('success')}")
                            if token_tracker:
                                token_tracker.add_tool_call(func_name, func_args, result)
                            await gemini_client.send_tool_response([{
                                "id": func_id,
                                "name": func_name,
                                "response": result,
                            }])

                    elif response.type == "turn_complete":
                        if token_tracker:
                            token_tracker.finalize_turn()

                    elif response.type == "input_transcription":
                        if response.transcription:
                            print(f"🎤 [SIP] User said: {response.transcription}")
                            if token_tracker:
                                token_tracker.set_input_transcription(response.transcription)

                    elif response.type == "output_transcription":
                        if response.transcription:
                            print(f"🔊 [SIP] Agent said: {response.transcription}")
                            if token_tracker:
                                token_tracker.set_output_transcription(response.transcription)

                    elif response.type == "interrupted":
                        # AudioSocket has no "clear playout" primitive; Asterisk
                        # will just play whatever has been buffered. Nothing to do.
                        pass

                    elif response.type == "tool_call_cancelled":
                        pass

            except Exception as e:
                print(f"❌ [SIP] Error in Gemini receive loop: {e}")
                traceback.print_exc()

        tasks = [
            asyncio.create_task(from_asterisk()),
            asyncio.create_task(to_asterisk()),
            asyncio.create_task(dtmf_timeout_watcher()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    except Exception as e:
        print(f"❌ [SIP] Error in call handler: {e}")
        traceback.print_exc()

    finally:
        hangup_received = True

        if gemini_client:
            try:
                await gemini_client.close()
            except Exception as e:
                print(f"⚠️ [SIP] Error closing Gemini: {e}")

        try:
            writer.write(_frame(KIND_HANGUP, b""))
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        if call_id:
            try:
                user_path = str(USER_AUDIO_DIR / f"{call_id}_user.wav")
                agent_path = str(AGENT_AUDIO_DIR / f"{call_id}_agent.wav")
                _save_wav(user_path, user_pcm_buffer.getvalue(), 8000)
                _save_wav(agent_path, agent_pcm_buffer.getvalue(), 24000)
                agent_8k_path = str(AGENT_AUDIO_DIR / f"{call_id}_agent_8k.wav")
                _save_wav(agent_8k_path, agent_pcm_8k_buffer.getvalue(), 8000)
                print(f"💾 [SIP] Saved recordings for call {call_id}")

                try:
                    user_txt = await transcribe_audio(user_path)
                except Exception as e:
                    print(f"⚠️ [SIP] User transcription failed: {e}")
                    user_txt = ""
                try:
                    agent_txt = await transcribe_audio(agent_path)
                except Exception as e:
                    print(f"⚠️ [SIP] Agent transcription failed: {e}")
                    agent_txt = ""

                token_summary = token_tracker.get_summary() if token_tracker else {}
                if token_summary:
                    ts = token_summary
                    print(
                        f"🔢 [TOKENS] call={call_id} FINAL SUMMARY (sip)\n"
                        f"   turns: {ts['total_turns']} | base: ~{ts['base_tokens']}\n"
                        f"   input: ~{ts['total_input_tokens']} | output: ~{ts['total_output_tokens']} "
                        f"| tools: ~{ts['total_tool_tokens']}\n"
                        f"   total: ~{ts['total_tokens']} / {ts['context_window']} "
                        f"({ts['utilization_pct']}% utilization)"
                    )

                transcripts_out = {
                    "call_id": call_id,
                    "user_transcript": user_txt,
                    "agent_transcript": agent_txt,
                    "conversation_summary": call_metadata.get(call_id, {}).get("conversation_summary", ""),
                    "topics_discussed": call_metadata.get(call_id, {}).get("conversation_memory", []),
                    "pending_questions": call_metadata.get(call_id, {}).get("question_queue", []),
                    "answered_questions": call_metadata.get(call_id, {}).get("answered_questions", []),
                    "token_usage": token_summary,
                    "source": "sip",
                }
                with open(RECORDINGS_DIR / f"{call_id}_transcript.json", "w", encoding="utf-8") as f:
                    json.dump(transcripts_out, f, ensure_ascii=False, indent=2)

                try:
                    analysis = await analyze_call_with_llm(call_id, user_txt, agent_txt)
                    print(f"📊 [SIP] Call analysis complete: {analysis}")
                except Exception as e:
                    print(f"⚠️ [SIP] Analysis failed: {e}")
            except Exception as e:
                print(f"⚠️ [SIP] Post-call cleanup error: {e}")
                traceback.print_exc()

        print(f"🔚 [SIP] Call ended: {call_id or asterisk_uuid or peer}")


def _on_task_done(t: asyncio.Task, task_set: set[asyncio.Task] | None) -> None:
    if task_set is not None:
        task_set.discard(t)
    if t.cancelled():
        return
    exc = t.exception()
    if exc:
        print(f"❌ [SIP] handle_call task raised: {exc!r}")


async def start_audiosocket_server(
    task_set: set[asyncio.Task] | None = None,
) -> asyncio.Server:
    """Start the AudioSocket TCP listener and return the asyncio.Server.

    If `task_set` is provided, each spawned `handle_call` task is registered
    in it so a parent (e.g. FastAPI shutdown hook) can cancel in-flight calls
    during graceful shutdown. If None, tasks run fire-and-forget but their
    exceptions still surface via the done_callback.
    """
    def _spawn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        t = asyncio.create_task(handle_call(reader, writer))
        if task_set is not None:
            task_set.add(t)
        t.add_done_callback(lambda _t: _on_task_done(_t, task_set))

    server = await asyncio.start_server(_spawn, SIP_SERVER_HOST, SIP_SERVER_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"🎧 [SIP] AudioSocket server listening on {addrs}")
    return server


async def main() -> None:
    """Standalone entrypoint — `python -m backend.sip_server`.

    In production the AudioSocket listener is launched by backend.main's
    FastAPI startup hook (single-process deployment). This entrypoint is
    retained for local/isolated testing without running the full FastAPI app.
    """
    server = await start_audiosocket_server()
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
