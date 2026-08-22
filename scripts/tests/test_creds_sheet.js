/* Tests for src/static/creds_sheet.js.
 *
 *   osascript -l JavaScript scripts/tests/test_creds_sheet.js     (from repo root)
 *   node scripts/tests/test_creds_sheet.js                        (same, in CI)
 *
 * JavaScriptCore via osascript is the only JS runtime on the development Mac —
 * no node, deno or bun — and it is the engine Safari runs, so it is the one that
 * matters locally. CI has node and not osascript, and a regression guard that
 * only fires on one developer's laptop is a guard that rots, so the handful of
 * runtime-specific calls below are abstracted rather than picking a side.
 * buildCredsSheet() itself is pure and takes its clock and hostname as
 * arguments, which is what lets it run anywhere at all.
 * See docs/plans/RECOVERY_SHEET.md §6.
 */

var IS_JXA = (typeof ObjC !== 'undefined');
if (IS_JXA) { ObjC.import('Foundation'); ObjC.import('stdlib'); }

var MODULE = 'src/static/creds_sheet.js';
var failures = 0;

function exit(code) { if (IS_JXA) { $.exit(code); } else { process.exit(code); } }

function readFile(path) {
    if (!IS_JXA) return require('fs').readFileSync(path, 'utf8');
    var s = $.NSString.stringWithContentsOfFileEncodingError(
        $(path), $.NSUTF8StringEncoding, null);
    if (!s) {
        console.log('FAIL  cannot read ' + path + ' — run from the repo root');
        exit(1);
    }
    return ObjC.unwrap(s);
}

function check(name, cond) {
    if (cond) { console.log('ok    ' + name); }
    else { console.log('FAIL  ' + name); failures++; }
}

eval(readFile(MODULE));

var DATE = new Date(2026, 7, 20, 9, 5);      // 2026-08-20 09:05, local
var PHRASE = 'wobble tundra deputy chrome amulet salsa';
var PW = 'correct-horse-battery-staple';

function sheet(opts) {
    return buildCredsSheet(Object.assign(
        { device: 'homebrain.local', date: DATE }, opts));
}

var both = sheet({ password: PW, phrase: PHRASE });
var phraseOnly = sheet({ phrase: PHRASE });
var pwOnly = sheet({ password: PW });
var changed = sheet({ password: PW, phraseUnchanged: true });

// --- 1-4: the conditional blocks ----------------------------------------
check('phrase-only omits the password line', phraseOnly.indexOf('Master password:') === -1);
check('phrase-only keeps the how-to-recover paragraph', phraseOnly.indexOf('Forgot your') !== -1);
check('phrase-only describes dead-box restore', phraseOnly.indexOf('If the box is gone') !== -1);
check('password-only omits the phrase line', pwOnly.indexOf('Recovery phrase:') === -1);
check('password-only drops the how-to-recover paragraph', pwOnly.indexOf('Forgot your') === -1);
check('password-only does not promise dead-box restore via phrase', pwOnly.indexOf('If the box is gone') === -1);
check('both carries the password', both.indexOf('Master password:  ' + PW) !== -1);
check('both carries the phrase', both.indexOf('Recovery phrase:  ' + PHRASE) !== -1);
check('phraseUnchanged states the phrase still works',
      changed.indexOf('Recovery phrase:  unchanged -- this change did not affect it.') !== -1);
// An owner who regenerated their phrase has a live phrase on a LATER sheet, so
// pointing them at "the sheet from setup" would aim them at a retired secret.
check('phraseUnchanged names no particular sheet', changed.indexOf('setup') === -1);
check('phraseUnchanged never prints a phrase', changed.indexOf(PHRASE) === -1);
check('phraseUnchanged drops the how-to-recover paragraph', changed.indexOf('Forgot your') === -1);
check('timestamp is hand-formatted, not locale-formatted',
      both.indexOf('Generated: 2026-08-20 09:05') !== -1);

// A sheet whose two adjacent lines disagree about the phrase is the one thing it
// must never be, so the options are mutually exclusive rather than additive.
var conflicted = sheet({ password: PW, phrase: PHRASE, phraseUnchanged: true });
check('a real phrase wins over the unchanged note',
      conflicted.indexOf(PHRASE) !== -1 && conflicted.indexOf('unchanged --') === -1);

// --- 5-6: the file is printable anywhere --------------------------------
var all = [both, phraseOnly, pwOnly, changed];
check('body is pure ASCII', all.every(function (t) { return /^[\x20-\x7e\r\n]*$/.test(t); }));
check('every newline is CRLF', all.every(function (t) { return !/[^\r]\n/.test(t); }));

// --- 7: filename --------------------------------------------------------
var fname = credsSheetFilename(DATE);
check('filename has second resolution', fname === 'homebrain-recovery-2026-08-20T09-05-00.txt');
check('filename shape', /^homebrain-recovery-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.txt$/.test(fname));
// The repeat that actually happens is a second click after the first save went
// unnoticed -- both land in the same minute.
check('two clicks in one minute get different names',
      credsSheetFilename(new Date(2026, 7, 20, 9, 5, 1)) !== fname);
check('filename leaks no secret', fname.indexOf(PW) === -1 && fname.indexOf('homebrain.local') === -1);

// --- 8: regression against the sheet that actually shipped ---------------
// Verbatim body of the pre-consolidation builder (installing.html @ 6fcee05),
// parameterised only where it read a closure variable. A broken handover sheet
// costs a factory reset, so the wording is pinned, not merely reviewed.
function legacyBuild(handoverCreds) {
    const lines = [
        'HomeBrain — recovery sheet',
        `Generated: PINNED`,
        `Device:    homebrain.local`,
        '',
    ];
    if (handoverCreds.password) lines.push(`Master password:  ${handoverCreds.password}`);
    if (handoverCreds.phrase) lines.push(`Recovery phrase:  ${handoverCreds.phrase}`);
    lines.push(
        '',
        'Keep this offline — print it, or put it on a USB stick. Anyone holding',
        'it can reset administrative access to this device and open encrypted',
        'backups made after backup-unlock was enabled.',
        '',
    );
    if (handoverCreds.phrase) lines.push(
        'To use the recovery phrase: open the Dashboard, click "Forgot your',
        'password?", enter the phrase and choose a new master password.',
        'If the box is gone: plug the backup drive into a new HomeBrain, tick',
        '"Restore system", and enter this phrase (or the master password).',
        'An off-site copy works the same way if you have one.',
        '',
    );
    lines.push(
        'Admin access covers the Dashboard, Nextcloud and Home Assistant.',
        'It does NOT unlock individual Vault items — those are encrypted with each',
        "user's own password and cannot be recovered from here.",
        '',
    );
    return lines.join('\n');
}

// Compare everything but the timestamp line, which changed format on purpose.
function comparable(text) {
    var lines = text.replace(/\r\n/g, '\n').replace(/—/g, '--').split('\n');
    lines.splice(1, 1);
    return lines.join('\n');
}

[['handover (both)', both, { password: PW, phrase: PHRASE }],
 ['phrase reveal', phraseOnly, { phrase: PHRASE }],
 ['wordlist-missing handover', pwOnly, { password: PW }]].forEach(function (c) {
    check('wording unchanged vs shipped sheet — ' + c[0],
          comparable(c[1]) === comparable(legacyBuild(c[2])));
});

// --- 9: saveCredsSheet's contract with the DOM ---------------------------
// Not a browser test — it cannot be, there is no browser here. It pins the two
// fixes from RECOVERY_SHEET.md F5.1/F5.2, which are otherwise unverifiable:
// the anchor must be in the document when clicked, and the object URL must NOT
// be revoked in the same task as the click.
// One ordered log, not a set of counters: counters would still pass if append
// and click were swapped, which is precisely the defect being guarded against.
var log = [], el = null, revoked = [], deferred = [], blob = null;
var Blob = function (parts, opts) { this.parts = parts; this.type = opts && opts.type; };
var URL = {
    createObjectURL: function (b) { blob = b; log.push('create'); return 'blob:test'; },
    revokeObjectURL: function (u) { revoked.push(u); log.push('revoke'); },
};
var document = {
    createElement: function () {
        el = { click: function () { log.push('click'); }, remove: function () { log.push('remove'); } };
        return el;
    },
    body: { appendChild: function (e) { log.push(e === el ? 'append' : 'append-wrong'); } },
};
var setTimeout = function (fn) { deferred.push(fn); };

saveCredsSheet(both, fname);

check('blob carries the sheet text', blob.parts[0] === both);
check('blob declares its charset', blob.type === 'text/plain;charset=utf-8');
check('anchor gets the filename', el.download === fname && el.href === 'blob:test');
check('append, then click, then remove — in that order',
      log.join(',') === 'create,append,click,remove');
check('object URL is not revoked in the click task', revoked.length === 0 && deferred.length === 1);
deferred[0]();
check('object URL is revoked once deferred',
      revoked[0] === 'blob:test' && log.join(',') === 'create,append,click,remove,revoke');

console.log(failures ? '\n' + failures + ' FAILED' : '\nall passed');
exit(failures ? 1 : 0);
