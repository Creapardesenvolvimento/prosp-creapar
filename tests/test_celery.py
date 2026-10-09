from app.tasks import ping


def test_task_roda_em_modo_eager():
    assert ping.delay().get() == "pong"
