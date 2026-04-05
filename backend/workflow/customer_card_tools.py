import json
import time
from typing import Dict, Optional, Any

CUSTOMER_CARDS: Dict[str, Dict[str, Any]] = {
    "42101-1234567-9": {
        "cnic": "42101-1234567-9",
        "full_name": "Ahmed Khan",
        "mother_maiden_name": "Fatima Bibi",
        "date_of_birth": "1988-05-20",
        "registered_mobile": "0321-1234567",
        "address": "House 45, Block B, DHA Phase 5, Lahore",
        "tpin": "4321",
        "tpin_verified": False,
        "tpin_updated": False,
        "accounts": [
            {
                "account_type": "UBL Smart Account",
                "account_number": "0501234567890",
                "balance_pkr": 125000
            },
            {
                "account_type": "UBL Digital Account",
                "account_number": "0501234567891",
                "balance_pkr": 450000
            }
        ],
        "debit_card": {
            "card_number_last_four": "5678",
            "expiry_date": "09/27",
            "card_type": "UBL VISA Gold Debit Card",
            "physical_custody_confirmed": True,
            "is_activated": False,
            "linked_account": "0501234567890",
            "activation_date": None
        },
        "verification_attempts": {
            "cnic": 0,
            "tpin": 0,
            "card_details": 0
        },
        "max_attempts": 50
    }
}


async def verify_customer_by_cnic(cnic: str) -> dict:
    try:
        print(f"→ verify_customer_by_cnic: {cnic} @ {time.time()}")
        
        normalized_cnic = cnic.replace("-", "").replace(" ", "")
        
        for stored_cnic, customer_data in CUSTOMER_CARDS.items():
            if stored_cnic.replace("-", "").replace(" ", "") == normalized_cnic:
                customer_data["verification_attempts"]["cnic"] += 1
                
                if customer_data["verification_attempts"]["cnic"] > customer_data["max_attempts"]:
                    return {
                        "success": False,
                        "error": "Maximum verification attempts exceeded",
                        "transfer_to_agent": True,
                        "message": "Customer has exceeded maximum CNIC verification attempts. Transfer to human agent required."
                    }
                
                print(f"✅ verify_customer_by_cnic: Found customer {customer_data['full_name']} @ {time.time()}")
                
                return {
                    "success": True,
                    "customer": {
                        "cnic": customer_data["cnic"],
                        "full_name": customer_data["full_name"],
                        "mother_maiden_name": customer_data["mother_maiden_name"],
                        "registered_mobile": customer_data["registered_mobile"],
                        "has_debit_card": True,
                        "card_activated": customer_data["debit_card"]["is_activated"]
                    },
                    "verification_attempts": customer_data["verification_attempts"]["cnic"],
                    "max_attempts": customer_data["max_attempts"],
                    "message": f"Customer {customer_data['full_name']} verified successfully by CNIC."
                }
        
        print(f"⚠️ verify_customer_by_cnic: Customer not found with CNIC {cnic} @ {time.time()}")
        return {
            "success": False,
            "error": "Customer not found",
            "message": "No customer found with this CNIC number. Please verify the CNIC and try again."
        }
        
    except Exception as e:
        print(f"⚠️ verify_customer_by_cnic error: {str(e)} @ {time.time()}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred during CNIC verification. Please try again."
        }


async def confirm_physical_custody(cnic: str, has_card: bool) -> dict:
    try:
        print(f"→ confirm_physical_custody: {cnic}, has_card={has_card} @ {time.time()}")
        
        customer_data = CUSTOMER_CARDS.get(cnic)
        if not customer_data:
            return {
                "success": False,
                "error": "Customer not found",
                "message": "Customer data not found. Please verify CNIC first."
            }
        
        if not has_card:
            print(f"⚠️ confirm_physical_custody: Customer does not have card @ {time.time()}")
            return {
                "success": False,
                "error": "Physical custody not confirmed",
                "transfer_to_agent": True,
                "message": "Customer does not have physical custody of the card. Transfer to agent for card delivery status check."
            }
        
        customer_data["debit_card"]["physical_custody_confirmed"] = True
        
        print(f"✅ confirm_physical_custody: Confirmed for {customer_data['full_name']} @ {time.time()}")
        return {
            "success": True,
            "message": "Physical custody of card confirmed. Proceeding with TPIN verification."
        }
        
    except Exception as e:
        print(f"⚠️ confirm_physical_custody error: {str(e)} @ {time.time()}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred during physical custody confirmation."
        }


async def verify_tpin(cnic: str, tpin: str) -> dict:
    try:
        print(f"→ verify_tpin: {cnic} @ {time.time()}")
        
        customer_data = CUSTOMER_CARDS.get(cnic)
        if not customer_data:
            return {
                "success": False,
                "error": "Customer not found",
                "message": "Customer data not found. Please verify CNIC first."
            }
        
        customer_data["verification_attempts"]["tpin"] += 1
        attempts = customer_data["verification_attempts"]["tpin"]
        max_attempts = customer_data["max_attempts"]
        
        if attempts > max_attempts:
            print(f"⚠️ verify_tpin: Max attempts exceeded @ {time.time()}")
            return {
                "success": False,
                "error": "Maximum verification attempts exceeded",
                "transfer_to_agent": True,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "message": "Maximum TPIN verification attempts exceeded. Transferring to human agent for security."
            }
        
        if tpin == customer_data["tpin"]:
            customer_data["tpin_verified"] = True
            print(f"✅ verify_tpin: TPIN verified for {customer_data['full_name']} @ {time.time()}")
            return {
                "success": True,
                "tpin_verified": True,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "message": "TPIN verified successfully. Proceeding with card details verification."
            }
        else:
            remaining_attempts = max_attempts - attempts
            print(f"⚠️ verify_tpin: Incorrect TPIN, {remaining_attempts} attempts remaining @ {time.time()}")
            return {
                "success": False,
                "error": "Incorrect TPIN",
                "tpin_verified": False,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "remaining_attempts": remaining_attempts,
                "message": f"Incorrect TPIN. You have {remaining_attempts} attempt(s) remaining."
            }
        
    except Exception as e:
        print(f"⚠️ verify_tpin error: {str(e)} @ {time.time()}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred during TPIN verification."
        }


async def verify_card_details(cnic: str, last_four_digits: str, expiry_date: str) -> dict:
    try:
        print(f"→ verify_card_details: {cnic}, last4={last_four_digits}, expiry={expiry_date} @ {time.time()}")
        
        customer_data = CUSTOMER_CARDS.get(cnic)
        if not customer_data:
            return {
                "success": False,
                "error": "Customer not found",
                "message": "Customer data not found. Please verify CNIC first."
            }
        
        customer_data["verification_attempts"]["card_details"] += 1
        attempts = customer_data["verification_attempts"]["card_details"]
        max_attempts = customer_data["max_attempts"]
        
        if attempts > max_attempts:
            print(f"⚠️ verify_card_details: Max attempts exceeded @ {time.time()}")
            return {
                "success": False,
                "error": "Maximum verification attempts exceeded",
                "transfer_to_agent": True,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "message": "Maximum card verification attempts exceeded. Transferring to human agent."
            }
        
        card_data = customer_data["debit_card"]
        
        expiry_normalized = expiry_date.replace(" ", "").replace("-", "/")
        stored_expiry = card_data["expiry_date"]
        
        digits_match = last_four_digits == card_data["card_number_last_four"]
        
        expiry_match = (
            expiry_normalized == stored_expiry or
            expiry_normalized == stored_expiry[:5] or
            expiry_normalized.replace("/", "") == stored_expiry.replace("/", "")
        )
        
        if digits_match and expiry_match:
            print(f"✅ verify_card_details: Card verified for {customer_data['full_name']} @ {time.time()}")
            return {
                "success": True,
                "card_verified": True,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "card_type": card_data["card_type"],
                "message": "Card details verified successfully. Ready for activation."
            }
        else:
            remaining_attempts = max_attempts - attempts
            error_detail = []
            if not digits_match:
                error_detail.append("last 4 digits incorrect")
            if not expiry_match:
                error_detail.append("expiry date incorrect")
            
            print(f"⚠️ verify_card_details: Verification failed - {', '.join(error_detail)} @ {time.time()}")
            return {
                "success": False,
                "error": f"Card verification failed: {', '.join(error_detail)}",
                "card_verified": False,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "remaining_attempts": remaining_attempts,
                "message": f"Card details incorrect ({', '.join(error_detail)}). You have {remaining_attempts} attempt(s) remaining."
            }
        
    except Exception as e:
        print(f"⚠️ verify_card_details error: {str(e)} @ {time.time()}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred during card verification."
        }


async def activate_card(cnic: str) -> dict:
    try:
        print(f"→ activate_card: {cnic} @ {time.time()}")
        
        customer_data = CUSTOMER_CARDS.get(cnic)
        if not customer_data:
            return {
                "success": False,
                "error": "Customer not found",
                "message": "Customer data not found."
            }
        
        if not customer_data.get("tpin_verified"):
            return {
                "success": False,
                "error": "TPIN not verified",
                "message": "TPIN verification required before card activation."
            }
        
        if not customer_data["debit_card"].get("physical_custody_confirmed"):
            return {
                "success": False,
                "error": "Physical custody not confirmed",
                "message": "Physical custody confirmation required before card activation."
            }
        
        customer_data["debit_card"]["is_activated"] = True
        customer_data["debit_card"]["activation_date"] = time.strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"✅ activate_card: Card activated for {customer_data['full_name']} @ {time.time()}")
        return {
            "success": True,
            "card_activated": True,
            "activation_date": customer_data["debit_card"]["activation_date"],
            "card_type": customer_data["debit_card"]["card_type"],
            "message": f"Card activated successfully for {customer_data['full_name']}. Please proceed to PIN generation."
        }
        
    except Exception as e:
        print(f"⚠️ activate_card error: {str(e)} @ {time.time()}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred during card activation."
        }


async def update_customer_tpin(cnic: str, new_tpin: str) -> dict:
    try:
        print(f"→ update_customer_tpin: {cnic} @ {time.time()}")
        
        customer_data = CUSTOMER_CARDS.get(cnic)
        if not customer_data:
            return {
                "success": False,
                "error": "Customer not found",
                "message": "Customer data not found."
            }
        
        if not new_tpin.isdigit() or len(new_tpin) != 4:
            return {
                "success": False,
                "error": "Invalid TPIN format",
                "message": "TPIN must be exactly 4 digits."
            }
        
        old_tpin = customer_data["tpin"]
        customer_data["tpin"] = new_tpin
        customer_data["tpin_updated"] = True
        customer_data["tpin_verified"] = True
        
        print(f"✅ update_customer_tpin: TPIN updated for {customer_data['full_name']} @ {time.time()}")
        return {
            "success": True,
            "tpin_updated": True,
            "message": f"TPIN updated successfully for {customer_data['full_name']}."
        }
        
    except Exception as e:
        print(f"⚠️ update_customer_tpin error: {str(e)} @ {time.time()}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred during TPIN update."
        }


async def transfer_to_ivr_for_pin() -> dict:
    try:
        print(f"→ transfer_to_ivr_for_pin @ {time.time()}")
        
        print(f"✅ transfer_to_ivr_for_pin: Transfer initiated @ {time.time()}")
        return {
            "success": True,
            "message": "Transferring to IVR for PIN generation. Please follow the IVR prompts to set your card PIN."
        }
        
    except Exception as e:
        print(f"⚠️ transfer_to_ivr_for_pin error: {str(e)} @ {time.time()}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred during IVR transfer."
        }


async def transfer_to_agent(cnic: str, reason: str) -> dict:
    try:
        print(f"→ transfer_to_agent: {cnic}, reason={reason} @ {time.time()}")
        
        customer_data = CUSTOMER_CARDS.get(cnic, {})
        customer_name = customer_data.get("full_name", "Customer")
        
        print(f"✅ transfer_to_agent: Transfer initiated for {customer_name} @ {time.time()}")
        return {
            "success": True,
            "transfer_initiated": True,
            "reason": reason,
            "message": f"Transferring {customer_name} to human agent. Reason: {reason}"
        }
        
    except Exception as e:
        print(f"⚠️ transfer_to_agent error: {str(e)} @ {time.time()}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred during agent transfer."
        }


async def get_customer_status(cnic: str) -> dict:
    try:
        customer_data = CUSTOMER_CARDS.get(cnic)
        if not customer_data:
            return {
                "success": False,
                "error": "Customer not found"
            }
        
        return {
            "success": True,
            "customer_name": customer_data["full_name"],
            "cnic": customer_data["cnic"],
            "tpin_verified": customer_data["tpin_verified"],
            "physical_custody_confirmed": customer_data["debit_card"]["physical_custody_confirmed"],
            "card_activated": customer_data["debit_card"]["is_activated"],
            "verification_attempts": customer_data["verification_attempts"]
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


async def reset_verification_attempts(cnic: str) -> dict:
    try:
        customer_data = CUSTOMER_CARDS.get(cnic)
        if not customer_data:
            return {
                "success": False,
                "error": "Customer not found"
            }
        
        customer_data["verification_attempts"] = {
            "cnic": 0,
            "tpin": 0,
            "card_details": 0
        }
        
        return {
            "success": True,
            "message": f"Verification attempts reset for {customer_data['full_name']}"
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }
