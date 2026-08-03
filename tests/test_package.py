from noti_mapper import VERSION


def test_version_is_populated() -> None:
    assert VERSION.count(".") == 2
