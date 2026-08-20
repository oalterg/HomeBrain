/* Shared credential-sheet builder.
 *
 * Loaded by the two pages that ever reveal a secret: the setup handover
 * (templates/installing.html) and Settings (templates/dashboard.html, via
 * static/dashboard.js). Both used to carry their own copy of this text; see
 * docs/plans/RECOVERY_SHEET.md F1.
 *
 * Client-side by necessity, not preference: the new master password typed in
 * Settings exists only in the browser form, and the device keeps nothing but a
 * scrypt hash of the recovery phrase. A server-side builder would serve two of
 * the three surfaces and need a new secret round trip for the third (D4).
 *
 * buildCredsSheet is pure — no DOM, no globals, no clock — so
 * scripts/tests/test_creds_sheet.js can exercise it under JavaScriptCore
 * (osascript -l JavaScript), which is the only JS runtime available here.
 */

function _hbSheetParts(date) {
    const pad = (n) => String(n).padStart(2, '0');
    return {
        day: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
        hh: pad(date.getHours()),
        mm: pad(date.getMinutes()),
    };
}

// Minute resolution, not day: two sheets generated on one day would otherwise
// collide into "…(1).txt" with nothing in the name to say which one is current.
function credsSheetFilename(date) {
    const p = _hbSheetParts(date);
    return `homebrain-recovery-${p.day}T${p.hh}-${p.mm}.txt`;
}

/* Returns the sheet body. ASCII only and CRLF terminated on purpose: this file
 * exists to be opened by an unknown text editor and printed, possibly from a
 * machine the owner does not control. The timestamp is formatted by hand rather
 * than with toLocaleString() for the same reason — modern ICU emits a narrow
 * no-break space (U+202F) inside en-US times, which is neither ASCII nor
 * something anyone would notice until a printout looked wrong. */
function buildCredsSheet({ password, phrase, device, date, phraseUnchanged }) {
    const p = _hbSheetParts(date);
    const lines = [
        'HomeBrain -- recovery sheet',
        `Generated: ${p.day} ${p.hh}:${p.mm}`,
        `Device:    ${device}`,
        '',
    ];
    if (password) lines.push(`Master password:  ${password}`);
    if (phrase) lines.push(`Recovery phrase:  ${phrase}`);
    // Set when the sheet was produced by a deliberate password change: the
    // phrase survives rotation untouched, so the setup sheet is still the
    // owner's way back in and must not be thrown away with this one.
    if (phraseUnchanged) lines.push(
        'Recovery phrase:  unchanged -- the sheet from setup is still valid.');
    lines.push(
        '',
        'Keep this offline -- print it, or put it on a USB stick. Anyone holding',
        'it can reset administrative access to this device.',
        '',
    );
    // Only promise the recovery flow when a phrase is actually on the sheet:
    // setup falls back to a password-only handover when the wordlist is
    // unavailable, and telling that user to "enter the phrase" sends them
    // looking for something they were never given.
    if (phrase) lines.push(
        'To use the recovery phrase: open the Dashboard, click "Forgot your',
        'password?", enter the phrase and choose a new master password.',
        '',
    );
    lines.push(
        'Admin access covers the Dashboard, Nextcloud and Home Assistant.',
        'It does NOT unlock individual Vault items -- those are encrypted with each',
        "user's own password and cannot be recovered from here.",
        '',
    );
    return lines.join('\r\n');
}

function saveCredsSheet(text, filename) {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
    const a = Object.assign(document.createElement('a'), { href: url, download: filename });
    // Firefox has historically ignored a programmatic click on an anchor that is
    // not in the document, and cancelled the transfer when the object URL was
    // revoked in the same task as the click. Both are cheap to avoid.
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
}

// The one call site both pages use. Everything impure lives here and above.
function downloadCredsSheet(opts) {
    const date = new Date();
    saveCredsSheet(
        buildCredsSheet(Object.assign({}, opts, { device: location.hostname, date })),
        credsSheetFilename(date));
}
