from datetime import datetime, timedelta

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _positive(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number <= 0:
        raise ValidationError(field + " must be positive")
    return number


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")


def _validate_inspection(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("maintenance requires equipment")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")


def _validate_alarm(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("alarm requires equipment")
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ("closed", "false_alarm")
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] == "closed":
        raise ValidationError("rescue_job requires an active alarm")
    key = data.get("dedupe_key")
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
            raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _validate_permit(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


ALARM_DONE_STATUSES = ("closed", "false_alarm")


def _equipment_open_alarms(lookup, equipment_id):
    return [
        alarm
        for alarm in _all(lookup, "alarm")
        if alarm["data"].get("equipment_id") == equipment_id
        and alarm["status"] not in ALARM_DONE_STATUSES
    ]


def _equipment_blockers(lookup, equipment_id):
    """Reasons why an equipment cannot currently support a return-to-service permit."""
    blockers = []
    equipment = _find_one(lookup, "equipment", "id", equipment_id)
    if not equipment:
        blockers.append({"type": "equipment_missing", "reason": "设备不存在"})
        return blockers
    if equipment["status"] != "in_service":
        blockers.append(
            {"type": "equipment_status", "reason": "设备状态为%s，恢复许可需重新复核" % equipment["status"]}
        )
    for alarm in _equipment_open_alarms(lookup, equipment_id):
        blockers.append(
            {
                "type": "open_alarm",
                "entity_id": alarm["id"],
                "reason": "设备存在未关闭报警（%s）" % alarm["data"].get("code", alarm["id"]),
            }
        )
    for remediation in _all(lookup, "remediation"):
        if (
            remediation["data"].get("equipment_id") == equipment_id
            and remediation["status"] != "closed"
        ):
            blockers.append(
                {
                    "type": "open_remediation",
                    "entity_id": remediation["id"],
                    "reason": "存在未关闭整改（%s）" % remediation["data"].get("issue", remediation["id"]),
                }
            )
    passed = [
        inspection
        for inspection in _all(lookup, "inspection")
        if inspection["data"].get("equipment_id") == equipment_id
        and inspection["status"] == "passed"
    ]
    if not passed:
        blockers.append({"type": "missing_inspection", "reason": "缺少已通过的检验"})
    return blockers


def _require_no_open_alarm(label):
    """Gate for inspection/maintenance results: an unclosed alarm blocks them."""

    def _check(actor, entity, data, lookup):
        alarms = _equipment_open_alarms(lookup, entity["data"].get("equipment_id"))
        if alarms:
            raise ConflictError("设备存在未关闭报警，%s结果暂不生效" % label)
        return {}

    return _check


def _grant_permit(actor, entity, data, lookup):
    blockers = _equipment_blockers(lookup, entity["data"].get("equipment_id"))
    if blockers:
        raise ConflictError(blockers[0]["reason"])
    return {"granted_by": actor.user_id, "granted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}


def _complete_rescue(actor, entity, data, lookup):
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if not jobs or any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    return {"resolved_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit", "offline_records": "offline_record",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "dispatched", "remediation": "open",
        "permit": "blocked",
        "offline_record": "received",
    }
    OFFLINE_RECORD_STATUSES = (
        "received", "applied", "stale", "held", "rejected",
    )
    TRANSITIONS = {
        "equipment": {
            "suspend": (("in_service",), "suspended"),
            "out_of_service": (("in_service", "suspended"), "out_of_service"),
            "return_to_service": (("suspended",), "in_service"),
        },
        "inspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "reschedule": (("failed",), "scheduled"),
        },
        "maintenance": {
            "start": (("planned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
        },
        "alarm": {
            "dispatch": (("received",), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
        },
        "rescue_job": {
            "arrive": (("dispatched",), "on_site"),
            "complete": (("on_site",), "completed"),
            "abort": (("dispatched", "on_site"), "aborted"),
        },
        "remediation": {
            "submit_evidence": (("open",), "evidence_submitted"),
            "verify": (("evidence_submitted",), "verified"),
            "reject": (("evidence_submitted",), "open"),
            "close": (("verified",), "closed"),
        },
        "permit": {
            "request_review": (("blocked",), "pending_review"),
            "grant": (("pending_review",), "granted"),
            "revoke": (("granted", "pending_review"), "revoked"),
            "expire": (("granted",), "expired"),
            "return_to_review": (("granted",), "pending_review"),
        },
        "offline_record": {
            "mark_applied": (("received", "held"), "applied"),
            "mark_stale": (("received", "held"), "stale"),
            "mark_held": (("received",), "held"),
            "mark_rejected": (("received", "held"), "rejected"),
        },
    }
    CREATE_REQUIRED = {
        "equipment": ("asset_no", "equipment_type", "location", "inspection_interval_days"),
        "inspection": ("equipment_id", "scheduled_at", "cycle_days"),
        "maintenance": ("equipment_id", "work_type", "planned_at"),
        "alarm": ("equipment_id", "code", "occurred_at"),
        "rescue_job": ("alarm_id", "dedupe_key", "team"),
        "remediation": ("issue", "owner", "due_at"),
        "permit": ("equipment_id", "purpose", "requested_by"),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
        ("permit", "revoke"): ("reason",),
    }
    CREATE_ROLES = {
        "equipment": ("admin", "inspector"),
        "inspection": ("admin", "inspector"),
        "maintenance": ("admin", "maintenance"),
        "alarm": ("admin", "dispatcher", "inspector"),
        "rescue_job": ("admin", "dispatcher"),
        "remediation": ("admin", "inspector", "maintenance"),
        "permit": ("admin", "inspector"),
    }
    ROLE_ACTIONS = {
        "suspend": ("admin", "inspector"),
        "out_of_service": ("admin", "inspector"),
        "return_to_service": ("admin", "inspector"),
        "pass": ("admin", "inspector"),
        "fail": ("admin", "inspector"),
        "reschedule": ("admin", "inspector"),
        "start": ("admin", "maintenance"),
        ("maintenance", "complete"): ("admin", "maintenance", "inspector"),
        ("rescue_job", "complete"): ("admin", "maintenance", "dispatcher"),
        "complete": ("admin", "maintenance", "dispatcher"),
        "dispatch": ("admin", "dispatcher"),
        "mark_false": ("admin", "dispatcher", "inspector"),
        "resolve": ("admin", "dispatcher"),
        "close": ("admin", "dispatcher", "inspector"),
        "arrive": ("admin", "dispatcher"),
        "abort": ("admin", "dispatcher"),
        "submit_evidence": ("admin", "maintenance", "inspector"),
        "verify": ("admin", "inspector"),
        "reject": ("admin", "inspector"),
        "request_review": ("admin", "inspector"),
        "grant": ("admin", "inspector"),
        "revoke": ("admin", "inspector"),
        ("permit", "return_to_review"): ("admin", "inspector"),
        "expire": ("admin", "inspector"),
    }
    OFFLINE_KINDS = ("inspection", "maintenance")
    CUSTOM_CREATE = {
        "equipment": lambda a, d, l: _validate_equipment(d, l),
        "inspection": lambda a, d, l: _validate_inspection(d, l),
        "maintenance": lambda a, d, l: _validate_maintenance(d, l),
        "alarm": lambda a, d, l: _validate_alarm(d, l),
        "rescue_job": lambda a, d, l: _validate_rescue(d, l),
        "remediation": lambda a, d, l: _validate_remediation(d, l),
        "permit": lambda a, d, l: _validate_permit(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("permit", "grant"): _grant_permit,
        ("remediation", "verify"): _verify_remediation,
        ("alarm", "close"): _complete_rescue,
        ("inspection", "pass"): _require_no_open_alarm("检验"),
        ("inspection", "fail"): _require_no_open_alarm("检验"),
        ("maintenance", "complete"): _require_no_open_alarm("维保"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def validate_offline_record(self, raw):
        """Shape-check one offline inspection/maintenance record."""
        if not isinstance(raw, dict):
            raise ValidationError("each offline record must be an object")
        kind = self.normalize_kind(str(raw.get("kind", "")))
        if kind not in self.OFFLINE_KINDS:
            raise ValidationError("offline record kind must be one of: %s" % ",".join(self.OFFLINE_KINDS))
        target_id = str(raw.get("target_id", "")).strip()
        if not target_id:
            raise ValidationError("target_id is required")
        action = str(raw.get("action", "")).strip()
        if action not in self.TRANSITIONS.get(kind, {}):
            raise ValidationError("unknown offline action %s for %s" % (action, kind))
        base_version = raw.get("base_version")
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
            raise ValidationError("base_version must be a positive integer")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise ValidationError("payload must be an object")
        return kind, target_id, action, base_version, dict(payload)

    def intended_next_status(self, kind, action):
        return self.TRANSITIONS[kind][action][1]

    def offline_diff(self, kind, target, action, payload):
        """List differences between an offline record and the center's current copy."""
        diffs = []
        center_status = target["status"]
        intended_status = self.intended_next_status(kind, action)
        if center_status != intended_status:
            diffs.append(
                {"field": "status", "center": center_status, "offline": intended_status}
            )
        for key, offline_value in sorted(payload.items()):
            if key in ("target_id", "base_version"):
                continue
            center_value = target["data"].get(key)
            if center_value != offline_value:
                diffs.append({"field": "data." + key, "center": center_value, "offline": offline_value})
        return diffs

    def apply_system_transition(self, entity, action, patch, lookup=None):
        """Transition used by the system itself (no role gate), e.g. permit recompute."""
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        merged = dict(entity["data"])
        merged.update(patch or {})
        return next_status, merged
