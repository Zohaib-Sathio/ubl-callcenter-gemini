import uuid
import io
from pydub import AudioSegment
import audioop
from dateutil import parser
from datetime import timezone, timedelta
import datetime



try:
    import pyaudio
except ImportError:
    pyaudio = None

CHUNK = 1024
if pyaudio:
    FORMAT = pyaudio.paInt16
else:
    FORMAT = None
CHANNELS = 1
RATE = 8000




def check_weekday_or_error(date_str: str) -> dict | None:
    appt_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    wd = appt_date.weekday()

    if wd == 5:
        return {"status_code": 400, "error": "Cannot book on Saturday. Please choose a weekday."}
    elif wd == 6:
        return {"status_code": 400, "error": "Cannot book on Sunday. Please choose a weekday."}
    else:
        return None


def check_business_hours(time_str: str) -> dict | None:
    t = datetime.datetime.strptime(time_str, "%H:%M").time()
    earliest = datetime.time(hour=9, minute=30)
    if t < earliest:
        return {
            "status_code": 400,
            "error": "Cannot book before 9:30 AM. Please choose a later time."
        }
    return None




def generate_call_id():
    return str(uuid.uuid4())

def get_total_duration_ms(events):
    if not events:
        return 0
    last_offset = max(offset for offset, _ in events)
    chunk_duration_ms = int((CHUNK / RATE) * 1000)
    total = int(last_offset * 1000) + chunk_duration_ms
    print(f"Total duration (ms): {total} (last offset: {last_offset:.2f} sec, chunk duration: {chunk_duration_ms} ms)")
    return total

def merge_timeline_events(events, total_duration_ms):
    base = AudioSegment.silent(duration=total_duration_ms, frame_rate=RATE)
    sorted_events = sorted(events, key=lambda x: x[0])
    print(f"Merging {len(sorted_events)} events into a base of {total_duration_ms} ms")
    for offset, audio_data in sorted_events:
        try:
            pcm_audio = audioop.ulaw2lin(audio_data, 2) 
            seg = AudioSegment.from_raw(io.BytesIO(pcm_audio), frame_rate=RATE, channels=1, sample_width=2)
            base = base.overlay(seg, position=int(offset * 1000))
        except Exception as e:
            print(f"Error overlaying chunk at {offset:.2f} sec: {e}")
    return base

def make_filenames(call_id):
    return (
        f"call_{call_id}_incoming.wav",
        f"call_{call_id}_outgoing.wav",
        f"call_{call_id}_merged.wav"
    )

def to_iso_z_from_simple(date_str: str,
                         time_str: str) -> tuple[str, str]:
    local = parser.parse(f"{date_str} {time_str}")

    end_local = local + timedelta(hours=1)

    start_z = local.replace(tzinfo=timezone.utc) \
                   .isoformat(timespec="milliseconds") \
                   .replace("+00:00", "Z")
    end_z   = end_local.replace(tzinfo=timezone.utc) \
                       .isoformat(timespec="milliseconds") \
                       .replace("+00:00", "Z")

    return start_z, end_z
