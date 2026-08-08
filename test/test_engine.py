from invoice_core import SpatialIndex
from invoice_models import TextLine


def line(y: float, text: str) -> TextLine:
    return TextLine(page=0, text=text, x0=50, y0=y, x1=150, y1=y+10, page_width=600, page_height=800)


def test_spatial_index_band_is_local():
    lines = [line(float(i * 20), f"line {i}") for i in range(100)]
    index = SpatialIndex(lines, bucket_px=10)
    nearby = index.query_band(0, 400, radius_px=15)
    assert len(nearby) < 10
    assert any(item.text == "line 20" for item in nearby)
