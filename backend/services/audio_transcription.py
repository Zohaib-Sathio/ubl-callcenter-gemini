from openai import AsyncOpenAI
import asyncio
from dotenv import load_dotenv
import os
import json
from pathlib import Path
from typing import List

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RECORDINGS_ANALYSIS = _REPO_ROOT / "recordings" / "analysis"

load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def transcribe_audio(file_path: str, language: str | None = None):
    """
    Transcribe a call recording.

    Whisper auto-detects language by default, but for short Urdu clips it frequently
    flips to Hindi (same phonetics, larger training share). Passing an explicit ISO
    language code ("ur", "en", "ps", "sd", "pa") locks Whisper to that language and
    eliminates the Hindi contamination that the UAT client reported.

    Default is "ur" because the overwhelming majority of UBL call-center traffic is
    Urdu. Callers handling multilingual or English-dominant sessions should pass
    language explicitly.
    """
    resolved_language = language or os.getenv("WHISPER_LANGUAGE", "ur")

    with open(file_path, "rb") as audio_file:
        transcription = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language=resolved_language,
            prompt=(
                "This is a UBL Pakistan call-center recording. The conversation is in "
                "Urdu, English, Sindhi, Punjabi, Pashto, or Siraiki — NOT Hindi. "
                "Transcribe in the same language as spoken without translating. "
                "Proper nouns are Pakistani: Muhammad, Ahmed, Ayesha, Fatima, Khan, Malik, Sheikh. "
                "Banking terms may appear: CNIC, TPIN, UBL, debit card, expiry, balance. "
                "Digits should be transcribed as numerals, not words."
            )
        )
    return transcription.text


async def analyze_call_with_llm(call_id: str, user_transcript: str, agent_transcript: str):
    combined_transcripts = f"""
[AGENT TRANSCRIPT]
{agent_transcript.strip()}

[USER TRANSCRIPT]
{user_transcript.strip()}
"""

    system_prompt = """
You are a professional call quality analysis system for UBL Digital contact center.

The conversation is provided in two separate blocks:
- Agent transcript: utterances spoken by the bank agent.
- User transcript: utterances spoken by the customer.

These transcripts may not be in perfect alternating order. Your first task is to:
1. Reconstruct the conversation in the correct chronological sequence of turns.
2. Clearly label each turn as either "AGENT" or "USER".
3. Make sure questions and answers are logically paired (agent questions with user answers, and vice versa where applicable).

Once the conversation is reconstructed, analyze it and return a STRICT JSON object with the following structure:
- All fields with <score> tag should have values in percentages ranging from 0 - 100%.

{
  "core_performance": {
    "intent_recognition_accuracy": "<score>",
    "entity_extraction_accuracy": "<score>",
    "task_completion_rate": "<score>",
    "fallback_rate": "<score>",
    "branch_logic_accuracy": "<score>"
  },
  "technical_performance": {
    "response_latency": "<score>",
    "transcription_accuracy": "<score>",
    "speech_clarity": "<score>"
  },
  "conversational_quality": {
    "interrupt_handling": "<score>",
    "turn_taking_management": "<score>",
    "context_retention": "<score>",
    "tone_appropriateness": "<score>",
    "accent_understanding": "<score>",
    "disfluency_handling": "<score>"
  },
  "compliance_and_ux": {
    "ai_disclosure": "<yes/no>",
    "empathy_score": "<score>",
    "confusion_rate": "<score>"
  },
  "summary": "<3-4 line summary of the call highlighting the key points and quality>"
}

Return ONLY valid JSON. Do not include explanations or any text outside of the JSON object.
"""

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": combined_transcripts}
        ],
        temperature=0,
        response_format={"type": "json_object"}
    )

    content = response.choices[0].message.content

    try:
        parsed_json = json.loads(content)
    except json.JSONDecodeError:
        parsed_json = {"error": "Failed to parse LLM output", "raw": content}

    _RECORDINGS_ANALYSIS.mkdir(parents=True, exist_ok=True)
    analysis_path = _RECORDINGS_ANALYSIS / f"{call_id}_analysis.json"
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(parsed_json, f, ensure_ascii=False, indent=2)

    return parsed_json
