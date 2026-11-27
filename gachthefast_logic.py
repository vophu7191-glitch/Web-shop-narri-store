def _safe_int(value):
    """Convert integer-like values safely without accepting NaN or infinity."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        try:
            number = float(value)
            if not number.is_integer():
                return int(number)
            return int(number)
        except (TypeError, ValueError, OverflowError):
            return 0


def calculate_credit(face_value, target_discount_pct, gtf_amount):
    """Return customer credit and shop margin from face value and GTF net payout."""
    face_value = _safe_int(face_value)
    gtf_amount = _safe_int(gtf_amount)
    target_discount_pct = float(target_discount_pct)
    if face_value <= 0 or not (0 <= target_discount_pct < 100):
        raise ValueError("invalid_face_value_or_target_discount")
    credited_amount = round(face_value * (100 - target_discount_pct) / 100)
    return credited_amount, gtf_amount - credited_amount


def claim_pending_topup(collection, topup_id, processing_started_at=None):
    """Atomically let only one callback move a top-up from pending to processing."""
    fields = {"status": "processing"}
    if processing_started_at is not None:
        fields["processing_started_at"] = processing_started_at
    result = collection.update_one(
        {"_id": topup_id, "status": "pending"},
        {"$set": fields},
    )
    return result.matched_count == 1
