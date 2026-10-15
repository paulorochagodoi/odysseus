"""AI reply must treat a signature-only draft as empty.

The composer drops the account's signature ("-- \\nName") into every new
draft. The AI-reply gate counted that block as text the user had typed and
refused with "Reply already has text", and the generated reply replaced the
whole own-part of the body, signature included.

These run the real gate from ``static/js/document.js`` (extracted by name)
together with ``static/js/emailLibrary/signature.js`` in node, so they pin
behaviour rather than source text.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.helpers.document_source import function_body

_REPO = Path(__file__).resolve().parent.parent
_SIG_MOD = _REPO / "static" / "js" / "emailLibrary" / "signature.js"
_HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")

_GATE_FUNCTIONS = (
    "_emailQuoteStartIndex",
    "_emailQuoteStartOffset",
    "_splitEmailReplyQuote",
    "_emailReplySignatureSplit",
    "_emailAiReplyDraftState",
)

# Just enough of <template> for the gate: textContent and a media check.
_DOM_STUB = r"""
globalThis.document = { createElement: () => {
  let html = '';
  return {
    set innerHTML(v) { html = String(v); },
    get innerHTML() { return html; },
    content: {
      get textContent() { return html.replace(/<[^>]*>/g, ''); },
      querySelector(sel) {
        const tags = sel.split(',').map(t => t.trim()).join('|');
        return new RegExp(`<(?:${tags})\\b`, 'i').test(html) ? {} : null;
      },
    },
  };
} };
"""


_HARNESS = """
import {{ splitSignature, composeReplyWithSignature }} from {sig_mod};
{dom_stub}
{functions}
let input = '';
for await (const chunk of process.stdin) input += chunk;
const out = JSON.parse(input).map(([body, reply]) => {{
  const state = _emailAiReplyDraftState(body);
  const result = state.replaceable
    ? composeReplyWithSignature(reply, state.signature, state.quote)
    : null;
  return {{ replaceable: state.replaceable, result }};
}});
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def run_many():
    """Run (body, reply) cases through one node process."""
    functions = "\n".join(textwrap.dedent(function_body(n)) for n in _GATE_FUNCTIONS)
    script = _HARNESS.format(sig_mod=json.dumps(_SIG_MOD.as_uri()),
                             dom_stub=_DOM_STUB, functions=functions)

    def run(cases):
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              input=json.dumps(cases), capture_output=True,
                              text=True, cwd=str(_REPO), timeout=120)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip())

    return run


@pytest.fixture
def _run(run_many):
    return lambda body, reply: run_many([[body, reply]])[0]


_SIG = "-- \nAda Lovelace\nAnalytical Engines Ltd"
_QUOTE = "---------- Previous message ----------\nFrom: bob@example.com\nCan we meet tomorrow?"
_REPLY = "Hi Bob,\n\nTomorrow works.\n\nBest,\nAda"


def test_empty_body_with_signature_allows_ai_reply_and_keeps_one_signature(_run):
    out = _run(f"\n\n{_SIG}\n", _REPLY)

    assert out["replaceable"] is True
    assert out["result"] == f"{_REPLY}\n\n{_SIG}"
    assert out["result"].count("Ada Lovelace") == 1
    assert out["result"].index("Tomorrow works") < out["result"].index("-- \n")


def test_signature_above_quote_allows_ai_reply_and_keeps_order(_run):
    out = _run(f"\n{_SIG}\n\n{_QUOTE}", _REPLY)

    assert out["replaceable"] is True
    assert out["result"] == f"{_REPLY}\n\n{_SIG}\n\n{_QUOTE}"


def test_signature_survives_rich_editor_dropping_the_delimiter_space(_run):
    out = _run("--\nAda Lovelace\nAnalytical Engines Ltd", _REPLY)

    assert out["replaceable"] is True
    assert out["result"].count("Ada Lovelace") == 1
    assert out["result"].startswith(_REPLY)


def test_model_echoing_the_signature_does_not_duplicate_it(_run):
    echoed = f"Hi Bob,\n\nTomorrow works.\n\n{_SIG}"
    out = _run(f"{_SIG}\n\n{_QUOTE}", echoed)

    assert out["result"] == f"Hi Bob,\n\nTomorrow works.\n\n{_SIG}\n\n{_QUOTE}"

    bare = "Hi Bob,\n\nTomorrow works.\n\nAda Lovelace\nAnalytical Engines Ltd"
    out = _run(f"{_SIG}\n\n{_QUOTE}", bare)
    assert out["result"] == f"Hi Bob,\n\nTomorrow works.\n\n{_SIG}\n\n{_QUOTE}"


def test_quoted_original_only_allows_ai_reply(_run):
    out = _run(f"\n\n{_QUOTE}", _REPLY)

    assert out["replaceable"] is True
    assert out["result"] == f"{_REPLY}\n\n{_QUOTE}"


def test_whitespace_only_body_allows_ai_reply(_run):
    out = _run("  \n\n ", _REPLY)

    assert out["replaceable"] is True
    assert out["result"] == _REPLY


def test_user_text_still_blocks_ai_reply(_run):
    assert _run("Thanks, I'll check.", _REPLY)["replaceable"] is False
    assert _run(f"Thanks, I'll check.\n\n{_SIG}\n\n{_QUOTE}", _REPLY)["replaceable"] is False
    assert _run(f"Thanks, I'll check.\n\n{_QUOTE}", _REPLY)["replaceable"] is False


def test_drafting_placeholder_is_still_replaceable_with_signature(_run):
    out = _run(f"[AI reply draft will appear here]\n\n{_SIG}", _REPLY)

    assert out["replaceable"] is True
    assert out["result"] == f"{_REPLY}\n\n{_SIG}"


def test_html_signature_only_body_is_replaceable(_run):
    out = _run("<p><br></p><p>-- <br>Ada Lovelace</p>", _REPLY)

    assert out["replaceable"] is True
    assert out["result"] == f"{_REPLY}\n\n-- \nAda Lovelace"
