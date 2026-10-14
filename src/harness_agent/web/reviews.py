"""Trusted approval state for one running, single-user web bridge."""

import contextlib
import hashlib
import json
import secrets
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.roles import role_level
from harness_agent.models import ChangeRequest
from harness_agent.runner.context_check import context_violations
from harness_agent.runner.dsh import DshSetupError, LocalConfig
from harness_agent.runner.pipeline import lock_workspace, run_case


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


class ReviewError(ValueError):
    """Safe message suitable for a tool or approval-page response."""


class ReviewService:
    def __init__(self, config, context, role, out_root, *, runner=run_case):
        self.config = Path(config).resolve(strict=True)
        self.context = Path(context).resolve(strict=True)
        self.role = role
        role_level(role)
        if context_violations(self.context):
            raise ReviewError("Context is invalid")
        self.context_hash = digest(self.context)
        self.config_hash = digest(self.config)
        self.model_file = LocalConfig.load(self.config).model_file
        self.model_hash = digest(self.model_file)
        self.secrets = ContextStore.load(self.context).secret_values()
        self.out_root = Path(out_root)
        self.out_root.mkdir(parents=True, exist_ok=True)
        self.records = {}
        self.lock = threading.RLock()
        self.runner_lock = threading.Lock()
        self.runner = runner
        self.origin = ""

    def _unchanged(self):
        if (
            digest(self.context) != self.context_hash
            or digest(self.config) != self.config_hash
            or digest(self.model_file) != self.model_hash
        ):
            raise ReviewError("Context/config/model changed; restart the bridge and prepare again")

    def _record(self, request_id):
        if not isinstance(request_id, str) or request_id not in self.records:
            raise ReviewError("Unknown request_id in this bridge session")
        return self.records[request_id]

    def _verify(self, record):
        self._unchanged()
        if digest(record["proposal_path"]) != record["proposal_hash"]:
            raise ReviewError("Proposal changed; prepare again")

    def _audit(self, record):
        write_json(
            record["audit_path"],
            {key: record[key] for key in ("request_id", "state", "role", "context_hash", "proposal_hash")},
        )

    def prepare(self, intent):
        if not isinstance(intent, str) or not intent.strip() or len(intent) > 12000:
            raise ReviewError("Intent must contain 1 to 12000 characters")
        if any(secret and secret in intent for secret in self.secrets):
            raise ReviewError("Intent contains a protected secret; remove it")
        if not self.runner_lock.acquire(blocking=False):
            raise ReviewError("Another request is running; wait for its result")
        try:
            self._unchanged()
            request_id = uuid4().hex
            run_id = "web-prepare-" + request_id
            result = self.runner(
                LocalConfig.load(self.config),
                self.context,
                intent,
                self.role,
                self.out_root,
                run_id,
                level="B4",
                prepare_only=True,
            )
            payload = asdict(result)
            if result.status != "pending_review":
                return {"request_id": request_id, "phase": "prepare", "result": payload}
            if result.leak_attempt_detected or result.leak_in_package or not result.leak_scan_complete:
                raise ReviewError("Preparation did not pass the leak checks; no approval available")
            self._unchanged()
            run_dir = self.out_root / run_id
            proposal_path = run_dir / "change_request.json"
            proposal = ChangeRequest.model_validate_json(proposal_path.read_text(encoding="utf-8"))
            if proposal.status != "ready" or not proposal.changes:
                raise ReviewError("No complete proposal to approve")
            metadata = json.loads((run_dir / "review.json").read_text(encoding="utf-8"))
            proposal_hash = digest(proposal_path)
            if (
                metadata.get("intent") != intent
                or metadata.get("role") != self.role
                or metadata.get("context_sha256") != self.context_hash
                or metadata.get("proposal_sha256") != proposal_hash
            ):
                raise ReviewError("Review metadata does not match this request")
            changes = [c.model_dump(mode="json") for c in proposal.changes]
            if any(secret and secret in json.dumps(changes, ensure_ascii=False) for secret in self.secrets):
                raise ReviewError("Proposal contains a protected secret; no approval available")
            record = {
                "request_id": request_id,
                "state": "pending",
                "intent": intent,
                "role": self.role,
                "context_hash": self.context_hash,
                "proposal_hash": proposal_hash,
                "proposal_path": proposal_path,
                "changes": changes,
                "csrf": set(),
                "contract_path": run_dir / "operator-confirmed.json",
                "audit_path": run_dir / "operator-review.json",
                "prepare_result": payload,
            }
            with self.lock:
                self.records[request_id] = record
                self._audit(record)
            return self.status(request_id)
        finally:
            self.runner_lock.release()

    def status(self, request_id):
        with self.lock:
            record = self._record(request_id)
            result = {
                "request_id": request_id,
                "state": record["state"],
                "role": self.role,
                "context_sha256": self.context_hash,
                "intent": record["intent"],
                "proposed_changes": record["changes"],
                "prepare_result": record["prepare_result"],
            }
            if record["state"] == "pending":
                result["next_step"] = (
                    "Operator refreshes the separate review tab opened at startup, "
                    "matches this request_id, and chooses Approve or Reject. "
                    "Never paste approval links into chat."
                )
            if "execution" in record:
                result["execution"] = record["execution"]
            return result

    def operator_requests(self):
        """Private operator view; HTTP authentication is required by the caller."""
        with self.lock:
            return [(key, record["state"]) for key, record in self.records.items()]

    def browser_review(self, request_id):
        """Trusted caller only: HTTP must authenticate the operator before calling.

        Never register this method as an MCP tool or return its CSRF through MCP.
        """
        with self.lock:
            record = self._record(request_id)
            self._verify(record)
            csrf = secrets.token_urlsafe(32)
            if len(record["csrf"]) > 20:
                record["csrf"].clear()
            record["csrf"].add(csrf)
            return self.status(request_id), csrf

    def browser_decide(self, request_id, csrf, decision):
        with self.lock:
            record = self._record(request_id)
            if csrf not in record["csrf"]:
                raise ReviewError("Invalid or expired browser form")
            if record["state"] != "pending":
                raise ReviewError("Request is no longer pending")
            if decision not in ("approve", "reject"):
                raise ReviewError("Invalid decision")
            self._verify(record)
            if decision == "approve":
                write_json(
                    record["contract_path"],
                    {
                        "intent": record["intent"],
                        "context_sha256": self.context_hash,
                        "changes": record["changes"],
                    },
                )
                record["contract_hash"] = digest(record["contract_path"])
                record["state"] = "approved"
            else:
                record["state"] = "rejected"
            record["csrf"].clear()
            self._audit(record)

    def generate(self, request_id):
        if not self.runner_lock.acquire(blocking=False):
            raise ReviewError("Another request is running; wait for its result")
        try:
            # Keep the workspace reservation through runner entry and completion.
            # run_case re-enters this same process-owned lock (D-025).
            with contextlib.ExitStack() as reservation:
                with self.lock:
                    record = self._record(request_id)
                    if record["state"] == "completed":
                        return self.status(request_id)  # Cached outcome; never run a second time.
                    if record["state"] != "approved":
                        raise ReviewError("Operator browser approval is required before generation")
                    self._verify(record)
                    if digest(record["contract_path"]) != record["contract_hash"]:
                        raise ReviewError("Approval contract changed; prepare again")
                    try:
                        cfg = LocalConfig.load(self.config)
                        reservation.enter_context(lock_workspace(cfg))
                    except (DshSetupError, OSError) as exc:
                        self._generation_diagnostic("before_start", exc)
                        raise ReviewError(
                            "Generation has not started: config/workspace unavailable; approval retained. "
                            "Operator must resolve the issue and explicitly request another attempt. "
                            "Do not retry automatically."
                        ) from None
                    record["state"] = "running"
                run_id = "web-generate-" + request_id
                try:
                    self._audit(record)
                    result = self.runner(
                        cfg,
                        self.context,
                        record["intent"],
                        self.role,
                        self.out_root,
                        run_id,
                        level="B4",
                        confirmed_intent_path=record["contract_path"],
                    )
                    payload = asdict(result)
                    package = self.out_root / run_id / "change_package.md"
                    if (
                        result.status == "ok"
                        and result.verdict == "accept"
                        and result.leak_scan_complete
                        and not result.leak_in_package
                        and package.is_file()
                    ):
                        payload["change_package"] = package.read_text(encoding="utf-8")
                    with self.lock:
                        record["execution"] = payload
                        record["state"] = "completed"
                        self._audit(record)
                        return self.status(request_id)
                except Exception as exc:
                    # After the start boundary, even a DshSetupError may follow
                    # earlier model calls. Never infer safe retry from its type/text.
                    self._generation_diagnostic("after_start", exc)
                    with self.lock:
                        record["state"] = "failed"
                        record.pop("execution", None)
                        try:
                            self._audit(record)
                        except Exception as audit_exc:
                            self._generation_diagnostic("failure_audit", audit_exc)
                    raise ReviewError(
                        "Generation failed; inspect local runner logs before preparing again"
                    ) from None
        finally:
            self.runner_lock.release()

    @staticmethod
    def _generation_diagnostic(phase, exc):
        # Exception messages/tracebacks can include paths, request data or secrets.
        # Phase + exception class identify the failure category without echoing them.
        print(f"[multi-web] generate phase={phase} error={type(exc).__name__}", file=sys.stderr, flush=True)
