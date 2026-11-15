import json

import httpx

from recall.engine import Notice
from recall.verda import VerdaExtractionAuditor, source_excerpt


def test_source_excerpt_drops_script_and_markup_before_the_model_call():
    assert source_excerpt("<style>hide</style><p>A &amp; B</p><script>ignore()</script>") == "A & B"


def test_verda_audit_returns_grounding_labels_without_changing_the_notice():
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "kimi-k3-1-pro" and "not a safety decision" in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "field_status": {"products": "supported", "eans": "supported"}, "concerns": [], "overall": "supported"
        })}}]})

    notice = Notice(id="n", url="https://source.example/notice", title="t", date="2026-02-10", published_at=1, detected_at=2,
                    products=["Atria Coleslaw 300 g"], eans=["6407860065295"])
    result = VerdaExtractionAuditor("key", transport=httpx.MockTransport(handler)).audit(notice, "<p>Source</p>")
    assert result["notice"] == notice.url and result["audit"]["field_status"]["eans"]["status"] == "supported"
