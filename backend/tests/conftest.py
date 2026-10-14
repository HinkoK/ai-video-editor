from fractions import Fraction

import pytest

from media_fixtures import make_fixture


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    # Кириллица и пробелы в пути нарочно: так выглядят реальные папки автора
    return tmp_path_factory.mktemp("медиа") / "папка с пробелами"


@pytest.fixture(scope="session")
def fx_plain(media_dir):
    media_dir.mkdir(parents=True, exist_ok=True)
    return make_fixture(media_dir / "обычный 25.mp4", frames=1500, gop=48)


@pytest.fixture(scope="session")
def fx_ntsc_offset(media_dir):
    # 29.97, видео начинается позже звука: служебные отсчёты AAC здесь звучат
    media_dir.mkdir(parents=True, exist_ok=True)
    return make_fixture(
        media_dir / "ntsc со сдвигом.mp4",
        fps=Fraction(30000, 1001),
        frames=1500,
        audio_delay=Fraction(1, 4),
        video_delay=Fraction(1, 2),
        gop=60,
    )


@pytest.fixture(scope="session")
def fx_late_audio(media_dir):
    media_dir.mkdir(parents=True, exist_ok=True)
    return make_fixture(media_dir / "звук позже.mp4", frames=500, audio_delay=Fraction(1, 3))


@pytest.fixture(scope="session")
def fx_vfr(media_dir):
    media_dir.mkdir(parents=True, exist_ok=True)
    return make_fixture(media_dir / "плавающая частота.mp4", frames=300, vfr=True)


@pytest.fixture(scope="session")
def fx_ntsc_mkv(media_dir):
    # mkv хранит время в миллисекундах (так пишет OBS по умолчанию):
    # кадры 29.97 стоят на 0, 33, 67, 100 мс, а не точно на k * 1001/30000 с
    media_dir.mkdir(parents=True, exist_ok=True)
    return make_fixture(media_dir / "ntsc obs.mkv", fps=Fraction(30000, 1001), frames=1500, gop=60)


@pytest.fixture(scope="session")
def fx_second(media_dir):
    """Второй файл того же формата, что fx_plain: «экран» после «камеры».
    Другой шум и номера кадров с 30000."""
    media_dir.mkdir(parents=True, exist_ok=True)
    return make_fixture(media_dir / "экран.mp4", frames=500, seed=2, first_number=30000)


@pytest.fixture(scope="session")
def fx_lesson(tmp_path_factory):
    """Синтетическая русская речь (голос macOS) с дублями, паузами и фразой-ловушкой."""
    from media_fixtures import make_speech_fixture

    return make_speech_fixture(tmp_path_factory.mktemp("урок") / "урок с дублями.mp4")
