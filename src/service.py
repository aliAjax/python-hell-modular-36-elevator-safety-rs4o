import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, DomainError, NotFoundError, ValidationError
from .rules import ALARM_DONE_STATUSES, RuleEngine, _equipment_blockers, _equipment_open_alarms


SYSTEM_ACTOR = Actor("system", "admin")


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        if kind == "offline_record":
            raise ValidationError("offline records can only enter the system via /api/offline-records")
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
        if kind in ("alarm", "remediation"):
            self._recompute_permits(payload.get("equipment_id"), "create:" + kind, actor)
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
        self._after_state_change(entity, updated, action, actor)
        return updated

    def _after_state_change(self, before, after, action, actor):
        kind = after["kind"]
        if kind == "equipment":
            equipment_id = after["id"]
        elif kind in ("inspection", "maintenance", "alarm", "remediation"):
            equipment_id = after["data"].get("equipment_id")
        else:
            return
        if kind == "alarm" and after["status"] in ALARM_DONE_STATUSES:
            # Alarm cleared: held field results can now take effect.
            self._retry_held(equipment_id, actor)
        self._recompute_permits(equipment_id, kind + ":" + action, actor)

    # ------------------------------------------------------------------
    # Offline record merge
    # ------------------------------------------------------------------

    @staticmethod
    def _offline_identity(source_id, record_id):
        digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
        return "offline-" + digest

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity.

        The same source record is processed exactly once. Records whose base
        version is older than the center are kept aside as ``stale`` with a
        field-level diff; the center copy is never overwritten. Results that
        arrive while the equipment has an unclosed alarm are ``held`` and
        applied once the alarm is cleared.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        results = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            entity_id = self._offline_identity(source_id, record_id)
            existing = self.repository.get_entity(entity_id)
            if existing:
                # Same source record is processed exactly once.
                results.append(existing)
                continue
            kind, target_id, action, base_version, payload = self.rules.validate_offline_record(raw)
            target = self.repository.get_entity(target_id)
            base = {
                "source_id": source_id,
                "record_id": record_id,
                "kind": kind,
                "target_id": target_id,
                "action": action,
                "base_version": base_version,
                "payload": payload,
                "merged_by": actor.user_id,
                "merged_at": _utcnow(),
            }
            if not target or self.rules.normalize_kind(target["kind"]) != kind:
                record = self._finalize_offline(
                    actor, entity_id, "rejected",
                    dict(base, reason="目标记录不存在或类型不匹配"),
                    None, target,
                )
                results.append(record)
                continue
            base["equipment_id"] = target["data"].get("equipment_id")
            if target["version"] > base_version:
                record = self._finalize_offline(
                    actor, entity_id, "stale",
                    dict(
                        base,
                        reason="记录基于版本%s，中心已到版本%s，保留中心内容" % (base_version, target["version"]),
                        center_version=target["version"],
                        diffs=self.rules.offline_diff(kind, target, action, payload),
                    ),
                    None, target,
                )
                results.append(record)
                continue
            results.append(self._apply_or_hold(actor, entity_id, base, target))
        return results

    def _apply_or_hold(self, actor, entity_id, base, target):
        """Attempt to apply one validated offline record to its target."""
        equipment_id = target["data"].get("equipment_id")
        open_alarms = _equipment_open_alarms(self._lookup, equipment_id)
        if open_alarms:
            reason = "设备存在未关闭报警，结果暂不生效，待报警关闭后重算：" + "；".join(
                alarm["data"].get("code", alarm["id"]) for alarm in open_alarms
            )
            return self._finalize_offline(
                actor, entity_id, "held",
                dict(base, reason=reason, open_alarm_ids=[alarm["id"] for alarm in open_alarms]),
                None, target,
            )
        return self._apply_offline(actor, entity_id, base, target, base["base_version"])

    def _apply_offline(self, actor, entity_id, base, target, expected_version, record=None, audit_action="apply_offline"):
        try:
            next_status, patch = self.rules.validate_transition(
                actor, target, base["action"], dict(base["payload"]), self._lookup
            )
        except DomainError as exc:
            return self._finalize_offline(
                actor, entity_id, "rejected",
                dict(base, reason="结果未通过规则校验：" + str(exc)),
                record, target,
            )
        merged = dict(target["data"])
        merged.update(patch)
        try:
            updated = self.repository.update_entity(target["id"], expected_version, next_status, merged)
        except ConflictError:
            # Center changed between merge and write: never overwrite someone else's edit.
            fresh = self.repository.get_entity(target["id"])
            return self._finalize_offline(
                actor, entity_id, "stale",
                dict(
                    base,
                    reason="并入时中心记录已被他人修改（版本%s），保留中心内容" % fresh["version"],
                    center_version=fresh["version"],
                    diffs=self.rules.offline_diff(base["kind"], fresh, base["action"], base["payload"]),
                ),
                record, fresh,
            )
        self.audit.record(
            target["id"], actor, audit_action, target["status"], updated["status"],
            {"source_id": base["source_id"], "record_id": base["record_id"], "patch": patch},
        )
        finalized = self._finalize_offline(
            actor, entity_id, "applied",
            dict(base, applied_target_version=updated["version"]),
            record, updated,
        )
        self._recompute_permits(base.get("equipment_id"), "offline:" + base["kind"], actor)
        return finalized

    def _retry_held(self, equipment_id, actor=None):
        """Re-evaluate held records for an equipment after its state changed."""
        actor = actor or SYSTEM_ACTOR
        applied = []
        for record in self.repository.list_entities(kind="offline_record", status="held"):
            if record["data"].get("equipment_id") != equipment_id:
                continue
            target = self.repository.get_entity(record["data"]["target_id"])
            base = dict(record["data"])
            if not target or self.rules.normalize_kind(target["kind"]) != record["data"]["kind"]:
                self._finalize_offline(
                    actor, record["id"], "rejected",
                    dict(base, reason="目标记录已不存在或类型变更"),
                    record, target,
                )
                continue
            if _equipment_open_alarms(self._lookup, equipment_id):
                continue
            if target["version"] != record["data"]["base_version"]:
                # Center moved on while the record sat held: keep center, list diffs.
                self._finalize_offline(
                    actor, record["id"], "stale",
                    dict(
                        base,
                        reason="重算时发现中心已更新到版本%s（记录基于版本%s），保留中心内容"
                        % (target["version"], record["data"]["base_version"]),
                        center_version=target["version"],
                        diffs=self.rules.offline_diff(
                            record["data"]["kind"], target, record["data"]["action"], record["data"]["payload"]
                        ),
                    ),
                    record, target,
                )
                continue
            applied.append(
                self._apply_offline(
                    actor, record["id"], base, target, target["version"],
                    record=record, audit_action="retry_offline",
                )
            )
        return applied

    def _finalize_offline(self, actor, entity_id, status, data, record, target):
        """Create or advance an offline record to its processing status."""
        if record is None:
            stored = self.repository.create_entity(
                entity_id, "offline_record", status, data, data.get("merged_by", "system")
            )
        else:
            action = {
                "applied": "mark_applied",
                "stale": "mark_stale",
                "held": "mark_held",
                "rejected": "mark_rejected",
            }[status]
            next_status, merged = self.rules.apply_system_transition(record, action, data, self._lookup)
            stored = self.repository.update_entity(record["id"], record["version"], next_status, merged)
        self._audit_offline(actor, stored, target)
        return stored

    def _audit_offline(self, actor, record, target):
        self.audit.record(
            record["id"], actor, "merge_offline", None, record["status"],
            {
                "source_id": record["data"].get("source_id"),
                "record_id": record["data"].get("record_id"),
                "target_id": record["data"].get("target_id"),
                "reason": record["data"].get("reason"),
                "diffs": record["data"].get("diffs", []),
                "target_version": target["version"] if target else None,
            },
        )

    # ------------------------------------------------------------------
    # Return-to-service permit recompute
    # ------------------------------------------------------------------

    def _recompute_permits(self, equipment_id, trigger, actor=None):
        """Re-evaluate granted permits for an equipment after its state changed.

        A granted permit whose preconditions no longer hold is returned to
        ``pending_review`` with an explicit reason; it must be re-approved.
        """
        if not equipment_id:
            return
        actor = actor or SYSTEM_ACTOR
        blockers = _equipment_blockers(self._lookup, equipment_id)
        if not blockers:
            return
        reason = "；".join(blocker["reason"] for blocker in blockers)
        for permit in self.repository.list_entities(kind="permit", status="granted"):
            if permit["data"].get("equipment_id") != equipment_id:
                continue
            history = list(permit["data"].get("review_history", []))
            history.append(
                {
                    "reason": reason,
                    "trigger": trigger,
                    "at": _utcnow(),
                    "blockers": blockers,
                }
            )
            patch = {
                "review_reason": reason,
                "review_trigger": trigger,
                "reviewed_at": _utcnow(),
                "review_history": history,
            }
            next_status, merged = self.rules.apply_system_transition(
                permit, "return_to_review", patch, self._lookup
            )
            updated = self.repository.update_entity(permit["id"], permit["version"], next_status, merged)
            self.audit.record(
                permit["id"], actor, "return_to_review", permit["status"], updated["status"],
                {"reason": reason, "trigger": trigger, "blockers": blockers},
            )

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
