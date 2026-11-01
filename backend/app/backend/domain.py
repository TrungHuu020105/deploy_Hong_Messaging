"""Quantity rules ported from SO_LUONG_HAT_NGAY_TUAN.ipynb, cells 27/29.

No API calls, database writes, scheduling or message sending in this module.
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

GRADES = {"1820": "BI", "2123": "TRUNG", "2425": "DAI", "2632": "VIP"}
GRADE_LABELS = {"BI": "Bi · 18–20 mm", "TRUNG": "Trung · 21–23 mm", "DAI": "Đại · 24–25 mm", "VIP": "VIP · 26–32 mm"}
TIERS = [1, 2, 3, 5, 10, 50, 100]
PRICE_TABLE = {
    "BI": [170000, 165000, 160000, 155000, 150000, 145000, 140000],
    "TRUNG": [190000, 185000, 180000, 175000, 170000, 165000, 160000],
    "DAI": [200000, 195000, 190000, 185000, 180000, 175000, 170000],
    "VIP": [230000, 220000, 215000, 210000, 205000, 200000, 200000],
}
COMBOS = {
    "CB_HATDIEURANGMUOI_500GR_MACCAVO2123_HN_1000GR": ["LE_HATDIEURANGMUOI_HT_500GR", "LE_MACCAVO2123_HN_1000GR"],
    "CB_HATDIEURANGMUOI_500GR_MACCAVO2425_HN_1000GR": ["LE_HATDIEURANGMUOI_HT_500GR", "LE_MACCAVO2425_HN_1000GR"],
    "CB_HANHNHAN_500GR_MACCAVO2123_HN_1000GR": ["LE_HANHNHAN_HG_500GR", "LE_MACCAVO2123_HN_1000GR"],
    "CB_HANHNHAN_500GR_MACCAVO2425_HN_1000GR": ["LE_HANHNHAN_HG_500GR", "LE_MACCAVO2425_HN_1000GR"],
    "CB_MACCAVO2425_HATDIEURANGMUOI_1000GR": ["LE_HATDIEURANGMUOI_HT_500GR", "LE_MACCAVO2425_HN_500GR"],
}
REASONS = {
    "eligible": "Đạt điều kiện",
    "other_nut": "Có loại hạt khác hoặc combo",
    "no_macadamia": "Không thuộc 4 loại mắc ca trong bảng",
    "unknown_sku": "SKU chưa phân loại được",
    "unknown_weight": "Thiếu quy cách khối lượng hoặc số gói",
    "packaging": "Quy cách bao bì ngoài bảng giá",
    "missing_amount": "Thiếu tiền đơn để so giá",
    "missing_line_price": "Cần giá riêng cho từng loại mắc ca",
    "below_price": "Giá bán chưa cao hơn bảng giá",
    "non_exact_tier": "Khối lượng không có mốc giá tương ứng",
    "missing_date": "Thiếu ngày nhận hàng",
    "not_received": "Trạng thái cuối chưa bằng 3",
    "wrong_source": "Nguồn đơn không phải Tiktok",
    "expired": "Đã quá 15 ngày",
    "invalid_username": "Tên tài khoản TikTok không hợp lệ",
    "opted_out": "Khách từ chối nhận tin",
}


def decimal(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def classify_sku(sku):
    sku = str(sku or "").strip().upper()
    packaging = sku.split("_")[-2] if len(sku.split("_")) >= 2 else ""
    for token, group in (("HATDIEURANGMUOI", "HATDIEU"), ("HANHNHAN", "HANHNHAN"),
                         ("MACCANHANNGUYEN", "NHANNGUYEN"), ("MACCANHANVO", "NHANVO"),
                         ("MACCAVOBATU", "MACCAVOBATU")):
        if token in sku:
            return {"group": group, "grade": None, "packaging": packaging, "loai": f"{group}_{packaging}_500GR"}
    for size, grade in GRADES.items():
        if f"MACCACUA{size}" in sku or f"MACCAVO{size}" in sku:
            return {"group": "MACCA", "grade": grade, "packaging": packaging, "loai": f"{grade}_{packaging}_500GR"}
    return {"group": "UNKNOWN", "grade": None, "packaging": packaging, "loai": "CHUA_PHAN_LOAI"}


def kg_from_sku(sku):
    match = re.search(r"_(\d+(?:\.\d+)?)(GR|KG)$", str(sku or "").strip().upper())
    if not match:
        return None
    amount = Decimal(match[1])
    return amount / 1000 if match[2] == "GR" else amount


def expand_items(items):
    """One purchased combo repeats each component's package quantity exactly once."""
    expanded = []
    for item in items:
        sku = str(item.get("sku") or "").strip().upper()
        quantity = decimal(item.get("quantity"))
        valid_quantity = quantity is not None and quantity > 0 and quantity == quantity.to_integral_value()
        for part in COMBOS.get(sku, [sku]):
            kg = kg_from_sku(part)
            total_kg = kg * quantity if kg is not None and valid_quantity else None
            expanded.append({
                **classify_sku(part), "sku": part, "parent_sku": sku,
                "combo": sku in COMBOS or sku.startswith("CB_"),
                "quantity": float(quantity) if valid_quantity else None,
                "kg_each": float(kg) if kg is not None else None,
                "total_kg": float(total_kg) if total_kg is not None else None,
                "units_500g": float(total_kg*2) if total_kg is not None else None,
                "unit_price": item.get("unit_price"), "total_discount": item.get("total_discount"),
                "line_no": item.get("line_no"),
            })
    return expanded


def tier_for(weight, mode="floor"):
    weight = decimal(weight)
    if weight is None or weight <= 0:
        return None
    if mode == "exact":
        return int(weight) if weight in [Decimal(n) for n in TIERS] else None
    # Sub-kilogram packages use the 1 kg rate, prorated to their actual weight.
    return max((n for n in TIERS if Decimal(n) <= weight), default=1)


def price_rules_hash(rules):
    import hashlib
    import json
    normalized = {"prices":PRICE_TABLE,"packaging":["HN","HG","TZ"],
        "packaging_multipliers":{"HN":1,"HG":1.1,"TZ":1.1},"tier_mode":"floor",
        "match_mode":"all","exclude_mixed":True,**rules}
    return hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(",",":" )).encode()).hexdigest()


def evaluate_price(order, items, rules):
    """Strictly greater, never >=. Mixed products never borrow the entire order total."""
    parts = expand_items(items)
    result = {"eligible": False, "reason": "no_macadamia", "items": parts, "price_checks": [],
              "total_kg": 0.0, "units_500g": 0.0, "threshold_amount": None}
    if not parts:
        return result
    mac = [p for p in parts if p["grade"]]
    other = [p for p in parts if p["group"] != "MACCA"]
    if not mac:
        return result
    if any(p["combo"] for p in parts) or other:
        if rules.get("exclude_mixed", True):
            result["reason"] = "other_nut"
            return result
        # The notebook splits quantities, not money. No invented combo allocation.
        if any(p["combo"] for p in parts):
            result["reason"] = "missing_line_price"
            return result
    if any(p["total_kg"] is None for p in mac):
        result["reason"] = "unknown_weight"
        return result
    if any(p["packaging"] not in rules.get("packaging", ["HN","HG","TZ"]) for p in mac):
        result["reason"] = "packaging"
        return result
    groups = defaultdict(list)
    for p in mac:
        groups[p["grade"]].append(p)
    result["total_kg"] = sum(p["total_kg"] for p in mac)
    result["units_500g"] = sum(p["units_500g"] for p in mac)
    table = rules.get("prices", PRICE_TABLE)
    checks = []
    for grade, lines in groups.items():
        kg = sum((decimal(p["total_kg"]) for p in lines), Decimal(0))
        tier = tier_for(kg,rules.get("tier_mode", "floor"))
        if tier is None:
            result["reason"] = "non_exact_tier"
            return result
        base_rate = decimal(table[grade][TIERS.index(tier)])
        multipliers = rules.get("packaging_multipliers", {"HN":1,"HG":1.1,"TZ":1.1})
        threshold = sum((base_rate*decimal(multipliers[p["packaging"]])*decimal(p["total_kg"]) for p in lines),Decimal(0))
        rate = threshold/kg
        if len(groups) == 1 and not other:
            money, price_source = decimal(order.get("amount")), "order_amount"
        else:
            if any(decimal(p["unit_price"]) is None or decimal(p["total_discount"]) is None for p in lines):
                result["reason"] = "missing_line_price"
                return result
            money = sum((decimal(p["unit_price"])*decimal(p["quantity"])-decimal(p["total_discount"]) for p in lines),Decimal(0))
            price_source = "item_price_after_discount"
        if money is None:
            result["reason"] = "missing_amount"
            return result
        checks.append({"grade":grade,"kg":float(kg),"tier_kg":tier,"rate":float(rate),
                       "actual_rate":float(money/kg),"actual_amount":float(money),
                       "threshold_amount":float(threshold),"passed":money > threshold,"source":price_source})
    result["price_checks"] = checks
    result["threshold_amount"] = sum(c["threshold_amount"] for c in checks)
    passed = all(c["passed"] for c in checks) if rules.get("match_mode","all") == "all" else any(c["passed"] for c in checks)
    result.update(eligible=passed, reason="eligible" if passed else "below_price")
    return result
