"""
Gemini Live API client for real-time audio streaming.

This module provides a client wrapper for Google's Gemini Live API,
handling WebSocket connections, audio streaming, and function calling.
"""

import os
import asyncio
import json
import base64
from typing import Dict, Any, List, Optional, Callable, AsyncIterator
from dataclasses import dataclass, field
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv(override=True)


# Gemini Live API Configuration
# Valid models for Live API: gemini-2.5-flash-native-audio-preview-12-2025
GEMINI_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
GEMINI_SEND_SAMPLE_RATE = 16000  # Input audio sample rate
GEMINI_RECEIVE_SAMPLE_RATE = 24000  # Output audio sample rate

# All available Gemini Live API voices (30 HD voices)
# Keys are Gemini API voice names, 'name' is human-friendly display name
GEMINI_VOICES = {
    # Original Live API voices
    'Puck': {'name': 'Omar', 'gender': 'Male', 'description': 'Conversational, friendly, and upbeat'},
    'Charon': {'name': 'Saad', 'gender': 'Male', 'description': 'Deep, informative, and authoritative'},
    'Kore': {'name': 'Ayesha', 'gender': 'Female', 'description': 'Energetic, youthful, and professional'},
    'Fenrir': {'name': 'Ahmed', 'gender': 'Male', 'description': 'Warm, approachable, and friendly'},
    'Aoede': {'name': 'Sara', 'gender': 'Female', 'description': 'Clear, conversational, and thoughtful'},
    
}

# Map OpenAI voices to Gemini voices
OPENAI_TO_GEMINI_VOICE_MAP = {
    'echo': 'Charon',      # Male, calm and informative
    'alloy': 'Puck',       # Male, upbeat and conversational
    'shimmer': 'Kore',     # Female, energetic and youthful
    'ash': 'Fenrir',       # Male, warm and friendly
    'coral': 'Aoede',      # Female, clear and thoughtful
    'sage': 'Aoede',       # Female, thoughtful
}


def get_gemini_voice(openai_voice: str) -> str:
    """Map OpenAI voice name to Gemini voice."""
    return OPENAI_TO_GEMINI_VOICE_MAP.get(openai_voice, 'Charon')


@dataclass
class GeminiLiveConfig:
    """Configuration for Gemini Live API session."""
    system_instruction: str = ""
    tools: List[Dict[str, Any]] = field(default_factory=list)
    voice: str = "Charon"
    temperature: float = 0.8
    response_modalities: List[str] = field(default_factory=lambda: ["AUDIO"])
    enable_input_transcription: bool = True
    enable_output_transcription: bool = True


def convert_openai_tools_to_gemini(openai_tools: List[Dict[str, Any]]) -> List[types.Tool]:
    """
    Convert OpenAI function calling tools format to Gemini format.
    
    OpenAI format:
    {
        "type": "function",
        "name": "function_name",
        "description": "...",
        "parameters": {...}
    }
    
    Gemini format uses FunctionDeclaration with same structure.
    """
    function_declarations = []
    
    for tool in openai_tools:
        if tool.get("type") == "function":
            func_decl = types.FunctionDeclaration(
                name=tool.get("name", ""),
                description=tool.get("description", ""),
                parameters=tool.get("parameters", {})
            )
            function_declarations.append(func_decl)
    
    if function_declarations:
        return [types.Tool(function_declarations=function_declarations)]
    return []


def convert_openai_tools_to_gemini_dict(openai_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert OpenAI function calling tools format to Gemini dict format for config.
    
    Returns a list of tool dictionaries suitable for the Live API config.
    """
    function_declarations = []
    
    for tool in openai_tools:
        if tool.get("type") == "function":
            func_decl = {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {})
            }
            function_declarations.append(func_decl)
    
    if function_declarations:
        return [{"function_declarations": function_declarations}]
    return []


@dataclass
class GeminiResponse:
    """Represents a response from Gemini Live API."""
    type: str  # 'audio', 'text', 'tool_call', 'setup_complete', 'turn_complete', 'interrupted', 'usage_metadata'
    audio_data: Optional[bytes] = None
    text: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    transcription: Optional[str] = None
    is_final: bool = False
    usage_metadata: Optional[Dict[str, Any]] = None


class GeminiLiveClient:
    """
    Client for Gemini Live API with real-time audio streaming.
    
    Usage:
        config = GeminiLiveConfig(
            system_instruction="You are a helpful assistant.",
            tools=converted_tools,
            voice="Charon"
        )
        
        async with GeminiLiveClient(config) as client:
            # Send audio
            await client.send_audio(audio_bytes)
            
            # Receive responses
            async for response in client.receive():
                if response.type == 'audio':
                    # Handle audio output
                    pass
                elif response.type == 'tool_call':
                    # Handle function calls
                    result = await execute_function(...)
                    await client.send_tool_response(call_id, result)
    """
    
    def __init__(self, config: GeminiLiveConfig):
        self.config = config
        self.client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        self.session = None
        self._session_context = None
        self._receive_task = None
        self._audio_queue = asyncio.Queue()
        self._response_queue = asyncio.Queue()
        self._is_connected = False
        self._pending_tool_calls: Dict[str, Dict] = {}
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def connect(self) -> None:
        """Establish connection to Gemini Live API."""
        # Build config as a simple dictionary (per official docs)
        config = {
            "response_modalities": self.config.response_modalities,
        }
        
        # Add speech config for voice
        if "AUDIO" in self.config.response_modalities and self.config.voice:
            config["speech_config"] = {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": self.config.voice
                    }
                }
            }
        
        # Add system instruction
        if self.config.system_instruction:
            config["system_instruction"] = self.config.system_instruction
        
        # Add tools if defined
        if self.config.tools:
            tools = convert_openai_tools_to_gemini_dict(self.config.tools)
            tools.append({"google_search": {}})
            config["tools"] = tools
        
        config["output_audio_transcription"] = {}
        config["input_audio_transcription"] = {}
        config["context_window_compression"] = {
            "sliding_window": {
                "target_tokens": 8192
            }
        }

        # Wire temperature into generation config
        config["generation_config"] = {
            "temperature": self.config.temperature
        }

        # Add VAD (Voice Activity Detection) settings
        # Using official Gemini API syntax from: https://ai.google.dev/gemini-api/docs/live-guide
        config["realtime_input_config"] = {
            "automatic_activity_detection": {
                "disabled": False,
                "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_LOW,
                "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_LOW,
                "prefix_padding_ms": 20,
                "silence_duration_ms": 200,
            }
        }

        print(f"🎙️ VAD Settings: prefix_padding=20ms, silence_duration=400ms, start_sensitivity=MEDIUM, temp={self.config.temperature}")
        
        # Connect to Live API - this returns an async context manager
        self._session_context = self.client.aio.live.connect(
            model=GEMINI_MODEL,
            config=config
        )
        # Enter the async context manager to get the actual session
        self.session = await self._session_context.__aenter__()
        self._is_connected = True
        print(f"✅ Connected to Gemini Live API (model: {GEMINI_MODEL}, voice: {self.config.voice})")
    
    async def close(self) -> None:
        """Close the connection."""
        self._is_connected = False
        if self._session_context:
            try:
                # Exit the async context manager properly
                await self._session_context.__aexit__(None, None, None)
            except Exception as e:
                print(f"⚠️ Error closing Gemini session: {e}")
        self.session = None
        self._session_context = None
        print("🔌 Gemini Live API connection closed")
    
    async def send_audio(self, pcm_data: bytes, mime_type: str = "audio/pcm") -> None:
        """
        Send audio data to Gemini.
        
        Args:
            pcm_data: 16-bit PCM audio at 16kHz sample rate
            mime_type: Audio MIME type (default: audio/pcm)
        """
        if not self._is_connected or not self.session:
            raise RuntimeError("Not connected to Gemini Live API")
        
        await self.session.send_realtime_input(
            audio=types.Blob(data=pcm_data, mime_type=mime_type)
        )
    
    async def send_text(self, text: str) -> None:
        """Send text input to Gemini to trigger a response."""
        if not self._is_connected or not self.session:
            raise RuntimeError("Not connected to Gemini Live API")
        
        # Use the correct format from official docs
        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": text}]},
            turn_complete=True
        )
    
    async def send_tool_response(self, function_responses: List[Dict[str, Any]]) -> None:
        """
        Send function call responses back to Gemini.
        
        Args:
            function_responses: List of {"id": "...", "name": "...", "response": {...}}
        """
        if not self._is_connected or not self.session:
            raise RuntimeError("Not connected to Gemini Live API")
        
        responses = []
        for resp in function_responses:
            responses.append(types.FunctionResponse(
                id=resp.get("id"),
                name=resp.get("name"),
                response=resp.get("response", {})
            ))
        
        await self.session.send_tool_response(function_responses=responses)
    
    async def receive(self) -> AsyncIterator[GeminiResponse]:
        """
        Async iterator for receiving responses from Gemini.
        
        Yields:
            GeminiResponse objects with audio, text, or tool calls
        """
        if not self._is_connected or not self.session:
            raise RuntimeError("Not connected to Gemini Live API")
        
        try:
            # Continuously receive turns - each receive() handles one turn
            # We need to keep calling receive() to handle multiple turns
            while self._is_connected:
                turn = self.session.receive()
                async for response in turn:
                    try:
                        # Handle server content (audio/text responses)
                        if response.server_content:
                            content = response.server_content
                            
                            # Check for model turn with parts
                            if content.model_turn:
                                for part in content.model_turn.parts:
                                    # Audio data
                                    if hasattr(part, 'inline_data') and part.inline_data:
                                        if hasattr(part.inline_data, 'data') and isinstance(part.inline_data.data, bytes):
                                            yield GeminiResponse(
                                                type='audio',
                                                audio_data=part.inline_data.data
                                            )
                                    # Text data
                                    elif hasattr(part, 'text') and part.text:
                                        yield GeminiResponse(
                                            type='text',
                                            text=part.text
                                        )
                            
                            # Check for turn complete
                            if hasattr(content, 'turn_complete') and content.turn_complete:
                                yield GeminiResponse(type='turn_complete', is_final=True)
                            
                            # Check for interruption
                            if hasattr(content, 'interrupted') and content.interrupted:
                                yield GeminiResponse(type='interrupted')
                            
                            # Handle transcriptions
                            if hasattr(content, 'input_transcription') and content.input_transcription:
                                if hasattr(content.input_transcription, 'text') and content.input_transcription.text:
                                    yield GeminiResponse(
                                        type='input_transcription',
                                        transcription=content.input_transcription.text
                                    )
                            if hasattr(content, 'output_transcription') and content.output_transcription:
                                if hasattr(content.output_transcription, 'text') and content.output_transcription.text:
                                    yield GeminiResponse(
                                        type='output_transcription',
                                        transcription=content.output_transcription.text
                                    )
                        
                        # Handle tool calls
                        if response.tool_call:
                            tool_calls = []
                            for func_call in response.tool_call.function_calls:
                                tool_calls.append({
                                    "id": func_call.id,
                                    "name": func_call.name,
                                    "arguments": dict(func_call.args) if func_call.args else {}
                                })
                            
                            if tool_calls:
                                yield GeminiResponse(
                                    type='tool_call',
                                    tool_calls=tool_calls
                                )
                        
                        # Handle tool call cancellation
                        if hasattr(response, 'tool_call_cancellation') and response.tool_call_cancellation:
                            cancelled_ids = response.tool_call_cancellation.ids
                            print(f"⚠️ Tool calls cancelled: {cancelled_ids}")
                            yield GeminiResponse(
                                type='tool_call_cancelled',
                                tool_calls=[{"cancelled_ids": cancelled_ids}]
                            )

                        # Handle usage metadata (real token counts from Gemini)
                        if hasattr(response, 'usage_metadata') and response.usage_metadata:
                            usage = response.usage_metadata
                            meta = {
                                "total_token_count": getattr(usage, 'total_token_count', None),
                            }
                            details = getattr(usage, 'response_tokens_details', None)
                            if details:
                                meta["response_tokens_details"] = [
                                    {"modality": str(d.modality), "token_count": d.token_count}
                                    for d in details
                                    if hasattr(d, 'modality') and hasattr(d, 'token_count')
                                ]
                            yield GeminiResponse(
                                type='usage_metadata',
                                usage_metadata=meta
                            )

                    except Exception as parse_error:
                        print(f"⚠️ Error parsing Gemini response: {parse_error}")
                        # Continue processing other responses
                        continue
                            
        except Exception as e:
            print(f"❌ Error in Gemini receive loop: {e}")
            import traceback
            traceback.print_exc()
            raise
    
    @property
    def is_connected(self) -> bool:
        return self._is_connected


async def test_gemini_connection():
    """Test basic Gemini Live API connection."""
    config = GeminiLiveConfig(
        system_instruction="You are a helpful assistant. Say hello briefly.",
        voice="Charon"
    )
    
    try:
        async with GeminiLiveClient(config) as client:
            print("✅ Connection test successful!")
            
            # Send a text message to trigger a response
            await client.send_text("Hello!")
            
            # Receive response
            async for response in client.receive():
                print(f"📨 Response type: {response.type}")
                if response.type == 'audio':
                    print(f"   Audio bytes: {len(response.audio_data)}")
                elif response.type == 'text':
                    print(f"   Text: {response.text}")
                elif response.type == 'turn_complete':
                    print("   Turn complete")
                    break
                    
    except Exception as e:
        print(f"❌ Connection test failed: {e}")
        raise


if __name__ == "__main__":
    # Run connection test
    asyncio.run(test_gemini_connection())
