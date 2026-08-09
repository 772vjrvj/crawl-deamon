# -*- coding: utf-8 -*-
"""SERVICE_CODE별 Worker 선택 및 공통 실행 결과 변환."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from src.database.execution_repository import ExecutionJob


@dataclass(frozen=True)
class WorkerExecutionResult:
    inserted_count: int
    success_count: int
    fail_count: int
    skipped_count: int
    stopped_early: bool = False
    stop_reason: Optional[str] = None


def requires_selenium(service_code: str) -> bool:
    """공통 main에서 Selenium Chrome을 만들어야 하는 서비스인지 반환한다."""
    if service_code == "PANDA_MESSAGE":
        return True
    if service_code == "NAVER_SHOP_TOTAL_DETAIL":
        return False
    raise ValueError(f"지원하지 않는 SERVICE_CODE입니다: {service_code}")


def execute_registered_worker(
        service_code: str,
        job: ExecutionJob,
        logger: Any,
        result_store: Any,
        driver: Any = None,
        selenium_utils: Any = None,
) -> WorkerExecutionResult:
    """등록된 서비스 Worker를 실행하고 공통 결과로 변환한다."""
    if service_code == "PANDA_MESSAGE":
        from src.workers.panda_worker import execute_message_job

        result = execute_message_job(
            driver=driver,
            selenium_utils=selenium_utils,
            job=job,
            logger=logger,
            result_inserter=result_store.insert,
            result_updater=result_store.update,
        )

        return WorkerExecutionResult(
            inserted_count=result.inserted_count,
            success_count=result.success_count,
            fail_count=result.fail_count,
            skipped_count=result.skipped_count,
            stopped_early=result.stopped_early,
            stop_reason=result.stop_reason,
        )

    if service_code == "NAVER_SHOP_TOTAL_DETAIL":
        # 네이버 Worker 관련 무거운 패키지는 해당 서비스 실행 시에만 import한다.
        from src.workers.naver_shop_total_detail_worker import (
            NaverShopTotalDetailWorker,
        )

        worker = NaverShopTotalDetailWorker(
            setting=asdict(job.setting),
            logger=logger,
            result_inserter=result_store.insert,
            existing_store_loader=result_store.load_existing_store_names,
        )

        try:
            if not worker.init():
                raise RuntimeError("네이버 쇼핑 Worker 초기화에 실패했습니다.")

            success = worker.main()
            if not success:
                raise RuntimeError("네이버 쇼핑 수집 작업에 실패했습니다.")

            return WorkerExecutionResult(
                inserted_count=worker.inserted_count,
                success_count=worker.inserted_count,
                fail_count=worker.failed_count,
                skipped_count=0,
            )
        finally:
            worker.cleanup()

    raise ValueError(f"지원하지 않는 SERVICE_CODE입니다: {service_code}")
