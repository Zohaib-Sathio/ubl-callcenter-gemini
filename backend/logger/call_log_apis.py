import time
import httpx
import json


ADD_CALL_URL = "https://api.odentika.com/api/v1/add"
UPDATE_CALL_URL = "https://api.odentika.com/api/v1/update-call-by-id"


def normalize_number(num_str: str) -> int:
    digits = "".join(ch for ch in num_str if ch.isdigit())
    return int(digits) if digits else 0

async def register_call(caller_number_str: str) -> int | None:
    payload = {
        "CallID":     0,
        "CallerPhone":   caller_number_str,
        "RecieverID": 233,  # Different receiver ID for UBL Digital
        "CallType":   "InBound",
        "IsActive":   True
    }

    body = json.dumps(payload)
    headers = {
    "Content-Type": "application/json; charset=utf-8; version=1",
    "Accept":       "application/json; charset=utf-8; version=1",
    "api-version":  "1.0"
}
    print("→ Sending to call logging API:", body, headers)
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(ADD_CALL_URL, content=body, headers=headers)

        if resp.status_code != 200:
            print("⚠️ Call logging add-call failed:", resp.status_code, resp.text)
            fallback_id = int(time.time() * 1000) % 10000000
            print(f"⚠️ Using fallback local CallID: {fallback_id}")
            return fallback_id

        data = resp.json()
        new_id = data.get("CallID")
        print(f"✅ Call logging CallID: {new_id} @ {time.time()}")
        return new_id
        
    except Exception as e:
        fallback_id = int(time.time() * 1000) % 10000000
        print(f"⚠️ Call logging API unreachable ({e}), using fallback local CallID: {fallback_id}")
        return fallback_id


async def update_call_status(call_id: int, action: str) -> bool:
    payload = {
        "CallID":  call_id,
        "updateas": action
    }
    body = json.dumps(payload)
    headers = {
        "Content-Type": "application/json; charset=utf-8; version=1",
        "Accept":       "application/json; charset=utf-8; version=1",
        "api-version":  "1.0",
    }
    print(f"→ Updating call status: {body}")
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(UPDATE_CALL_URL, content=body, headers=headers)

        if resp.status_code != 200:
            print("⚠️ Call logging update-call failed:", resp.status_code, resp.text)
            return False

        print(f"✅ Call logging update-call succeeded: {action}@{call_id} @ {time.time()}")
        return True
        
    except Exception as e:
        print(f"⚠️ Call logging update unreachable ({e}), skipping update")
        return False
