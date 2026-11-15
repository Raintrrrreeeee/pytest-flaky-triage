"""SECTION 4: Runtime cause disambiguator (`disambiguate_causes`)."""

import re

from report.ast_utils import _dedup_preserve_order

ALL_CAUSE_CATEGORIES = (
    "unseeded_randomness",
    "race_conditions",
    "hash_order_sensitivity",
    "shared_state_interference",
    "timezone_sensitivity",
    "resource_leak",
    "external_io",
    "float_precision",
)

_AST_CATEGORY_REASONS = {
    "resource_leak": "Outcomes vary across runs or test orders due to unisolated file or port resources.",
    "external_io": "Outcomes vary under repeated runs due to unmocked external network I/O.",
    "float_precision": "Outcomes vary across hash seeds or repeated runs due to strict floating-point equality assertions.",
}


def _with_polluter_suffix(new_base_reason, original_reason):
    if " Suspected polluting predecessor(s): " in (original_reason or ""):
        _, p_part = original_reason.split(" Suspected polluting predecessor(s): ", 1)
        return f"{new_base_reason} Suspected polluting predecessor(s): {p_part}"
    return new_base_reason


def _is_weak_uuid_only(rand_hits):
    """Return True if all unseeded_randomness hits are solely uuid1/uuid4 calls (e.g., request trace IDs)."""
    if not rand_hits:
        return False
    for hit in rand_hits:
        detail = hit.get("detail", "")
        head_part = detail.split(" without a fixed seed")[0]
        calls = re.findall(r"`([^`]+)`", head_part)
        if not calls or not all(c in ("uuid.uuid4()", "uuid.uuid1()", "uuid4()", "uuid1()") for c in calls):
            return False
    return True


def _is_weak_sleep_only(race_hits):
    """Return True if all race_conditions hits are solely standalone sleep calls without worker threads/tasks."""
    if not race_hits:
        return False
    for hit in race_hits:
        detail = hit.get("detail", "")
        head_part = detail.split(" (via ")[0]
        calls = re.findall(r"`([^`]+)`", head_part)
        if not calls or not all(c in ("time.sleep", "sleep") for c in calls):
            return False
    return True


def disambiguate_causes(suspected_causes, source_info):
    """Combine runtime suspected_causes with AST heuristic hints.

    When source inspection is unavailable or no matching AST indicator is found,
    fault_side remains 'Undetermined' (never guessed from a typical default).
    When AST indicators exist only for categories other than the runtime
    candidates, they are preserved in `conflicting_hints` without altering
    `resolved_candidates`.
    """
    if not suspected_causes:
        return {
            "resolved_candidates": [],
            "runtime_candidates": [],
            "fault_side": "N/A",
            "heuristic_notes": [],
            "compact_hints": [],
            "conflicting_hints": [],
            "basis": "—",
            "resolution": "none",
            "disambiguated": False,
            "refined": False,
        }

    indicators = (source_info or {}).get("indicators", {})
    resolved = []
    sides = []
    notes = []
    compact_hints = []
    conflicting_hints = []
    disambiguated = False
    refined = False
    has_ambiguous = False
    all_runtime_cands = []
    has_strong_ext_io = bool(indicators.get("external_io"))

    for group in suspected_causes:
        candidates = list(group.get("runtime_candidates") or group.get("candidates", []))
        orig_status = group.get("runtime_status") or group.get(
            "status", "ambiguous" if len(candidates) > 1 else "suspected"
        )
        orig_reason = group.get("runtime_reason") or group.get("reason", "")
        all_runtime_cands.extend(candidates)

        if len(candidates) > 1:
            matched_cands = []
            for c in candidates:
                c_hits = indicators.get(c, [])
                if not c_hits:
                    continue
                if has_strong_ext_io and c == "unseeded_randomness" and _is_weak_uuid_only(c_hits):
                    continue
                if has_strong_ext_io and c == "race_conditions" and _is_weak_sleep_only(c_hits):
                    continue
                matched_cands.append(c)

            if 0 < len(matched_cands) < len(candidates):
                resolved.extend(matched_cands)
                disambiguated = True
                group["runtime_candidates"] = list(candidates)
                group["runtime_status"] = orig_status
                group.setdefault("runtime_reason", orig_reason)
                group["candidates"] = list(matched_cands)
                group["status"] = "ambiguous" if len(matched_cands) > 1 else "suspected"
                group["resolution"] = "disambiguated"
                group["disambiguated"] = True
                group["refined"] = False
                group["reason"] = orig_reason
                for cat in matched_cands:
                    for hit in indicators.get(cat, []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`{cat}`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
            elif len(matched_cands) == 0:
                ext_matched = [
                    c for c in ("external_io", "resource_leak", "float_precision")
                    if indicators.get(c)
                ]
                if ext_matched:
                    chosen = [ext_matched[0]]
                    resolved.extend(chosen)
                    disambiguated = True
                    group["runtime_candidates"] = list(candidates)
                    group["runtime_status"] = orig_status
                    group.setdefault("runtime_reason", orig_reason)
                    group["candidates"] = list(chosen)
                    group["status"] = "suspected"
                    group["resolution"] = "disambiguated"
                    group["disambiguated"] = True
                    group["refined"] = False
                    group["reason"] = _with_polluter_suffix(
                        _AST_CATEGORY_REASONS[chosen[0]], orig_reason
                    )
                    for cat in chosen:
                        for hit in indicators.get(cat, []):
                            sides.append(hit["side"])
                            notes.append(
                                f"Heuristic hint (`{cat}`, {hit['side']}): "
                                f"`{hit['location']}` {hit['detail']}."
                            )
                            compact_hints.append(f"`{hit['location']}` {hit['detail']}")
                else:
                    has_ambiguous = True
                    resolved.extend(candidates)
                    group["candidates"] = list(candidates)
                    group["status"] = orig_status
                    group["reason"] = orig_reason
                    group.pop("resolution", None)
                    group.pop("disambiguated", None)
                    group.pop("refined", None)
            else:
                has_ambiguous = True
                resolved.extend(candidates)
                group["candidates"] = list(candidates)
                group["status"] = orig_status
                group["reason"] = orig_reason
                group.pop("resolution", None)
                group.pop("disambiguated", None)
                group.pop("refined", None)
                for cat in candidates:
                    for hit in indicators.get(cat, []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`{cat}`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
        else:
            if orig_status == "ambiguous":
                has_ambiguous = True
            for cand in candidates:
                hits = indicators.get(cand, [])
                if (
                    cand == "shared_state_interference"
                    and not hits
                    and indicators.get("resource_leak")
                    and (
                        not indicators.get("unseeded_randomness")
                        or _is_weak_uuid_only(indicators.get("unseeded_randomness", []))
                    )
                ):
                    resolved.append("resource_leak")
                    refined = True
                    group["runtime_candidates"] = list(candidates)
                    group["runtime_status"] = orig_status
                    group.setdefault("runtime_reason", orig_reason)
                    group["candidates"] = ["resource_leak"]
                    group["status"] = "suspected"
                    group["resolution"] = "refined"
                    group["refined"] = True
                    group["disambiguated"] = False
                    group["reason"] = _with_polluter_suffix(
                        _AST_CATEGORY_REASONS["resource_leak"], orig_reason
                    )
                    for hit in indicators.get("resource_leak", []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`resource_leak`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
                elif (
                    cand == "hash_order_sensitivity"
                    and not hits
                    and indicators.get("float_precision")
                ):
                    resolved.append("float_precision")
                    refined = True
                    group["runtime_candidates"] = list(candidates)
                    group["runtime_status"] = orig_status
                    group.setdefault("runtime_reason", orig_reason)
                    group["candidates"] = ["float_precision"]
                    group["status"] = "suspected"
                    group["resolution"] = "refined"
                    group["refined"] = True
                    group["disambiguated"] = False
                    group["reason"] = _with_polluter_suffix(
                        _AST_CATEGORY_REASONS["float_precision"], orig_reason
                    )
                    for hit in indicators.get("float_precision", []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`float_precision`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
                else:
                    resolved.append(cand)
                    group["candidates"] = list(candidates)
                    group["status"] = orig_status
                    group["reason"] = orig_reason
                    group.pop("resolution", None)
                    group.pop("disambiguated", None)
                    group.pop("refined", None)
                    for hit in hits:
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`{cand}`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")

    dedup_runtime = _dedup_preserve_order(all_runtime_cands)
    runtime_cands_set = set(dedup_runtime) | set(resolved)
    runtime_str = " / ".join(f"`{c}`" for c in dedup_runtime)

    for other_cat in ALL_CAUSE_CATEGORIES:
        if other_cat in runtime_cands_set:
            continue
        for hit in indicators.get(other_cat, []):
            conflict_msg = (
                f"Conflicting hint: `{hit['location']}` {hit['detail']} "
                f"(`{other_cat}`?, {hit['side']}), runtime suggests {runtime_str}"
            )
            conflicting_hints.append(conflict_msg)
            notes.append(f"{conflict_msg}.")

    unique_sides = _dedup_preserve_order(sides)
    fault_side = " / ".join(unique_sides) if unique_sides else "Undetermined"

    if disambiguated:
        basis = "AST Hint *"
        overall_resolution = "disambiguated"
    elif refined:
        basis = "AST Refined *"
        overall_resolution = "refined"
    elif has_ambiguous:
        basis = "Ambiguous"
        overall_resolution = "ambiguous"
    elif compact_hints:
        basis = "Runtime+AST"
        overall_resolution = "unchanged"
    elif conflicting_hints:
        basis = "Runtime (AST conflict)"
        overall_resolution = "unchanged"
    else:
        basis = "Runtime"
        overall_resolution = "unchanged"

    return {
        "resolved_candidates": _dedup_preserve_order(resolved),
        "runtime_candidates": dedup_runtime,
        "fault_side": fault_side,
        "heuristic_notes": notes,
        "compact_hints": compact_hints,
        "conflicting_hints": conflicting_hints,
        "basis": basis,
        "resolution": overall_resolution,
        "disambiguated": disambiguated,
        "refined": refined,
    }
