"""Pure GachTheFast callback logic, kept free of Flask/MongoDB imports so it can be unit tested.

API contract (GachTheFast CARD PARTNER Postman collection):
- callback_sign = md5(partner_key + code + serial)
- declared_value = declared denomination; value = denomination used for calculation;
  amount = net amount GachTheFast pays the partner.
- status: 1 success/right denomination, 2 success/wrong denomination, 3 card error,
  4 maintenance, 99 pending, 100 submission failed.

Recovery policy for a top-up left in "processing" (e.g. the process crashed mid-way):
the first callback that claims the top-up stores its normalized result on the top-up
document. A later callback whose signed payload is identical resumes processing using that
stored result (wallet credit and history are idempotent). A later callback with a different
result never overwrites anything: the top-up is moved to "review" for manual reconciliation.
Terminal top-ups (success/failed/review) are never modified by a callback.
"""
import hashlib
import hmac
import math
from decimal import Decimal, InvalidOperation

SUCCESS_STATUSES = ("1", "2")
FAILED_STATUSES = ("3", "4", "100")
PENDING_STATUS = "99"


def parse_strict_int(value):
    """Return an int for integral numbers/strings, or None. Never truncates fractions."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
        if not number.is_finite() or number != number.to_integral_value():
            return None
        return int(number)
    return None


def calculate_credit(face_value, target_discount_pct, gtf_amount):
    """Return customer credit and shop margin from face value and GTF net payout."""
    face_value = parse_strict_int(face_value)
    gtf_amount = parse_strict_int(gtf_amount)
    target_discount_pct = float(target_discount_pct)
    if face_value is None or face_value <= 0 or not (0 <= target_discount_pct < 100):
        raise ValueError("invalid_face_value_or_target_discount")
    if gtf_amount is None:
        raise ValueError("invalid_gtf_amount")
    credited_amount = round(face_value * (100 - target_discount_pct) / 100)
    return credited_amount, gtf_amount - credited_amount


def expected_callback_sign(partner_key, code, serial):
    raw = str(partner_key) + str(code) + str(serial)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def verify_callback_sign(partner_key, data):
    """Validate callback_sign = md5(partner_key + code + serial) in constant time."""
    if not partner_key:
        return False
    code = data.get("code")
    serial = data.get("serial")
    sign = data.get("callback_sign")
    if not code or not serial or not sign or not isinstance(sign, str):
        return False
    expected = expected_callback_sign(partner_key, code, serial)
    return hmac.compare_digest(expected.lower(), sign.strip().lower())


def parse_callback_payload(json_data, form, args):
    """Same precedence as before: JSON body, then form body, then query string."""
    data = json_data if isinstance(json_data, dict) else {}
    if not data and form:
        data = dict(form)
    if not data and args:
        data = dict(args)
    return data


def normalize_callback_result(data):
    """The fields that define a callback's outcome; used to detect conflicting callbacks."""
    def money(field):
        raw = data.get(field)
        parsed = parse_strict_int(raw)
        return parsed if parsed is not None else (None if raw is None else str(raw))
    return {
        "status": str(data.get("status", "")).strip(),
        "value": money("value"),
        "amount": money("amount"),
    }


def claim_pending_topup(collection, topup_id, processing_started_at=None, callback_result=None):
    """Atomically let only one callback move a top-up from pending to processing."""
    fields = {"status": "processing"}
    if processing_started_at is not None:
        fields["processing_started_at"] = processing_started_at
    if callback_result is not None:
        fields["callback_result"] = callback_result
    result = collection.update_one(
        {"_id": topup_id, "status": "pending"},
        {"$set": fields},
    )
    return result.matched_count == 1


def credit_user_once(users_collection, user_id, topup_key, amount):
    """Atomically credit a top-up once and confirm its idempotency marker exists."""
    result = users_collection.update_one(
        {"_id": user_id, "processed_card_topup_ids": {"$ne": topup_key}},
        {"$inc": {"balance": amount},
         "$addToSet": {"processed_card_topup_ids": topup_key}},
    )
    user = users_collection.find_one(
        {"_id": user_id}, {"processed_card_topup_ids": 1}
    )
    confirmed = bool(user and topup_key in (user.get("processed_card_topup_ids") or []))
    return result.modified_count == 1, confirmed


def _noop(*_args, **_kwargs):
    return None


def handle_callback(data, *, topups, users, recharges, partner_key, discounts, now,
                    new_id, relay=None, on_success=None, on_failed=None, on_review=None):
    """Process one GachTheFast callback. Returns (json_body, http_status)."""
    on_success = on_success or _noop
    on_failed = on_failed or _noop
    on_review = on_review or _noop

    # 1. Authenticate before any top-up mutation or Discord relay.
    if not verify_callback_sign(partner_key, data):
        return {"status": "invalid_signature"}, 403

    request_id = str(data.get("request_id", ""))
    topup = topups.find_one({"request_id": request_id}) if request_id else None
    if not topup:
        # Not a website top-up -> forward to the Discord bot (GTF allows one fixed callback URL).
        if relay:
            relay(data)
        return {"status": "ignored"}, 200

    # A valid signature for another card must not settle this request_id.
    if (str(data.get("code")) != str(topup.get("code"))
            or str(data.get("serial")) != str(topup.get("serial"))):
        return {"status": "invalid_signature"}, 403

    result = normalize_callback_result(data)
    status = result["status"]
    if status == PENDING_STATUS:
        # Still processing at GTF; never claim or credit.
        return {"status": "pending"}, 200

    topup_id = topup["_id"]

    def set_if_processing(fields):
        fields["updated_at"] = now()
        return topups.update_one({"_id": topup_id, "status": "processing"}, {"$set": fields})

    def to_review(reason, **extra):
        review_fields = {"status": "review", "review_reason": reason, **extra}
        changed = set_if_processing(review_fields)
        if changed.modified_count == 1:
            # Notification failure must never change the provider callback outcome.
            try:
                on_review(topup, reason, review_fields)
            except Exception:
                pass
        return {"status": "review"}, 200

    def set_if_settling(fields):
        fields["updated_at"] = now()
        return topups.update_one(
            {"_id": topup_id, "status": "settling"},
            {"$set": fields},
        )

    # 2. Claim, or resume a stuck "processing" top-up only with an identical result.
    if not claim_pending_topup(topups, topup_id, now(), result):
        current = topups.find_one({"_id": topup_id}) or {}
        if current.get("status") != "processing":
            return {"status": "already_processed"}, 200
        stored = current.get("callback_result")
        if stored is None:
            # Claimed by older code that did not store the result: adopt this one atomically.
            topups.update_one(
                {"_id": topup_id, "status": "processing", "callback_result": None},
                {"$set": {"callback_result": result}},
            )
            current = topups.find_one({"_id": topup_id}) or {}
            stored = current.get("callback_result")
        if stored != result:
            return to_review("conflicting_callback_payload", conflicting_callback_result=result)
        topup = current

    trans_id = data.get("trans_id")

    if status in SUCCESS_STATUSES:
        face_value = parse_strict_int(data.get("value"))
        gtf_amount = parse_strict_int(data.get("amount"))
        if face_value is None or face_value <= 0:
            return to_review("successful_callback_missing_or_invalid_value",
                             value=data.get("value"), gachthefast_amount=data.get("amount"),
                             gachthefast_status=status)
        if gtf_amount is None or gtf_amount <= 0:
            return to_review("successful_callback_missing_positive_payout",
                             value=face_value, gachthefast_amount=data.get("amount"),
                             gachthefast_status=status)

        target_discount_value = (discounts or {}).get(str(topup.get("telco", "")).lower(), 0) or 0
        try:
            target_discount_pct = float(target_discount_value)
            credited_amount, shop_margin = calculate_credit(face_value, target_discount_pct, gtf_amount)
        except (TypeError, ValueError):
            return to_review("missing_face_value_or_invalid_target_discount",
                             value=face_value, gachthefast_amount=gtf_amount,
                             target_discount_pct=target_discount_value)

        if shop_margin < 0:
            return to_review("gachthefast_payout_below_customer_credit",
                             value=face_value, face_value=face_value,
                             credited_amount=credited_amount, gachthefast_amount=gtf_amount,
                             target_discount_pct=target_discount_pct, shop_margin=shop_margin)

        # Fence the callback before any irreversible side effect. A conflicting callback
        # may move "processing" to "review", but can never revoke a "settling" owner.
        settlement_claim = topups.update_one(
            {"_id": topup_id, "status": "processing", "callback_result": result},
            {"$set": {"status": "settling", "settling_started_at": now(),
                      "settlement_result": result}},
        )
        if settlement_claim.modified_count != 1:
            current = topups.find_one({"_id": topup_id}) or {}
            if current.get("status") == "settling":
                return {"status": "settlement_in_progress"}, 200
            return {"status": "already_processed"}, 200

        topup_key = str(topup_id)
        _credited_now, credit_confirmed = credit_user_once(
            users, topup["user_id"], topup_key, credited_amount
        )
        if not credit_confirmed:
            review_fields = {"status": "review", "review_reason": "wallet_credit_not_confirmed"}
            changed = set_if_settling(review_fields)
            if changed.modified_count == 1:
                try:
                    on_review(topup, "wallet_credit_not_confirmed", review_fields)
                except Exception:
                    pass
            return {"status": "review"}, 200

        recharges.update_one(
            {"source_topup_id": topup_key},
            {"$setOnInsert": {
                "_id": new_id(),
                "source_topup_id": topup_key,
                "user_id": topup["user_id"], "username": topup.get("username"),
                "amount": credited_amount, "bank": f"Thẻ cào {topup.get('telco')}",
                "source": "gachthefast", "created_at": now(),
            }},
            upsert=True,
        )
        final = set_if_settling({
            "status": "success", "value": face_value, "face_value": face_value,
            "credited_amount": credited_amount, "gachthefast_amount": gtf_amount,
            "gachthefast_status": status, "target_discount_pct": target_discount_pct,
            "shop_margin": shop_margin, "trans_id": trans_id,
        })
        # Exactly one callback performs the processing -> success transition.
        if final.modified_count == 1:
            on_success(topup, status, credited_amount, target_discount_pct)
        return {"status": "ok"}, 200

    if status in FAILED_STATUSES:
        final = set_if_processing({
            "status": "failed", "value": data.get("value"), "gachthefast_status": status,
            "trans_id": trans_id,
        })
        if final.modified_count == 1:
            on_failed(topup, status, data.get("message", ""))
        return {"status": "ok"}, 200

    return to_review("unknown_callback_status", gachthefast_status=status)


def record_processing_error(topups, request_id, error, now):
    """Keep a crashed top-up in 'processing' (recoverable) and record why."""
    if not request_id:
        return
    topups.update_one(
        {"request_id": request_id, "status": "processing"},
        {"$set": {"last_error": str(error)[:500], "last_error_at": now}},
    )
