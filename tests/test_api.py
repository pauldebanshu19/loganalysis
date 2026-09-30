"""The HTTP API: the happy paths, and every error code at least once."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.utils.errors import ErrorCode


class TestCreateAnalysis:
    async def test_multipart_upload_returns_the_brief_numbers(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        response = await client.post(
            "/api/v1/analyses",
            files={"file": ("brief.log", brief_bytes, "text/plain")},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["lines_processed"] == 7
        assert body["unparseable_lines"] == 1
        assert [(s["service"], s["error_count"]) for s in body["services"]] == [
            ("payment-service", 2),
            ("billing-service", 1),
            ("auth-service", 0),
        ]
        assert body["top_offenders"] == ["payment-service"]

    async def test_location_header_points_at_the_stored_result(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        response = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        location = response.headers["location"]
        assert location == f"/api/v1/analyses/{response.json()['id']}"

        fetched = await client.get(location)
        assert fetched.status_code == 200
        assert fetched.json() == response.json()

    async def test_raw_text_body_with_a_filename_header(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        response = await client.post(
            "/api/v1/analyses",
            content=brief_bytes,
            headers={"Content-Type": "text/plain", "X-Filename": "app.log"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["meta"]["filename"] == "app.log"
        assert body["lines_processed"] == 7

    async def test_raw_body_without_a_filename_reports_none(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        response = await client.post(
            "/api/v1/analyses",
            content=brief_bytes,
            headers={"Content-Type": "text/plain"},
        )
        assert response.json()["meta"]["filename"] is None

    async def test_meta_reports_the_files_own_size_not_the_envelopes(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        response = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        assert response.json()["meta"]["bytes"] == len(brief_bytes)

    async def test_a_chunked_upload_with_no_content_length_works(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        async def stream():
            for start in range(0, len(brief_bytes), 16):
                yield brief_bytes[start : start + 16]

        response = await client.post(
            "/api/v1/analyses",
            content=stream(),
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 201
        assert response.json()["lines_processed"] == 7

    @pytest.mark.parametrize("samples", [0, 1, 5, 100])
    async def test_samples_parameter_caps_the_examples(
        self, client: httpx.AsyncClient, samples: int
    ) -> None:
        body = b"\n".join(b"bad line %d" % i for i in range(50))
        response = await client.post(
            f"/api/v1/analyses?samples={samples}",
            content=body,
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 201
        result = response.json()
        assert result["unparseable_lines"] == 50
        assert len(result["unparseable_samples"]) == min(samples, 50)

    async def test_defaults_to_twenty_samples(self, client: httpx.AsyncClient) -> None:
        body = b"\n".join(b"bad line %d" % i for i in range(50))
        response = await client.post(
            "/api/v1/analyses", content=body, headers={"Content-Type": "text/plain"}
        )
        assert len(response.json()["unparseable_samples"]) == 20

    async def test_a_line_over_the_limit_is_data_not_an_error(
        self, client_factory
    ) -> None:
        """An unreadable line is counted and reported; the request succeeds."""
        client = await client_factory(MAX_LINE_KB=1)
        body = b"2026-09-18 10:23:45 INFO svc fine\n" + b"x" * 5000 + b"\n"
        response = await client.post(
            "/api/v1/analyses", content=body, headers={"Content-Type": "text/plain"}
        )
        assert response.status_code == 201
        result = response.json()
        assert result["lines_processed"] == 2
        assert result["unparseable_lines"] == 1
        sample = result["unparseable_samples"][0]
        assert sample["reason"] == "line_too_long"
        assert sample["line_number"] == 2
        assert len(sample["text"]) <= 500

    async def test_an_over_long_line_does_not_disturb_the_lines_after_it(
        self, client_factory
    ) -> None:
        client = await client_factory(MAX_LINE_KB=1)
        body = (
            b"2026-09-18 10:23:45 INFO svc first\n"
            + b"y" * 9000
            + b"\n2026-09-18 10:23:46 ERROR svc third\n"
        )
        response = await client.post(
            "/api/v1/analyses", content=body, headers={"Content-Type": "text/plain"}
        )
        result = response.json()
        assert result["lines_processed"] == 3
        assert result["unparseable_lines"] == 1
        assert result["services"][0]["error_count"] == 1

    async def test_invalid_utf8_is_replaced_rather_than_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        body = b"2026-09-18 10:23:45 ERROR payment-service bad \xff\xfe bytes\n"
        response = await client.post(
            "/api/v1/analyses", content=body, headers={"Content-Type": "text/plain"}
        )
        assert response.status_code == 201
        assert response.json()["unparseable_lines"] == 0

    async def test_a_filename_with_path_separators_is_sanitised(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        response = await client.post(
            "/api/v1/analyses",
            content=brief_bytes,
            headers={"Content-Type": "text/plain", "X-Filename": "../../etc/passwd"},
        )
        assert response.json()["meta"]["filename"] == "passwd"


class TestGetAnalysis:
    async def test_fetching_a_stored_result(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        created = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        analysis_id = created.json()["id"]
        fetched = await client.get(f"/api/v1/analyses/{analysis_id}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == analysis_id

    async def test_results_expire(self, client_factory, brief_bytes: bytes, error_of) -> None:
        client = await client_factory(RESULT_TTL_S=1)
        created = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        analysis_id = created.json()["id"]

        # Reach into the store's clock rather than sleeping for the TTL.
        store = client.app.state.store
        store._items[analysis_id] = (0.0, store._items[analysis_id][1])

        expired = await client.get(f"/api/v1/analyses/{analysis_id}")
        assert expired.status_code == 404
        assert error_of(expired)["code"] == ErrorCode.ANALYSIS_NOT_FOUND


class TestDeleteAnalysis:
    async def test_a_deleted_result_can_no_longer_be_fetched(
        self, client: httpx.AsyncClient, brief_bytes: bytes, error_of
    ) -> None:
        created = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        analysis_id = created.json()["id"]

        deleted = await client.delete(f"/api/v1/analyses/{analysis_id}")
        assert deleted.status_code == 204
        assert deleted.content == b""

        fetched = await client.get(f"/api/v1/analyses/{analysis_id}")
        assert fetched.status_code == 404
        assert error_of(fetched)["code"] == ErrorCode.ANALYSIS_NOT_FOUND

    async def test_deleting_twice_is_a_404_the_second_time(
        self, client: httpx.AsyncClient, brief_bytes: bytes, error_of
    ) -> None:
        created = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        path = f"/api/v1/analyses/{created.json()['id']}"
        await client.delete(path)

        again = await client.delete(path)
        assert again.status_code == 404
        assert error_of(again)["code"] == ErrorCode.ANALYSIS_NOT_FOUND

    async def test_a_malformed_id_is_not_found(
        self, client: httpx.AsyncClient, error_of
    ) -> None:
        response = await client.delete("/api/v1/analyses/not-an-id")
        assert response.status_code == 404
        assert error_of(response)["code"] == ErrorCode.ANALYSIS_NOT_FOUND


class TestEveryErrorCode:
    """Each code in the table, triggered once, checked for the shared body."""

    async def test_missing_file(self, client: httpx.AsyncClient, error_of) -> None:
        response = await client.post(
            "/api/v1/analyses", files={"wrong_field": ("x.log", b"data")}
        )
        assert response.status_code == 400
        error = error_of(response)
        assert error["code"] == ErrorCode.MISSING_FILE
        assert error["details"]["expected_field"] == "file"

    async def test_empty_file_multipart(self, client: httpx.AsyncClient, error_of) -> None:
        response = await client.post(
            "/api/v1/analyses", files={"file": ("empty.log", b"")}
        )
        assert response.status_code == 400
        assert error_of(response)["code"] == ErrorCode.EMPTY_FILE

    async def test_empty_file_raw_body(self, client: httpx.AsyncClient, error_of) -> None:
        response = await client.post(
            "/api/v1/analyses", content=b"", headers={"Content-Type": "text/plain"}
        )
        assert response.status_code == 400
        assert error_of(response)["code"] == ErrorCode.EMPTY_FILE

    async def test_analysis_not_found(self, client: httpx.AsyncClient, error_of) -> None:
        response = await client.get("/api/v1/analyses/an_0000000000000000")
        assert response.status_code == 404
        assert error_of(response)["code"] == ErrorCode.ANALYSIS_NOT_FOUND

    async def test_a_malformed_id_is_also_not_found(
        self, client: httpx.AsyncClient
    , error_of) -> None:
        response = await client.get("/api/v1/analyses/not-an-id")
        assert response.status_code == 404
        assert error_of(response)["code"] == ErrorCode.ANALYSIS_NOT_FOUND

    async def test_upload_timeout(self, client_factory, error_of) -> None:
        client = await client_factory(UPLOAD_IDLE_TIMEOUT_S=0.2)

        async def stalling_stream():
            yield b"2026-09-18 10:23:45 INFO svc first\n"
            await asyncio.sleep(2.0)
            yield b"2026-09-18 10:23:46 INFO svc never arrives\n"

        response = await client.post(
            "/api/v1/analyses",
            content=stalling_stream(),
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 408
        error = error_of(response)
        assert error["code"] == ErrorCode.UPLOAD_TIMEOUT
        assert response.headers["retry-after"] == "5"

    async def test_file_too_large_from_content_length(self, client_factory, error_of) -> None:
        """Refused from the header alone, without reading the body."""
        client = await client_factory(MAX_UPLOAD_MB=1)
        response = await client.post(
            "/api/v1/analyses",
            content=b"x" * (2 * 1024 * 1024),
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 413
        error = error_of(response)
        assert error["code"] == ErrorCode.FILE_TOO_LARGE
        assert error["details"]["limit_bytes"] == 1024 * 1024

    async def test_file_too_large_while_streaming(self, client_factory, error_of) -> None:
        """A client that sends no length is caught by the running count."""
        client = await client_factory(MAX_UPLOAD_MB=1)

        async def stream():
            for _ in range(40):
                yield b"2026-09-18 10:23:45 INFO svc padding\n" * 1000

        response = await client.post(
            "/api/v1/analyses",
            content=stream(),
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 413
        assert error_of(response)["code"] == ErrorCode.FILE_TOO_LARGE

    async def test_unsupported_media_type(self, client: httpx.AsyncClient, error_of) -> None:
        response = await client.post(
            "/api/v1/analyses",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 415
        error = error_of(response)
        assert error["code"] == ErrorCode.UNSUPPORTED_MEDIA_TYPE
        assert error["details"]["received"] == "application/json"

    async def test_binary_content_is_unsupported(
        self, client: httpx.AsyncClient
    , error_of) -> None:
        response = await client.post(
            "/api/v1/analyses",
            content=b"2026-09-18 10:23:45 INFO svc ok\n\x00\x01\x02binary\n",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 415
        error = error_of(response)
        assert error["code"] == ErrorCode.UNSUPPORTED_MEDIA_TYPE
        assert error["details"]["found"] == "nul_byte"

    async def test_a_nul_byte_past_the_probe_window_is_not_binary(
        self, client: httpx.AsyncClient
    ) -> None:
        """The probe covers the first 8 KB; beyond it, a stray NUL is just a
        line that will not parse."""
        padding = b"2026-09-18 10:23:45 INFO svc padding\n" * 300
        assert len(padding) > 8 * 1024
        response = await client.post(
            "/api/v1/analyses",
            content=padding + b"\x00 late nul\n",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 201

    @pytest.mark.parametrize("samples", [500, -1, "abc"])
    async def test_validation_error(self, client: httpx.AsyncClient, samples, error_of) -> None:
        response = await client.post(
            f"/api/v1/analyses?samples={samples}",
            content=b"x",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 422
        error = error_of(response)
        assert error["code"] == ErrorCode.VALIDATION_ERROR
        assert error["details"]["problems"][0]["field"].endswith("samples")

    async def test_rate_limited(self, client_factory, brief_bytes: bytes, error_of) -> None:
        client = await client_factory(RATE_LIMIT_PER_MIN=2)
        for _ in range(2):
            ok = await client.post(
                "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
            )
            assert ok.status_code == 201

        response = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        assert response.status_code == 429
        error = error_of(response)
        assert error["code"] == ErrorCode.RATE_LIMITED
        assert error["details"]["limit_per_minute"] == 2
        assert 1 <= int(response.headers["retry-after"]) <= 60

    async def test_server_busy_when_every_slot_is_taken(
        self, client_factory, brief_bytes: bytes
    , error_of) -> None:
        client = await client_factory(MAX_CONCURRENT_ANALYSES=1, SLOT_WAIT_S=0.05)
        # Hold the only slot, so the request has nowhere to run.
        async with client.app.state.slots.acquire():
            response = await client.post(
                "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
            )
        assert response.status_code == 503
        error = error_of(response)
        assert error["code"] == ErrorCode.SERVER_BUSY
        assert error["details"]["slots"] == 1
        assert response.headers["retry-after"] == "5"

    async def test_unauthorized_when_api_keys_are_configured(
        self, client_factory, brief_bytes: bytes
    , error_of) -> None:
        client = await client_factory(API_KEYS="secret-one,secret-two")

        without = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        assert without.status_code == 401
        assert error_of(without)["code"] == ErrorCode.UNAUTHORIZED

        wrong = await client.post(
            "/api/v1/analyses",
            files={"file": ("brief.log", brief_bytes)},
            headers={"X-API-Key": "nope"},
        )
        assert wrong.status_code == 401

        right = await client.post(
            "/api/v1/analyses",
            files={"file": ("brief.log", brief_bytes)},
            headers={"X-API-Key": "secret-two"},
        )
        assert right.status_code == 201

    async def test_internal_error_hides_the_details(
        self, client: httpx.AsyncClient, brief_bytes: bytes, monkeypatch
    , error_of) -> None:
        async def boom(_result):
            raise RuntimeError("the database is on fire and the password is hunter2")

        monkeypatch.setattr(client.app.state.store, "put", boom)
        response = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        assert response.status_code == 500
        error = error_of(response)
        assert error["code"] == ErrorCode.INTERNAL_ERROR
        assert "hunter2" not in error["message"]
        assert error["details"] == {}

    async def test_an_unknown_path_uses_the_same_error_shape(
        self, client: httpx.AsyncClient
    , error_of) -> None:
        """Starlette's own 404 must not leak a second error format."""
        response = await client.get("/no/such/path")
        assert response.status_code == 404
        assert error_of(response)["code"] == ErrorCode.ANALYSIS_NOT_FOUND

    async def test_a_wrong_method_keeps_its_status(
        self, client: httpx.AsyncClient
    , error_of) -> None:
        response = await client.put("/api/v1/analyses/an_0000000000000000")
        assert response.status_code == 405
        error_of(response)


class TestRequestId:
    async def test_every_response_carries_one(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        created = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        assert created.headers["x-request-id"].startswith("req_")

        failed = await client.get("/api/v1/analyses/an_0000000000000000")
        assert failed.headers["x-request-id"].startswith("req_")

    async def test_a_client_supplied_id_is_echoed(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(
            "/api/v1/health", headers={"X-Request-ID": "trace-abc-123"}
        )
        assert response.headers["x-request-id"] == "trace-abc-123"

    @pytest.mark.parametrize(
        "supplied", ["", "   ", "x" * 200, "has spaces", "inject\r\nheader"]
    )
    async def test_an_unusable_id_is_replaced(
        self, client: httpx.AsyncClient, supplied: str
    ) -> None:
        """The id is echoed into a header and a log line, so it is validated."""
        response = await client.get(
            "/api/v1/health", headers={"X-Request-ID": supplied}
        )
        assert response.headers["x-request-id"].startswith("req_")


class TestHealthAndMetrics:
    async def test_health_reports_the_store_and_the_slots(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["store_reachable"] is True
        assert body["slots"] == {"in_use": 0, "capacity": 8}

    async def test_health_is_degraded_but_still_200_when_the_store_is_gone(
        self, client: httpx.AsyncClient, monkeypatch
    ) -> None:
        """A monitor has to tell "Redis is down" from "the API is down"."""

        async def unreachable():
            return False

        monkeypatch.setattr(client.app.state.store, "ping", unreachable)
        response = await client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"

    async def test_metrics_counts_analyses_and_errors(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        await client.post("/api/v1/analyses", files={"file": ("brief.log", brief_bytes)})
        await client.get("/api/v1/analyses/an_0000000000000000")

        response = await client.get("/metrics")
        assert response.status_code == 200
        text = response.text
        assert "analysis_lines_processed_total" in text
        assert 'api_errors_total{code="analysis_not_found"}' in text
        assert 'api_requests_total{method="POST",route="/api/v1/analyses",status="201"}' in text
        assert "analysis_slots_total" in text

    async def test_metrics_labels_routes_by_template_not_by_id(
        self, client: httpx.AsyncClient, brief_bytes: bytes
    ) -> None:
        """Otherwise every analysis id would mint a new time series."""
        created = await client.post(
            "/api/v1/analyses", files={"file": ("brief.log", brief_bytes)}
        )
        analysis_id = created.json()["id"]
        await client.get(f"/api/v1/analyses/{analysis_id}")

        text = (await client.get("/metrics")).text
        assert "/api/v1/analyses/{analysis_id}" in text
        assert analysis_id not in text


class TestOpenApi:
    async def test_error_codes_are_published_as_an_enum(
        self, client: httpx.AsyncClient
    ) -> None:
        """Clients switch on `code`, so the codes belong in the schema."""
        spec = (await client.get("/openapi.json")).json()
        codes = spec["components"]["schemas"]["ErrorCode"]["enum"]
        assert set(codes) == {code.value for code in ErrorCode}

    async def test_the_error_body_is_documented_for_failures(
        self, client: httpx.AsyncClient
    ) -> None:
        spec = (await client.get("/openapi.json")).json()
        post = spec["paths"]["/api/v1/analyses"]["post"]["responses"]
        for status in ("400", "413", "415", "429", "503"):
            schema = post[status]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("ErrorResponse")

    async def test_docs_are_served(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 200


class TestSizeLimitBoundary:
    """The limit is on the file, not on the framing around it.

    A multipart envelope adds a couple of hundred bytes to the body. Counting
    those against the user's 100 MB would refuse a file that is exactly the
    advertised size, with an error reading "File is 100 MB; the limit is
    100 MB" -- which looks like a bug and cannot be acted on.
    """

    async def test_a_file_at_exactly_the_limit_is_accepted(
        self, client_factory
    ) -> None:
        client = await client_factory(MAX_UPLOAD_MB=1)
        line = b"2026-09-18 10:23:45 INFO svc padding\n"
        body = (line * (1024 * 1024 // len(line))).ljust(1024 * 1024, b"x")
        assert len(body) == 1024 * 1024

        response = await client.post(
            "/api/v1/analyses", files={"file": ("exact.log", body)}
        )
        assert response.status_code == 201
        assert response.json()["meta"]["bytes"] == 1024 * 1024

    async def test_a_file_over_the_limit_is_still_refused(
        self, client_factory, error_of
    ) -> None:
        client = await client_factory(MAX_UPLOAD_MB=1)
        body = b"x" * (1024 * 1024 + 1)

        response = await client.post(
            "/api/v1/analyses", files={"file": ("over.log", body)}
        )
        assert response.status_code == 413
        error = error_of(response)
        assert error["code"] == ErrorCode.FILE_TOO_LARGE
        assert error["details"]["limit_bytes"] == 1024 * 1024

    async def test_the_reported_limit_is_the_file_limit_not_the_transport_one(
        self, client_factory, error_of
    ) -> None:
        """Whatever slack the transport is given, the client is told the
        number it can act on."""
        client = await client_factory(MAX_UPLOAD_MB=1)
        response = await client.post(
            "/api/v1/analyses",
            content=b"x" * (4 * 1024 * 1024),
            headers={"Content-Type": "text/plain"},
        )
        assert error_of(response)["details"]["limit_bytes"] == 1024 * 1024
