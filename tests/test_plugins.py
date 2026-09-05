from du_pipeline.plugins import ImageProvider, AnimationProvider, InputAdapter


def test_protocols_are_importable():
    assert ImageProvider and AnimationProvider and InputAdapter
