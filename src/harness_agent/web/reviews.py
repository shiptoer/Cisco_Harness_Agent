"""Trusted approval state for one running, single-user web bridge.

Port từ shiptoer, chỉnh cho runner D (16/10):
- Gọi `run_case` với `context_sha256` ghim lúc khởi động bridge và `evaluated_at` = đồng hồ máy lúc gọi,
  giống UI vận hành (D-054, D-057).
- A6 (review 11/10): link duyệt chứa token KHÔNG vào kết quả tool MCP; chỉ `request_id`. Link in ra stderr.
- A7 (review 11/10): lỗi máy trước khi dsh khởi động (khoá workspace, cấu hình, và kết cục ERROR
  `PREFLIGHT_FAILED` mà chưa stage nào chạy dsh) đưa record về `approved` và log ra stderr; lỗi sau khi dsh
  đã chạy vẫn tiêu thụ phê duyệt (`failed`, hoặc `completed` với kết cục ERROR đã lưu; fail-closed).
"""

import hashlib
import json
import secrets
import sys
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.leak import redact_value
from harness_agent.harness.roles import role_level
from harness_agent.models import ChangeRequest
from harness_agent.runner.context_binding import ContextBindingError
from harness_agent.runner.context_check import context_violations
from harness_agent.runner.dsh import DshSetupError, LocalConfig
from harness_agent.runner.pipeline import ErrorCode, run_case
from harness_agent.runner.workspace import WorkspaceError

# Lỗi runner raise TRƯỚC khi gọi dsh: khoá/dọn workspace, cấu hình máy, hash context lệch (D-054).
_SETUP_ERRORS = (DshSetupError, WorkspaceError, ContextBindingError)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


class ReviewError(ValueError):
    """Safe message suitable for a tool or approval-page response."""


class ReviewService:
    def __init__(self, config, context, role, out_root, *, runner=run_case, clock=None):
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
        # Chỉ bridge đọc đồng hồ (như UI, D-057); giá trị vào runner qua `evaluated_at`.
        self.clock = clock or (lambda: datetime.now(UTC))
        self.origin = ""

    def _log(self, message):
        """Ghi một dòng ra stderr của bridge (terminal người vận hành); stdout là kênh MCP, không dùng."""
        print("[multi-web] " + redact_value(message, self.secrets), file=sys.stderr, flush=True)

    def _run(self, intent, run_id, **kwargs):
        return self.runner(
            LocalConfig.load(self.config),
            self.context,
            intent,
            self.role,
            self.out_root,
            run_id,
            level="B4",
            context_sha256=self.context_hash,
            evaluated_at=self.clock(),
            **kwargs,
        )

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
            result = self._run(intent, run_id, prepare_only=True)
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
                "view_token": secrets.token_urlsafe(32),
                "csrf": set(),
                "contract_path": run_dir / "operator-confirmed.json",
                "audit_path": run_dir / "operator-review.json",
                "prepare_result": payload,
            }
            with self.lock:
                self.records[request_id] = record
                self._audit(record)
            # A6: link có token chỉ dành cho người; không bao giờ nằm trong kết quả trả cho model.
            self._log(f"Review link for {request_id}: {self.review_link(request_id)}")
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
                    "Operator opens the review link printed in the bridge terminal and chooses "
                    "Approve or Reject in browser"
                )
            if "execution" in record:
                result["execution"] = record["execution"]
            return result

    def review_link(self, request_id):
        """Link trang duyệt (có token). Chỉ để in ra stderr của bridge; KHÔNG trả qua MCP (A6)."""
        with self.lock:
            record = self._record(request_id)
            return f"{self.origin}/review/{request_id}?token={record['view_token']}"

    def browser_review(self, request_id, view_token):
        """Only the local HTTP handler calls this; never expose CSRF values to MCP."""
        with self.lock:
            record = self._record(request_id)
            if not secrets.compare_digest(record["view_token"], view_token):
                raise ReviewError("Invalid review link")
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
            with self.lock:
                record = self._record(request_id)
                if record["state"] == "completed":
                    return self.status(request_id)  # Idempotent: never incur a second run on retries.
                if record["state"] != "approved":
                    raise ReviewError("Operator browser approval is required before generation")
                self._verify(record)
                if digest(record["contract_path"]) != record["contract_hash"]:
                    raise ReviewError("Approval contract changed; prepare again")
                record["state"] = "running"
                self._audit(record)
            run_id = "web-generate-" + request_id
            try:
                result = self._run(record["intent"], run_id, confirmed_intent_path=record["contract_path"])
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
            except _SETUP_ERRORS as exc:
                self._log(f"generate {request_id}: {type(exc).__name__}: {exc}")
                if self._dsh_started(run_id):
                    self._consume(record)
                    raise ReviewError(
                        "Generation failed; inspect local runner logs before preparing again"
                    ) from None
                # A7: dsh chưa chạy, model chưa được gọi: phê duyệt vẫn còn, sửa máy rồi generate lại.
                self._keep_approval(record)
            except Exception as exc:  # Fail closed: a failed run cannot silently reuse approval.
                self._log(f"generate {request_id}: {type(exc).__name__}: {exc}")
                self._consume(record)
                raise ReviewError(
                    "Generation failed; inspect local runner logs before preparing again"
                ) from None
            # Preflight hỏng là kết cục ERROR, không phải exception (`_run_stage`). Chưa stage nào chạy dsh
            # thì model chưa được gọi: giữ phê duyệt như lỗi setup ở trên (A7).
            if (
                result.status == "error"
                and result.error_code == ErrorCode.PREFLIGHT_FAILED
                and not self._dsh_started(run_id)
            ):
                self._log(f"generate {request_id}: {result.error_code}: {result.reason}")
                self._keep_approval(record)
            with self.lock:
                record["execution"] = payload
                record["state"] = "completed"
                self._audit(record)
                return self.status(request_id)
        finally:
            self.runner_lock.release()

    def _keep_approval(self, record):
        """Đưa record về `approved` rồi báo cho model (không kèm chi tiết; chi tiết đã ở stderr)."""
        with self.lock:
            record["state"] = "approved"
            self._audit(record)
        raise ReviewError(
            "Generation could not start (local setup error); approval kept. "
            "Fix the setup shown in the bridge terminal, then generate again"
        )

    def _consume(self, record):
        with self.lock:
            record["state"] = "failed"
            self._audit(record)

    def _dsh_started(self, run_id):
        """Đã có output stage của dsh (`<stage>.ndjson`) trong run-dir của lượt này chưa. Runner chỉ ghi file
        đó sau khi process dsh đã chạy; không đọc được thì coi như đã chạy (fail-closed)."""
        try:
            return any((self.out_root / run_id).glob("*.ndjson"))
        except OSError:
            return True
