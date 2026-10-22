// static/js/emailLibrary/signature.js
//
// The outgoing signature the composer drops into a new draft, so the user
// sees and can edit it before sending rather than discovering it in their
// Sent folder.
//
// Mirrors `src/email_signature.py` — same RFC 3676 `-- ` delimiter, same
// placement above the quoted original, same refusal to add a second copy.
// The two exist because the signature has to be visible while composing
// (here) and still reach messages nothing composed (there); if you change
// the rules in one, change them in both.

// RFC 3676 §4.3: two hyphens, a space, nothing else. The trailing space is
// what makes receiving clients treat the block as a signature.
export const SIGNATURE_DELIMITER = '-- ';

const QUOTE_MARKER_RE = /^-{3,}\s*(?:Previous|Forwarded|Original)\s+[Mm]essage\s*-{3,}\s*$/;
const ATTRIBUTION_RE = /^.*\b(?:wrote|escreveu|schrieb|a écrit)\s*:\s*$/;

let _cache = null;
let _cacheAt = 0;

export function normalizeSignature(raw) {
  const text = String(raw == null ? '' : raw).replace(/\r\n?/g, '\n');
  const lines = text.split('\n').map(l => l.replace(/\s+$/, ''));

  while (lines.length && !lines[0].trim()) lines.shift();
  if (lines.length && (lines[0].trim() === '--')) {
    lines.shift();
    while (lines.length && !lines[0].trim()) lines.shift();
  }
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();

  const out = [];
  let blanks = 0;
  for (const line of lines) {
    if (line.trim()) { blanks = 0; } else { blanks += 1; if (blanks > 1) continue; }
    out.push(line);
  }
  return out.join('\n');
}

export function signatureBlock(signature) {
  const cleaned = normalizeSignature(signature);
  return cleaned ? `${SIGNATURE_DELIMITER}\n${cleaned}` : '';
}

function _key(text) {
  return String(text || '').split('\n').map(l => l.trim()).filter(Boolean);
}

export function bodyHasSignature(body, signature) {
  const needle = _key(normalizeSignature(signature));
  const hay = _key(body);
  if (!needle.length || needle.length > hay.length) return false;
  for (let i = 0; i <= hay.length - needle.length; i++) {
    if (needle.every((line, j) => hay[i + j] === line)) return true;
  }
  return false;
}

function _quoteStart(lines) {
  for (let i = 0; i < lines.length; i++) {
    if (QUOTE_MARKER_RE.test(lines[i])) return i;
    if (lines[i].startsWith('>')) {
      // Step back over the attribution line so the signature does not land
      // between "…wrote:" and the quote it introduces.
      return (i && ATTRIBUTION_RE.test(lines[i - 1])) ? i - 1 : i;
    }
  }
  return -1;
}

// Put the signature into a message body: after what the user wrote, before
// anything quoted. A body that already carries it is returned untouched.
export function withSignature(body, signature) {
  const block = signatureBlock(signature);
  if (!block) return String(body == null ? '' : body);
  const text = String(body == null ? '' : body).replace(/\r\n?/g, '\n');
  if (bodyHasSignature(text, signature)) return text;

  const lines = text.split('\n');
  const cut = _quoteStart(lines);
  if (cut < 0) {
    return text.trim() ? `${text.replace(/\s+$/, '')}\n\n${block}\n` : `${block}\n`;
  }
  const above = lines.slice(0, cut).join('\n').replace(/\s+$/, '');
  const below = lines.slice(cut).join('\n');
  return `${above ? above + '\n\n' : ''}${block}\n\n${below}`;
}

// The delimiter line as it survives an editor round trip: the rich body's
// innerText can drop the trailing space RFC 3676 requires.
const DELIMITER_LINE_RE = /^--[ \t ]*$/;

// Split the user's part of a reply (quote already removed) at the signature
// delimiter: `authored` is what the user wrote, `signature` the block from the
// delimiter down. A signature the composer inserted is not text the user
// typed, so "is this reply still empty?" must look at `authored` only.
export function splitSignature(body) {
  const text = String(body == null ? '' : body).replace(/\r\n?/g, '\n');
  const lines = text.split('\n');
  const at = lines.findIndex(line => DELIMITER_LINE_RE.test(line));
  if (at < 0) return { authored: text.trim(), signature: '' };
  const sig = lines.slice(at + 1).join('\n').trim();
  return {
    authored: lines.slice(0, at).join('\n').trim(),
    signature: sig ? `${SIGNATURE_DELIMITER}\n${sig}` : SIGNATURE_DELIMITER,
  };
}

// Assemble a generated reply into a draft: the reply, then the draft's
// signature block (exactly once), then the quoted original. A model that
// signs off with its own copy of the signature has that copy dropped.
export function composeReplyWithSignature(reply, signatureBlock = '', quote = '') {
  let text = String(reply == null ? '' : reply).replace(/\r\n?/g, '\n').trim();
  const block = String(signatureBlock || '').trim() ? String(signatureBlock).replace(/\s+$/, '') : '';
  if (block) {
    text = splitSignature(text).authored;
    const sigLines = _key(block).filter(l => !DELIMITER_LINE_RE.test(l));
    if (sigLines.length) {
      const lines = text.split('\n');
      const kept = lines.map(l => l.trim()).filter(Boolean);
      const tail = kept.slice(-sigLines.length);
      if (tail.length === sigLines.length && tail.every((l, i) => l === sigLines[i])) {
        // Trim the trailing copy line by line, keeping earlier blank lines.
        let remaining = sigLines.length;
        while (remaining && lines.length) {
          if (lines.pop().trim()) remaining -= 1;
        }
        text = lines.join('\n').trim();
      }
    }
  }
  const parts = [text, block, String(quote || '').trim()].filter(Boolean);
  return parts.join('\n\n');
}

// The signature for the account this draft will be sent from: the active
// account when one is selected, otherwise the default.
export async function loadOutgoingSignature() {
  const now = Date.now();
  if (_cache && (now - _cacheAt) < 30000) return _pick(_cache);
  try {
    const origin = (typeof window !== 'undefined' && window.location) ? window.location.origin : '';
    const res = await fetch(`${origin}/api/email/accounts`, { credentials: 'same-origin' });
    if (!res.ok) return '';
    const data = await res.json();
    _cache = Array.isArray(data.accounts) ? data.accounts : [];
    _cacheAt = now;
  } catch (_) {
    // A signature is a nicety; failing to fetch one must never stop a draft
    // from opening.
    return '';
  }
  return _pick(_cache);
}

// Exported so a settings save can make the next compose pick up the edit
// instead of waiting out the cache.
export function invalidateSignatureCache() {
  _cache = null;
  _cacheAt = 0;
}

function _pick(accounts) {
  const activeId = (typeof window !== 'undefined' && window.__odysseusActiveEmailAccount) || null;
  const account = (activeId && accounts.find(a => String(a.id) === String(activeId)))
    || accounts.find(a => a.is_default)
    || accounts[0];
  if (!account || account.signature_enabled === false) return '';
  return normalizeSignature(account.signature);
}
