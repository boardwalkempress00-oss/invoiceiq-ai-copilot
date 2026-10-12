"""Public demo endpoints: simulated processing of predefined fictional scenarios.

    GET  /demo/scenarios   the fixed scenario catalog
    POST /demo/runs        run one scenario through the real processing code, in memory
    GET  /demo/reference   the historical, verified Azure run shown next to demo results

All three answer 404 `feature_disabled` unless DEMO_MODE_ENABLED is set. There is no upload: a
run is identified only by a catalog scenario ID. Nothing here uses Azure storage or messaging.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter

from invoiceiq.api.routes import Container
from invoiceiq.api.schemas import (
    DemoRunOut,
    DemoRunRequest,
    DemoScenarioOut,
    DemoStepOut,
    ErrorResponse,
    SubmissionErrorOut,
)
from invoiceiq.core.models import ValidationResult
from invoiceiq.processing.demo import PROCESSING_MODE, PROVENANCE, DemoError, DemoRun, DemoService

router = APIRouter(prefix="/demo", tags=["demo"])
_ERRORS = {
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


def _service(container: Container) -> DemoService:
    if container.demo_service is None:
        raise DemoError(404, "feature_disabled", "The processing demo is not enabled.")
    return container.demo_service


def to_out(run: DemoRun) -> DemoRunOut:
    record = run.record
    start = record.created_at

    def step(name: str, at: Any) -> DemoStepOut:
        offset = (at - start).total_seconds() * 1000 if at else None
        return DemoStepOut(
            step=name, at=at, offset_ms=round(offset, 1) if offset is not None else None
        )

    result = invoice = None
    if record.result_json:
        payload = json.loads(record.result_json)
        result = ValidationResult.model_validate(payload["result"])
        invoice = payload.get("invoice")
    error = None
    if record.error_code is not None:
        error = SubmissionErrorOut(
            code=record.error_code.value,
            message=record.error_message or "",
            details=list(record.error_details),
        )
    return DemoRunOut(
        run_id=record.submission_id,
        scenario_id=run.scenario.scenario_id,
        scenario_title=run.scenario.title,
        processing_mode=PROCESSING_MODE,
        provenance=PROVENANCE,
        status=record.status.value,
        steps=[
            step("Submission recorded (Pending)", record.created_at),
            step("Simulated BlobCreated event queued", record.queued_at),
            step("Processor claimed the submission", record.started_at),
            step(record.status.value, record.finished_at),
        ],
        invoice=invoice,
        result=result,
        error=error,
    )


@router.get("/scenarios", response_model=list[DemoScenarioOut], responses=_ERRORS)
def list_scenarios(container: Container) -> list[DemoScenarioOut]:
    service = _service(container)
    return [
        DemoScenarioOut(
            scenario_id=s.scenario_id,
            title=s.title,
            story=s.story,
            invoice_number=s.invoice_number,
            vendor=s.vendor,
            rules_exercised=list(s.expected_rules),
        )
        for s in service.catalog.list()
    ]


@router.post("/runs", response_model=DemoRunOut, responses=_ERRORS)
def run_scenario(body: DemoRunRequest, container: Container) -> DemoRunOut:
    return to_out(_service(container).run(session_id=body.session_id, scenario_id=body.scenario_id))


@router.get("/reference", responses=_ERRORS)
def reference(container: Container) -> dict[str, Any]:
    return _service(container).reference
