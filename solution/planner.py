"""Offline compliance deletion planner (core logic).

All time arithmetic uses UTC whole days: day(ts) = ts // 86400. A request
fixes the occurrence moment and the entry cutoff; only rules, record facts,
preservations and releases whose entry sequence number is <= the cutoff are
visible. See README.md for the full semantics.
"""
from __future__ import annotations

from datetime import datetime, timezone

DAY_SECONDS = 86400
MAX_OBJECTS = 16
MAX_HORIZON_DAYS = 7

# Error codes (stable, documented in README.md)
INVALID_INPUT = "INVALID_INPUT"
INVALID_CUTOFF = "INVALID_CUTOFF"
UNKNOWN_OBJECT = "UNKNOWN_OBJECT"
UNKNOWN_RECORD = "UNKNOWN_RECORD"
DUPLICATE_OBJECT = "DUPLICATE_OBJECT"
RECORD_CONFLICT = "RECORD_CONFLICT"
RULE_VERSION_CONFLICT = "RULE_VERSION_CONFLICT"
RULE_CONTRADICTION = "RULE_CONTRADICTION"
NO_APPLICABLE_RULE = "NO_APPLICABLE_RULE"
PRESERVATION_CONFLICT = "PRESERVATION_CONFLICT"
UNKNOWN_PRESERVATION = "UNKNOWN_PRESERVATION"
INVALID_DAG = "INVALID_DAG"


class PlanError(Exception):
    """Dataset- or request-level rejection carrying a stable error code."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _fail(code, message, details=None):
    raise PlanError(code, message, details)


def _day_of(ts):
    return ts // DAY_SECONDS


def _date_of_day(day):
    return datetime.fromtimestamp(day * DAY_SECONDS, tz=timezone.utc).date().isoformat()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _int_field(mapping, name, minimum=0):
    value = mapping.get(name)
    if not _is_int(value) or value < minimum:
        _fail(INVALID_INPUT, f"{name} must be an integer >= {minimum}",
              {"field": name, "value": value})
    return value


def _str_field(mapping, name):
    value = mapping.get(name)
    if not isinstance(value, str) or not value:
        _fail(INVALID_INPUT, f"{name} must be a non-empty string",
              {"field": name, "value": value})
    return value


# ---------------------------------------------------------------------------
# Dataset validation (cutoff independent)
# ---------------------------------------------------------------------------

def _check_acyclic(objects):
    color = {}  # 0/absent = white, 1 = gray, 2 = black

    def visit(oid, stack):
        color[oid] = 1
        for parent in objects[oid]["derived_from"]:
            state = color.get(parent, 0)
            if state == 1:
                _fail(INVALID_DAG, "dependency cycle detected",
                      {"cycle": stack + [oid, parent]})
            if state == 0:
                visit(parent, stack + [oid])
        color[oid] = 2

    for oid in objects:
        if color.get(oid, 0) == 0:
            visit(oid, [])


def _validate_dataset(raw):
    if not isinstance(raw, dict):
        _fail(INVALID_INPUT, "dataset must be an object")
    rules = raw.get("rules", [])
    records = raw.get("records", [])
    objects = raw.get("objects", [])
    preservations = raw.get("preservations", [])
    releases = raw.get("releases", [])
    for name, coll in (("rules", rules), ("records", records), ("objects", objects),
                       ("preservations", preservations), ("releases", releases)):
        if not isinstance(coll, list):
            _fail(INVALID_INPUT, f"dataset.{name} must be an array", {"field": name})

    norm_rules = []
    for r in rules:
        if not isinstance(r, dict):
            _fail(INVALID_INPUT, "each rule must be an object")
        norm_rules.append({
            "jurisdiction": _str_field(r, "jurisdiction"),
            "valid_from": _int_field(r, "valid_from"),
            "entry_seq": _int_field(r, "entry_seq", 1),
            "min_retention_days": _int_field(r, "min_retention_days"),
            "max_retention_days": _int_field(r, "max_retention_days"),
        })

    norm_records = []
    record_ids = set()
    for r in records:
        if not isinstance(r, dict):
            _fail(INVALID_INPUT, "each record must be an object")
        rid = _str_field(r, "record_id")
        norm_records.append({
            "record_id": rid,
            "jurisdiction": _str_field(r, "jurisdiction"),
            "entry_seq": _int_field(r, "entry_seq", 1),
        })
        record_ids.add(rid)

    norm_objects = {}
    for o in objects:
        if not isinstance(o, dict):
            _fail(INVALID_INPUT, "each object must be an object")
        oid = _str_field(o, "object_id")
        if oid in norm_objects:
            _fail(DUPLICATE_OBJECT, f"duplicate object id {oid!r}", {"object_id": oid})
        rid = _str_field(o, "record_id")
        if rid not in record_ids:
            _fail(UNKNOWN_RECORD, f"object {oid!r} references unknown record {rid!r}",
                  {"object_id": oid, "record_id": rid})
        parents = o.get("derived_from", [])
        if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
            _fail(INVALID_INPUT, "derived_from must be an array of object ids",
                  {"object_id": oid})
        norm_objects[oid] = {"object_id": oid, "record_id": rid,
                             "derived_from": list(parents)}
    for oid, o in norm_objects.items():
        for parent in o["derived_from"]:
            if parent not in norm_objects:
                _fail(UNKNOWN_OBJECT,
                      f"object {oid!r} derives from unknown object {parent!r}",
                      {"object_id": oid, "derived_from": parent})
    _check_acyclic(norm_objects)

    norm_preservations = {}
    for p in preservations:
        if not isinstance(p, dict):
            _fail(INVALID_INPUT, "each preservation must be an object")
        pid = _str_field(p, "preservation_id")
        oid = _str_field(p, "object_id")
        seq = _int_field(p, "entry_seq", 1)
        if oid not in norm_objects:
            _fail(UNKNOWN_OBJECT, f"preservation {pid!r} references unknown object {oid!r}",
                  {"preservation_id": pid, "object_id": oid})
        if pid in norm_preservations and norm_preservations[pid]["object_id"] != oid:
            _fail(PRESERVATION_CONFLICT,
                  f"preservation id {pid!r} used for two different objects",
                  {"preservation_id": pid})
        norm_preservations[pid] = {"preservation_id": pid, "object_id": oid,
                                   "entry_seq": seq}

    norm_releases = []
    for r in releases:
        if not isinstance(r, dict):
            _fail(INVALID_INPUT, "each release must be an object")
        pid = _str_field(r, "preservation_id")
        seq = _int_field(r, "entry_seq", 1)
        if pid not in norm_preservations:
            _fail(UNKNOWN_PRESERVATION,
                  f"release references unknown preservation {pid!r}",
                  {"preservation_id": pid})
        norm_releases.append({"preservation_id": pid, "entry_seq": seq})

    max_seq = max(
        [0]
        + [r["entry_seq"] for r in norm_rules]
        + [r["entry_seq"] for r in norm_records]
        + [p["entry_seq"] for p in norm_preservations.values()]
        + [r["entry_seq"] for r in norm_releases]
    )
    return {
        "rules": norm_rules,
        "records": norm_records,
        "objects": norm_objects,
        "preservations": norm_preservations,
        "releases": norm_releases,
        "max_entry_seq": max_seq,
    }


# ---------------------------------------------------------------------------
# Bi-temporal rule resolution
# ---------------------------------------------------------------------------

def _effective_rule(visible_rules, jurisdiction, occurred_day):
    """Latest revision valid at the occurrence day and visible at the cutoff.

    A rule version is identified by its valid_from UTC day. Two visible,
    applicable entries sharing a version but carrying different retention
    values are rejected (RULE_VERSION_CONFLICT).
    """
    candidates = [r for r in visible_rules
                  if r["jurisdiction"] == jurisdiction
                  and _day_of(r["valid_from"]) <= occurred_day]
    by_version = {}
    for r in candidates:
        version = _day_of(r["valid_from"])
        value = (r["min_retention_days"], r["max_retention_days"])
        if version in by_version and by_version[version] != value:
            _fail(RULE_VERSION_CONFLICT,
                  f"jurisdiction {jurisdiction!r}: conflicting values for rule "
                  f"version valid from day {version}",
                  {"jurisdiction": jurisdiction, "valid_from_day": version,
                   "values": [list(by_version[version]), list(value)]})
        by_version[version] = value
    if not candidates:
        _fail(NO_APPLICABLE_RULE,
              f"no visible rule of jurisdiction {jurisdiction!r} is valid at "
              f"the occurrence day",
              {"jurisdiction": jurisdiction, "occurred_day": occurred_day})
    best = max(candidates, key=lambda r: _day_of(r["valid_from"]))
    if best["min_retention_days"] > best["max_retention_days"]:
        _fail(RULE_CONTRADICTION,
              f"effective rule of jurisdiction {jurisdiction!r} has "
              f"min_retention_days > max_retention_days",
              {"jurisdiction": jurisdiction,
               "min_retention_days": best["min_retention_days"],
               "max_retention_days": best["max_retention_days"]})
    return best


# ---------------------------------------------------------------------------
# Plan optimizer
# ---------------------------------------------------------------------------

def _optimize(items, horizon, capacities):
    """Assign each item a plan day in 1..horizon or None (not deleted).

    Objectives, in order:
      1. maximize the number of on-time deletions (day <= deadline index);
      2. minimize the day sum, where a deleted object contributes its day and
         a non-deleted object contributes horizon+1 (not deleting is the last
         lexicographic option);
      3. lexicographically smallest plan encoded as the tuple of per-object
         values (day or horizon+1) ordered by object id.

    Hard constraints: not before the earliest day, downstream (children)
    deleted on the same or an earlier day than upstream (parents), per-day
    capacity. Same-day batches may eliminate dependencies in topological
    order, so day(child) <= day(parent) is sufficient.
    """
    if not items:
        return {}
    n = len(items)
    not_deleted = horizon + 1
    order = sorted(range(n), key=lambda i: items[i]["id"])
    ids = [items[i]["id"] for i in order]
    pos_of = {oid: p for p, oid in enumerate(ids)}
    min_day = [max(1, items[i]["earliest_idx"]) for i in order]
    deadline = [items[i]["deadline_idx"] for i in order]
    children = [[pos_of[c] for c in items[i]["children"]] for i in order]
    parents = [[] for _ in range(n)]
    for i in range(n):
        for c in children[i]:
            parents[c].append(i)

    # desc_max[i]: max min_day over i and all its downstream objects. Because a
    # same-day batch may eliminate a whole chain, object i can be deleted on day
    # d (ignoring capacity) iff d >= desc_max[i].
    desc_max = list(min_day)
    state = [0] * n

    def fill_desc(u):
        state[u] = 1
        best = min_day[u]
        for c in children[u]:
            if state[c] == 0:
                fill_desc(c)
            if desc_max[c] > best:
                best = desc_max[c]
        desc_max[u] = best

    for u in range(n):
        if state[u] == 0:
            fill_desc(u)

    # Static optimistic count of objects that can still be deleted on time.
    potential = [1 if min_day[i] <= min(horizon, deadline[i]) else 0
                 for i in range(n)]
    suffix_potential = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_potential[i] = suffix_potential[i + 1] + potential[i]

    cap_rem = list(capacities)
    assign = [0] * n  # 0 = unassigned, otherwise a day or not_deleted
    best_key = [None]
    best_assign = [None]

    def slot_lower_bound(pos, cur_sum):
        remaining = n - pos
        if remaining == 0:
            return cur_sum
        slots = []
        for d in range(1, horizon + 1):
            slots.extend([d] * cap_rem[d - 1])
        slots.sort()
        k = min(len(slots), remaining)
        return cur_sum + sum(slots[:k]) + (remaining - k) * not_deleted

    def search(pos, cur_ontime, cur_sum):
        bk = best_key[0]
        if bk is not None:
            best_ontime = -bk[0]
            if cur_ontime + suffix_potential[pos] < best_ontime:
                return
            if cur_ontime + suffix_potential[pos] == best_ontime:
                lower = slot_lower_bound(pos, cur_sum)
                if lower > bk[1]:
                    return
                if lower == bk[1]:
                    lex = bk[2]
                    for j in range(pos):
                        if assign[j] != lex[j]:
                            if assign[j] > lex[j]:
                                return
                            break
        if pos == n:
            key = (-cur_ontime, cur_sum, tuple(assign))
            if best_key[0] is None or key < best_key[0]:
                best_key[0] = key
                best_assign[0] = tuple(assign)
            return

        lo = min_day[pos]
        hi = horizon
        must_delete = False
        can_delete = True
        for p in parents[pos]:
            if assign[p] == 0 or assign[p] == not_deleted:
                continue
            must_delete = True  # a deleted upstream requires this downstream
            if assign[p] < hi:
                hi = assign[p]
        for c in children[pos]:
            if assign[c] == not_deleted:
                can_delete = False  # upstream needs all downstream deleted
            elif assign[c] != 0 and assign[c] > lo:
                lo = assign[c]
        if can_delete:
            d_lo = max(lo, desc_max[pos])
            d_hi = min(hi, horizon)
            for d in range(d_lo, d_hi + 1):
                if cap_rem[d - 1] <= 0:
                    continue
                assign[pos] = d
                cap_rem[d - 1] -= 1
                search(pos + 1,
                       cur_ontime + (1 if d <= deadline[pos] else 0),
                       cur_sum + d)
                cap_rem[d - 1] += 1
        if not must_delete:
            assign[pos] = not_deleted
            search(pos + 1, cur_ontime, cur_sum + not_deleted)
        assign[pos] = 0

    search(0, 0, 0)
    result = {}
    for i, oid in enumerate(ids):
        value = best_assign[0][i]
        result[oid] = None if value == not_deleted else value
    return result


# ---------------------------------------------------------------------------
# Request evaluation
# ---------------------------------------------------------------------------

def _run_request(ds, req, case_id):
    if not isinstance(req, dict):
        _fail(INVALID_INPUT, "request must be an object")
    request_id = _str_field(req, "request_id")
    occurred_at = _int_field(req, "occurred_at")
    cutoff = _int_field(req, "cutoff")
    today = _int_field(req, "today")
    horizon = req.get("horizon_days")
    if not _is_int(horizon) or not 1 <= horizon <= MAX_HORIZON_DAYS:
        _fail(INVALID_INPUT, "horizon_days must be an integer in 1..7",
              {"field": "horizon_days", "value": horizon})
    object_ids = req.get("object_ids")
    if not isinstance(object_ids, list) or not all(isinstance(x, str) for x in object_ids):
        _fail(INVALID_INPUT, "object_ids must be an array of strings")
    if len(object_ids) > MAX_OBJECTS:
        _fail(INVALID_INPUT, "at most 16 objects per request",
              {"count": len(object_ids)})
    if len(set(object_ids)) != len(object_ids):
        _fail(INVALID_INPUT, "duplicate object ids in request",
              {"object_ids": object_ids})
    cap = req.get("daily_capacity")
    if _is_int(cap):
        capacities = [cap] * horizon
    elif isinstance(cap, list) and len(cap) == horizon and all(_is_int(c) for c in cap):
        capacities = list(cap)
    else:
        _fail(INVALID_INPUT,
              "daily_capacity must be an integer or an array of horizon_days integers",
              {"value": cap})
    for c in capacities:
        if c < 0:
            _fail(INVALID_INPUT, "daily_capacity entries must be >= 0",
                  {"value": cap})
    if cutoff > ds["max_entry_seq"]:
        _fail(INVALID_CUTOFF, "cutoff exceeds any recorded entry sequence",
              {"cutoff": cutoff, "max_entry_seq": ds["max_entry_seq"]})
    objects = ds["objects"]
    for oid in object_ids:
        if oid not in objects:
            _fail(UNKNOWN_OBJECT, f"unknown object {oid!r}", {"object_id": oid})

    occurred_day = _day_of(occurred_at)
    today_day = _day_of(today)

    # Visible record facts; same id with conflicting visible facts is rejected.
    visible_records = {}
    for r in ds["records"]:
        if r["entry_seq"] > cutoff:
            continue
        prev = visible_records.get(r["record_id"])
        if prev is not None and prev["jurisdiction"] != r["jurisdiction"]:
            _fail(RECORD_CONFLICT,
                  f"record {r['record_id']!r} has conflicting visible versions",
                  {"record_id": r["record_id"]})
        visible_records[r["record_id"]] = r

    visible_rules = [r for r in ds["rules"] if r["entry_seq"] <= cutoff]
    released = {r["preservation_id"] for r in ds["releases"]
                if r["entry_seq"] <= cutoff}
    active_preservations = {}
    for p in ds["preservations"].values():
        if p["entry_seq"] <= cutoff and p["preservation_id"] not in released:
            active_preservations.setdefault(p["object_id"], []).append(
                p["preservation_id"])

    infos = {}
    rules_used = {}
    for oid in object_ids:
        rec = visible_records.get(objects[oid]["record_id"])
        if rec is None:
            infos[oid] = {"record_visible": False}
            continue
        rule = _effective_rule(visible_rules, rec["jurisdiction"], occurred_day)
        jurisdiction = rec["jurisdiction"]
        rules_used[jurisdiction] = {
            "jurisdiction": jurisdiction,
            "valid_from": rule["valid_from"],
            "valid_from_date": _date_of_day(_day_of(rule["valid_from"])),
            "min_retention_days": rule["min_retention_days"],
            "max_retention_days": rule["max_retention_days"],
            "entry_seq": rule["entry_seq"],
        }
        earliest_day = occurred_day + rule["min_retention_days"]
        deadline_day = occurred_day + rule["max_retention_days"]
        infos[oid] = {
            "record_visible": True,
            "jurisdiction": jurisdiction,
            "earliest_day": earliest_day,
            "deadline_day": deadline_day,
            "earliest_idx": earliest_day - today_day,
            "deadline_idx": deadline_day - today_day,
            "preserved_by": sorted(active_preservations.get(oid, [])),
        }

    children_map = {
        oid: sorted(c for c in object_ids if oid in objects[c]["derived_from"])
        for oid in object_ids
    }

    assess_memo = {}

    def assess(oid):
        """(deletable, reason, blocking path) ignoring capacity only."""
        if oid in assess_memo:
            return assess_memo[oid]
        info = infos[oid]
        if not info["record_visible"]:
            result = (False, "record_not_visible", [oid])
        elif info["preserved_by"]:
            result = (False, "preserved", [oid])
        elif info["earliest_idx"] > horizon:
            result = (False, "not_yet_eligible", [oid])
        else:
            result = (True, None, None)
            for child in children_map[oid]:
                c_ok, c_reason, c_path = assess(child)
                if not c_ok:
                    result = (False, "blocked_by_downstream", [oid] + c_path)
                    break
        assess_memo[oid] = result
        return result

    sched_items = []
    for oid in object_ids:
        ok, _, _ = assess(oid)
        if ok:
            info = infos[oid]
            sched_items.append({
                "id": oid,
                "earliest_idx": info["earliest_idx"],
                "deadline_idx": info["deadline_idx"],
                "children": children_map[oid],
            })
    assignment = _optimize(sched_items, horizon, capacities)

    not_deleted = horizon + 1
    objects_out = {}
    day_sum = 0
    scheduled_day_sum = 0
    on_time_count = 0
    by_day = {}
    overdue_objects = []
    for oid in sorted(object_ids):
        info = infos[oid]
        entry = {"object_id": oid, "record_id": objects[oid]["record_id"]}
        if not info["record_visible"]:
            entry.update({"status": "record_not_visible", "jurisdiction": None,
                          "earliest_day": None, "deadline_day": None,
                          "overdue": None, "required_within_horizon": None,
                          "day": None, "on_time": None})
            objects_out[oid] = entry
            day_sum += not_deleted
            continue
        ok, reason, path = assess(oid)
        overdue = info["deadline_idx"] < 1
        if overdue:
            overdue_objects.append(oid)
        entry.update({
            "jurisdiction": info["jurisdiction"],
            "earliest_day": {"seconds": info["earliest_day"] * DAY_SECONDS,
                             "date": _date_of_day(info["earliest_day"])},
            "deadline_day": {"seconds": info["deadline_day"] * DAY_SECONDS,
                             "date": _date_of_day(info["deadline_day"])},
            "overdue": overdue,
            "required_within_horizon": info["deadline_idx"] <= horizon,
        })
        day = assignment.get(oid)
        if ok and day is not None:
            abs_day = today_day + day
            on_time = day <= info["deadline_idx"]
            entry.update({"status": "scheduled", "day": day,
                          "delete_day": {"seconds": abs_day * DAY_SECONDS,
                                         "date": _date_of_day(abs_day)},
                          "on_time": on_time})
            by_day.setdefault(str(day), []).append(oid)
            day_sum += day
            scheduled_day_sum += day
            if on_time:
                on_time_count += 1
        else:
            day_sum += not_deleted
            if ok:
                entry.update({"status": "not_selected", "day": None,
                              "on_time": None})
            else:
                entry.update({"status": reason, "day": None, "on_time": None,
                              "blocking_path": path})
                if reason == "preserved":
                    entry["active_preservations"] = info["preserved_by"]
            if info["deadline_idx"] <= horizon:
                entry["deadline_missed"] = True
        objects_out[oid] = entry

    return {
        "case_id": case_id,
        "request_id": request_id,
        "status": "ok",
        "plan": {
            "on_time_count": on_time_count,
            "scheduled_count": sum(len(v) for v in by_day.values()),
            "day_sum": day_sum,
            "scheduled_day_sum": scheduled_day_sum,
            "not_deleted_penalty": not_deleted,
            "by_day": by_day,
            "overdue_objects": overdue_objects,
        },
        "objects": objects_out,
        "audit": {
            "cutoff": cutoff,
            "max_entry_seq": ds["max_entry_seq"],
            "occurred_at": occurred_at,
            "occurred_date": _date_of_day(occurred_day),
            "today": today,
            "today_date": _date_of_day(today_day),
            "horizon_days": horizon,
            "daily_capacity": capacities,
            "visible": {
                "rules": len(visible_rules),
                "records": len(visible_records),
                "preservations": sum(1 for p in ds["preservations"].values()
                                     if p["entry_seq"] <= cutoff),
                "releases": len(released),
            },
            "rules_used": rules_used,
        },
    }


def _error_result(case_id, request_id, err):
    return {
        "case_id": case_id,
        "request_id": request_id,
        "status": "error",
        "error": {"code": err.code, "message": err.message,
                  "details": err.details},
    }


def run_case(case):
    """Evaluate every request of one case; returns a list of result dicts."""
    if not isinstance(case, dict):
        return [_error_result(None, None,
                              PlanError(INVALID_INPUT, "case must be an object"))]
    case_id = case.get("case_id")
    requests = case.get("requests", [])
    if not isinstance(requests, list):
        requests = []
    try:
        dataset = _validate_dataset(case.get("dataset"))
    except PlanError as err:
        if requests:
            return [_error_result(case_id,
                                  r.get("request_id") if isinstance(r, dict) else None,
                                  err)
                    for r in requests]
        return [_error_result(case_id, None, err)]
    results = []
    for req in requests:
        rid = req.get("request_id") if isinstance(req, dict) else None
        try:
            results.append(_run_request(dataset, req, case_id))
        except PlanError as err:
            results.append(_error_result(case_id, rid, err))
    return results


def run_document(doc):
    """Evaluate a whole cases document; returns {"results": [...]}."""
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise PlanError(INVALID_INPUT,
                        "top level must be an object with a 'cases' array")
    results = []
    for case in doc["cases"]:
        results.extend(run_case(case))
    return {"results": results}
