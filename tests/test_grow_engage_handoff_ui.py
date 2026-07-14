from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_collect_handoff_keeps_source_context():
    html = (ROOT / "site/workbench/collect.html").read_text(encoding="utf-8")
    assert "sourceUrl: currentUrl" in html
    assert "hq_banana_to_collect" in html
    assert "urlInput.value = bananaReturn.sourceUrl" in html


def test_banana_offers_contextual_return_to_collect():
    html = (ROOT / "site/workbench/banana.html").read_text(encoding="utf-8")
    assert "btn.id='returnCollectBtn'" in html
    assert "hq_banana_to_collect" in html
    assert "location.href='collect.html?handoff=banana'" in html
    assert "imageUrl:lastResultUrl||''" in html
