import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "equipment":
            reason = "equipment status changed from %s to %s" % (entity["status"], updated["status"])
            self._recalculate_permits(actor, updated, reason)
        return updated

    def _recalculate_permits(self, actor, equipment, reason):
        """Recalculate recovery permits after an equipment status change.

        Granted permits are returned to pending_review with the reason;
        pending_review permits are left in review but their eligibility is
        re-evaluated. Nothing is auto-granted.
        """
        permits = self.repository.list_entities(kind="permit")
        recalculated = []
        for permit in permits:
            if permit["data"].get("equipment_id") != equipment["id"]:
                continue
            if permit["status"] == "granted":
                updated = self.transition(
                    actor, permit["id"], "reconsider", {"reason": reason},
                    expected_version=permit["version"],
                )
                recalculated.append({
                    "permit_id": permit["id"],
                    "status": "returned_to_review",
                    "reason": reason,
                })
            elif permit["status"] == "pending_review":
                recalculated.append({
                    "permit_id": permit["id"],
                    "status": "pending_review",
                    "eligible": self._permit_conditions_met(equipment),
                })
        return recalculated

    def _permit_conditions_met(self, equipment):
        if equipment["status"] not in ("in_service", "suspended"):
            return False
        inspections = self.repository.find_entities("inspection", "equipment_id", equipment["id"])
        if not any(i["status"] == "passed" for i in inspections):
            return False
        remediations = self.repository.find_entities("remediation", "equipment_id", equipment["id"])
        if any(r["status"] != "closed" for r in remediations):
            return False
        return True

    def merge_offline(self, actor, records):
        """Merge offline inspection/maintenance records one by one.

        Each record carries a stable (source_id, record_id) identity and a
        ``version``. The same (source, record, version) is processed at most
        once. When the source version is older than the center version the
        center content is kept and the field-level differences are reported
        instead of overwriting anyone else's changes.

        Inspection/maintenance results only take effect when the equipment has
        no unclosed alarm; otherwise the result is held with a reason.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        results = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            results.append(self._merge_one(actor, raw))
        return results

    RESULT_ACTIONS = {
        "inspection": ("pass", "fail"),
        "maintenance": ("complete",),
    }

    def _merge_one(self, actor, raw):
        source_id = str(raw.get("source_id", "")).strip()
        record_id = str(raw.get("record_id", "")).strip()
        if not source_id or not record_id:
            raise ValidationError("source_id and record_id are required")
        kind = self.rules.normalize_kind(raw.get("kind", ""))
        if kind not in self.RESULT_ACTIONS:
            raise ValidationError("offline record kind must be inspection or maintenance")
        version = self._coerce_int(raw.get("version"), "version")
        action = raw.get("action")
        actions = raw.get("actions")
        if actions is None:
            actions = [action] if action else []
        if not isinstance(actions, list):
            raise ValidationError("actions must be a list")
        data = dict(raw.get("data") or {})
        action_data = dict(raw.get("action_data") or {})

        # Idempotency: the exact (source, record, version) is processed once.
        prior = self.repository.get_offline_record(source_id, record_id, version)
        if prior:
            entity = self.repository.get_entity(prior["entity_id"])
            return {
                "source_id": source_id,
                "record_id": record_id,
                "status": "duplicate",
                "entity": entity,
                "detail": prior["detail"],
            }

        # Determine the target center entity.
        entity_id = raw.get("entity_id")
        if entity_id:
            entity = self.repository.get_entity(entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
        else:
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            entity = self.repository.get_entity(entity_id)

        result_status = None
        detail = {}
        was_created = False

        if entity is None:
            # New record: validate and create.
            was_created = True
            self.rules.validate_create(actor, kind, data, self._lookup)
            status = self.rules.initial_status(kind, data)
            entity = self.repository.create_entity(entity_id, kind, status, data, actor.user_id)
            self.audit.record(
                entity_id, actor, "merge_create", None, status,
                {"source_id": source_id, "record_id": record_id, "version": version},
            )
            result_status = "created"
        else:
            # Existing record: version check before applying anything.
            if version is not None and version < entity["version"]:
                diffs = self._compute_diffs(entity, data, actions)
                detail = {
                    "diffs": diffs,
                    "reason": "source version %s older than center version %s" % (version, entity["version"]),
                }
                self.repository.save_offline_record(
                    source_id, record_id, version, entity["id"], kind, "conflict", detail,
                )
                self.audit.record(
                    entity["id"], actor, "merge_conflict", entity["status"], entity["status"], detail,
                )
                return {
                    "source_id": source_id,
                    "record_id": record_id,
                    "status": "conflict",
                    "entity": entity,
                    "diffs": diffs,
                }
            # Apply the source field values.
            if data:
                merged = dict(entity["data"])
                merged.update(data)
                entity = self.repository.update_entity(entity["id"], entity["version"], entity["status"], merged)
                self.audit.record(
                    entity["id"], actor, "merge_update", entity["status"], entity["status"], {"data": data},
                )
            result_status = "applied"

        # Apply the ordered actions, gating result effectiveness on open alarms.
        if actions:
            equipment_id = entity["data"].get("equipment_id")
            for act in actions:
                if act in self.RESULT_ACTIONS[kind] and self._has_active_alarm(equipment_id):
                    merged = dict(entity["data"])
                    merged.update(action_data)
                    merged["held"] = True
                    merged["hold_reason"] = "equipment has unclosed alarm; result not effective"
                    entity = self.repository.update_entity(entity["id"], entity["version"], entity["status"], merged)
                    detail = {"action": act, "hold_reason": merged["hold_reason"]}
                    result_status = "held"
                    self.audit.record(
                        entity["id"], actor, "merge_hold", entity["status"], entity["status"], detail,
                    )
                    break
                entity = self.transition(
                    actor, entity["id"], act, action_data, expected_version=entity["version"],
                )
                detail = {"action": act}
            else:
                if not was_created:
                    result_status = "applied"

        self.repository.save_offline_record(
            source_id, record_id, version, entity["id"], kind, result_status, detail,
        )
        return {
            "source_id": source_id,
            "record_id": record_id,
            "status": result_status,
            "entity": entity,
            "detail": detail,
        }

    @staticmethod
    def _coerce_int(value, field):
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValidationError(field + " must be an integer")

    @staticmethod
    def _compute_diffs(entity, data, actions):
        diffs = []
        center_data = entity["data"]
        for field in sorted(set(center_data.keys()) | set(data.keys())):
            center_val = center_data.get(field)
            source_val = data.get(field)
            if center_val != source_val:
                diffs.append({"field": field, "center": center_val, "source": source_val})
        for act in actions or []:
            diffs.append({
                "field": "status",
                "center": entity["status"],
                "source": act,
                "note": "source intended action",
            })
        return diffs

    def _has_active_alarm(self, equipment_id):
        if not equipment_id:
            return False
        alarms = self.repository.find_entities("alarm", "equipment_id", equipment_id)
        return any(alarm["status"] not in ("closed", "false_alarm") for alarm in alarms)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
