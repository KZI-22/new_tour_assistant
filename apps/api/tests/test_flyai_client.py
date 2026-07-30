from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pytest
from app.clients.flyai_client import FlyAIClient
from app.schemas.travel import FlightSearchInput, FlyAIErrorCode


class FakeProcess:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = 0,
        *,
        times_out: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.times_out = times_out
        self.pid = 12345
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.times_out:
            raise subprocess.TimeoutExpired("flyai", timeout)
        return self.stdout, self.stderr

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class RecordingFactory:
    def __init__(self, *processes: FakeProcess) -> None:
        self.processes = list(processes)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **options: Any) -> FakeProcess:
        self.calls.append((command, options))
        return self.processes.pop(0)


def make_client(
    tmp_path: Any,
    factory: Callable[..., FakeProcess],
    *,
    max_retries: int = 0,
) -> FlyAIClient:
    executable = tmp_path / "flyai.cmd"
    executable.write_text("@echo off", encoding="utf-8")
    return FlyAIClient(
        executable,
        max_retries=max_retries,
        retry_delay_seconds=0,
        process_factory=factory,
    )


@pytest.mark.asyncio
async def test_valid_json_and_stderr_warning_succeed_with_safe_subprocess_options(
    tmp_path: Any,
) -> None:
    process = FakeProcess('{"items":[{"name":"西湖"}]}', "provider warning")
    factory = RecordingFactory(process)
    client = make_client(tmp_path, factory)

    result = await client.execute("search-poi", ["--city-name", "杭州", "--keyword", "西湖"])

    assert result.success is True
    assert result.data == {"items": [{"name": "西湖"}]}
    command, options = factory.calls[0]
    assert command[2:] == ["--city-name", "杭州", "--keyword", "西湖"]
    assert options["shell"] is False
    assert options["stdout"] is subprocess.PIPE
    assert options["stderr"] is subprocess.PIPE
    assert options["encoding"] == "utf-8"


@pytest.mark.asyncio
async def test_cli_not_found_returns_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.clients.flyai_client.shutil.which", lambda _: None)
    client = FlyAIClient()

    result = await client.execute("search-poi", ["--city-name", "杭州", "--keyword", "西湖"])

    assert result.success is False
    assert result.error_code == FlyAIErrorCode.CLI_NOT_FOUND
    assert "FLYAI_CLI_PATH" in (result.error_message or "")


@pytest.mark.asyncio
async def test_timeout_terminates_process_tree(tmp_path: Any) -> None:
    process = FakeProcess(returncode=None, times_out=True)
    factory = RecordingFactory(process)
    client = make_client(tmp_path, factory)
    terminated: list[FakeProcess] = []
    client._terminate_process_tree = terminated.append  # type: ignore[method-assign]

    result = await client.execute("search-poi", ["--city-name", "杭州", "--keyword", "西湖"])

    assert result.error_code == FlyAIErrorCode.CLI_TIMEOUT
    assert terminated == [process]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("not-json", "", FlyAIErrorCode.INVALID_JSON),
        ("", "warning only", FlyAIErrorCode.EMPTY_RESULT),
    ],
)
async def test_invalid_or_empty_stdout_is_classified(
    tmp_path: Any,
    stdout: str,
    stderr: str,
    expected: FlyAIErrorCode,
) -> None:
    client = make_client(tmp_path, RecordingFactory(FakeProcess(stdout, stderr)))

    result = await client.execute("search-poi", ["--city-name", "杭州", "--keyword", "西湖"])

    assert result.success is False
    assert result.error_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("unexpected provider failure", FlyAIErrorCode.CLI_EXIT_ERROR),
        ("401 Unauthorized: invalid API key=very-secret", FlyAIErrorCode.AUTH_ERROR),
    ],
)
async def test_nonzero_exit_and_authentication_errors_are_classified_and_redacted(
    tmp_path: Any,
    stderr: str,
    expected: FlyAIErrorCode,
) -> None:
    client = make_client(tmp_path, RecordingFactory(FakeProcess(stderr=stderr, returncode=1)))

    result = await client.execute("search-poi", ["--city-name", "杭州", "--keyword", "西湖"])

    assert result.error_code == expected
    assert "very-secret" not in (result.error_message or "")


@pytest.mark.asyncio
async def test_nonzero_exit_preserves_usable_json_business_result(tmp_path: Any) -> None:
    client = make_client(
        tmp_path,
        RecordingFactory(
            FakeProcess(
                stdout='{"items":[{"name":"西湖"}]}',
                stderr="wrapper exited after writing data",
                returncode=1,
            )
        ),
    )

    result = await client.execute(
        "search-poi",
        ["--city-name", "杭州", "--keyword", "西湖"],
    )

    assert result.success is True
    assert result.data == {"items": [{"name": "西湖"}]}
    assert result.diagnostics.process_status == "failed"
    assert result.diagnostics.process_return_code == 1
    assert result.diagnostics.parse_status == "success"
    assert result.diagnostics.business_status == "usable"


@pytest.mark.asyncio
async def test_rate_limit_is_retried_only_once(tmp_path: Any) -> None:
    factory = RecordingFactory(
        FakeProcess(stderr="429 Too Many Requests", returncode=1),
        FakeProcess(stderr="429 Too Many Requests", returncode=1),
    )
    client = make_client(tmp_path, factory, max_retries=1)

    result = await client.execute("search-poi", ["--city-name", "杭州", "--keyword", "西湖"])

    assert result.error_code == FlyAIErrorCode.RATE_LIMITED
    assert len(factory.calls) == 2


@pytest.mark.asyncio
async def test_chinese_flight_values_are_passed_as_separate_arguments(tmp_path: Any) -> None:
    factory = RecordingFactory(FakeProcess('{"flights":[]}'))
    client = make_client(tmp_path, factory)
    query = FlightSearchInput(
        origin="上海",
        destination="北京",
        departure_date=date.today() + timedelta(days=1),
        seat_classes=("economy",),
    )

    result = await client.search_flight(query)

    assert result.success is True
    command = factory.calls[0][0]
    assert command[1:] == [
        "search-flight",
        "--origin",
        "上海",
        "--destination",
        "北京",
        "--dep-date",
        query.departure_date.isoformat(),
        "--seat-class-name",
        "economy",
    ]


@pytest.mark.asyncio
async def test_keyword_search_rejects_blank_query_before_starting_process(tmp_path: Any) -> None:
    factory = RecordingFactory(FakeProcess("{}"))
    client = make_client(tmp_path, factory)

    result = await client.keyword_search("   ")

    assert result.error_code == FlyAIErrorCode.INVALID_ARGUMENT
    assert factory.calls == []
