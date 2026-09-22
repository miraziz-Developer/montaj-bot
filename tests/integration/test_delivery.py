import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile
from sqlalchemy import update

from app.bot import texts
from app.models import Job
from app.models.enums import JobStatus
from app.services import delivery
from app.services.delivery import deliver_result, max_inline_bytes
from app.worker.tasks import run_render
from tests.fakes.bot import FakeBotClient, bad_request, network_error

pytestmark = pytest.mark.slow

RESULT = b"final-video-bytes"


async def _delivering_job(harness, *, size: int | None = None, chat_id: int | None = 777):  # noqa: ANN001, ANN202
    """A job that finished rendering: final.mp4 is in `outputs`, status DELIVERING."""
    h = await harness.new_job(status=JobStatus.DELIVERING, current_plan_version=1)
    blob = f"{h.job_id}/final.mp4"
    harness.storage.put("outputs", blob, RESULT)
    async with harness.session_factory() as session:
        await session.execute(
            update(Job)
            .where(Job.id == h.job_id)
            .values(
                output_blob_path=blob,
                output_size_bytes=size if size is not None else len(RESULT),
                chat_id=chat_id,
            )
        )
        await session.commit()
    return h


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(delivery, "_sleep", instant)


# ---------- happy path and fallbacks ----------


async def test_small_result_is_sent_as_a_video_and_the_job_is_done(harness) -> None:  # noqa: ANN001
    harness.deps.bot = bot = FakeBotClient()
    h = await _delivering_job(harness)
    await deliver_result(harness.deps, await harness.job(h.job_id))

    [(kind, chat_id, payload, kwargs)] = bot.calls
    assert (kind, chat_id) == ("video", 777) and isinstance(payload, FSInputFile)
    assert kwargs["caption"] == texts.DELIVERY_CAPTION
    job = await harness.job(h.job_id)
    assert job.status == JobStatus.DONE and job.finished_at is not None
    assert [c[0] for c in harness.notifier.calls] == ["delivered"]
    assert ("outputs", f"{h.job_id}/final.mp4") in harness.storage.downloads
    assert not (harness.tmp_dir / f"{h.job_id}-deliver").exists()  # temp file removed


async def test_a_failed_video_send_falls_back_to_a_document(harness) -> None:  # noqa: ANN001
    harness.deps.bot = bot = FakeBotClient(video_errors=[bad_request()])
    h = await _delivering_job(harness)
    await deliver_result(harness.deps, await harness.job(h.job_id))
    assert [c[0] for c in bot.calls] == ["video", "document"]
    assert (await harness.job(h.job_id)).status == JobStatus.DONE


async def test_network_errors_are_retried_before_falling_back(harness) -> None:  # noqa: ANN001
    harness.deps.bot = bot = FakeBotClient(video_errors=[network_error(), network_error()])
    h = await _delivering_job(harness)
    await deliver_result(harness.deps, await harness.job(h.job_id))
    assert [c[0] for c in bot.calls] == ["video", "video", "video"]  # third attempt succeeded


async def test_three_network_failures_move_on_to_the_document(harness) -> None:  # noqa: ANN001
    harness.deps.bot = bot = FakeBotClient(video_errors=[network_error()] * 3)
    h = await _delivering_job(harness)
    await deliver_result(harness.deps, await harness.job(h.job_id))
    assert [c[0] for c in bot.calls] == ["video", "video", "video", "document"]


async def test_if_video_and_document_both_fail_a_48h_link_is_sent(harness) -> None:  # noqa: ANN001
    harness.deps.bot = bot = FakeBotClient(video_errors=[bad_request()], document_errors=[bad_request()])
    h = await _delivering_job(harness)
    await deliver_result(harness.deps, await harness.job(h.job_id))

    assert [c[0] for c in bot.calls] == ["video", "document", "message"]
    [(_, _, url_text, _)] = bot.of("message")
    assert url_text.startswith("✅ Video tayyor!") and "fake://read/outputs/" in url_text
    assert harness.storage.read_urls == [("outputs", f"{h.job_id}/final.mp4", 48, True)]
    assert (await harness.job(h.job_id)).status == JobStatus.DONE
    assert not (harness.tmp_dir / f"{h.job_id}-deliver").exists()


async def test_results_over_the_inline_limit_are_only_sent_as_a_link(harness) -> None:  # noqa: ANN001
    harness.deps.bot = bot = FakeBotClient()
    harness.settings.deliver_max_inline_bytes = 1000
    h = await _delivering_job(harness, size=5000)
    await deliver_result(harness.deps, await harness.job(h.job_id))
    assert [c[0] for c in bot.calls] == ["message"]  # no video, no download of the big file
    assert harness.storage.downloads == []
    assert harness.storage.read_urls[0][2] == 48
    assert (await harness.job(h.job_id)).status == JobStatus.DONE


async def test_without_a_local_bot_api_server_files_over_50mb_go_as_a_link(harness) -> None:  # noqa: ANN001
    harness.settings.telegram_api_base = ""  # the public cloud API: 50 MB upload limit
    assert max_inline_bytes(harness.deps) == 50_000_000
    harness.settings.telegram_api_base = "http://telegram-bot-api:8081"
    assert max_inline_bytes(harness.deps) == 1_900_000_000

    harness.settings.telegram_api_base = ""
    harness.deps.bot = bot = FakeBotClient()
    h = await _delivering_job(harness, size=60_000_000)
    await deliver_result(harness.deps, await harness.job(h.job_id))
    assert [c[0] for c in bot.calls] == ["message"]


async def test_the_chat_falls_back_to_the_users_telegram_id(harness) -> None:  # noqa: ANN001
    harness.deps.bot = bot = FakeBotClient()
    h = await _delivering_job(harness, chat_id=None)
    telegram_id = (await harness.user(h.user_id)).telegram_id
    await deliver_result(harness.deps, await harness.job(h.job_id))
    assert bot.calls[0][1] == telegram_id


# ---------- failures ----------


async def test_when_every_path_fails_the_error_propagates(harness) -> None:  # noqa: ANN001
    harness.deps.bot = FakeBotClient(
        video_errors=[bad_request()], document_errors=[bad_request()], message_errors=[bad_request()]
    )
    h = await _delivering_job(harness)
    with pytest.raises(TelegramAPIError):
        await deliver_result(harness.deps, await harness.job(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.DELIVERING  # not DONE: nothing reached the user


async def test_run_render_turns_an_undeliverable_result_into_delivery_failed(harness) -> None:  # noqa: ANN001
    harness.deps.bot = FakeBotClient(
        video_errors=[bad_request()], document_errors=[bad_request()], message_errors=[bad_request()]
    )
    h = await harness.new_job(status=JobStatus.AWAITING_PLAN_APPROVAL, current_plan_version=1)
    await harness.add_plan(h.job_id)
    harness.seed_artifacts(h.job_id)
    await run_render(harness.ctx, str(h.job_id))
    job = await harness.job(h.job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "DELIVERY_FAILED")
    assert await harness.ledger(h.user_id) == [("reserve", -1), ("refund", 1)]


async def test_run_render_with_a_bot_delivers_the_real_video(harness) -> None:  # noqa: ANN001
    harness.deps.bot = bot = FakeBotClient()
    h = await harness.new_job(status=JobStatus.AWAITING_PLAN_APPROVAL, current_plan_version=1)
    await harness.add_plan(h.job_id)
    harness.seed_artifacts(h.job_id)
    await run_render(harness.ctx, str(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.DONE
    [(kind, _, payload, _)] = bot.calls
    assert kind == "video" and isinstance(payload, FSInputFile)
    assert not (harness.tmp_dir / f"{h.job_id}-deliver").exists()


async def test_a_job_that_is_no_longer_delivering_is_not_marked_done_twice(harness) -> None:  # noqa: ANN001
    h = await _delivering_job(harness)
    await harness.set_status(h.job_id, JobStatus.CANCELED)
    await deliver_result(harness.deps, await harness.job(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.CANCELED
