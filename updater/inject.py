#!/usr/bin/env python3
"""Inject the Intel updater override into the checked-out upstream tree.

Reads pipeline/updater/update-override.ts, substitutes the build's release
repository, upstream commit and baked default channel, writes it into the
Electron main process sources and registers it from the actual bundler
entrypoint: entry.ts on tagged releases and recent main, main.ts on intermediate upstream.
Fails closed if the bundler entry is unknown rather than shipping dead code.
"""
import os
import sys
from pathlib import Path

workspace = Path(os.environ.get('GITHUB_WORKSPACE', '.'))
template = (workspace / 'pipeline/updater/update-override.ts').read_text()

replacements = {
    '__RELEASE_REPO__': os.environ['RELEASE_REPO'],
    '__CURRENT_SHA__': os.environ['UPSTREAM_SHA'],
    '__DEFAULT_CHANNEL__': os.environ['TRACK'],
}
if replacements['__DEFAULT_CHANNEL__'] not in ('bleeding-edge', 'stable'):
    sys.exit(f"TRACK must be 'bleeding-edge' or 'stable', got {replacements['__DEFAULT_CHANNEL__']!r}")

for placeholder, value in replacements.items():
    if template.count(placeholder) != 1:
        sys.exit(f'Placeholder {placeholder} expected exactly once in template ({template.count(placeholder)}x)')
    template = template.replace(placeholder, value, 1)

target = workspace / 'apps/desktop/electron/bleeding-edge-updater.ts'
target.write_text(template)

# Select the entry actually used by this upstream revision's desktop builder.
# Older tagged releases bundle entry.ts; current main bundles main.ts directly.
bundler = workspace / 'apps/desktop/scripts/bundle-electron-main.mjs'
if not bundler.exists():
    sys.exit('Desktop Electron bundler missing; cannot verify the entrypoint')
bundler_text = bundler.read_text()
main_entry = "entryPoints: [join(source, 'apps/desktop/electron/main.ts')]"
legacy_entry = "const mainEntry = resolve(root, 'electron/entry.ts')"
modern_entry = "entryPoints: [join(source, 'apps/desktop/electron/entry.ts')]"
matched_entries = [
    (marker, entry)
    for marker, entry in ((main_entry, 'main.ts'), (legacy_entry, 'entry.ts'), (modern_entry, 'entry.ts'))
    if bundler_text.count(marker) == 1
]
if len(matched_entries) == 1:
    via = matched_entries[0][1]
else:
    sys.exit('Unknown or ambiguous Electron main entrypoint; refuse inert override')
entry = workspace / 'apps/desktop/electron' / via
if not entry.exists():
    sys.exit(f'Expected Electron entrypoint {via} missing')
text = entry.read_text()
if 'installBleedingEdgeUpdater' in text:
    sys.exit(f'{via} already references installBleedingEdgeUpdater')
if via == 'entry.ts':
    anchor = "  await import('./main')"
    if text.count(anchor) != 1:
        sys.exit(f'entry.ts anchor not unique/found ({text.count(anchor)}x)')
    text = text.replace(
        anchor,
        anchor
        + "\n  const { installBleedingEdgeUpdater } = await import('./bleeding-edge-updater')\n  installBleedingEdgeUpdater()",
        1,
    )
else:
    # Import hoists; invocation executes after main.ts's module-scope IPC
    # registrations and before app.whenReady callbacks run.
    text += (
        "\nimport { installBleedingEdgeUpdater } from './bleeding-edge-updater'\n"
        'installBleedingEdgeUpdater()\n'
    )
entry.write_text(text)

print(f"Injected updater override via {via} (release repo {replacements['__RELEASE_REPO__']}, default channel {replacements['__DEFAULT_CHANNEL__']})")

# Recent upstream changed the About-version label to show the number of commits
# since the last tag (e.g. 0.21.5+2128). Keep the full version in IPC/build
# metadata but display only the release version in About and update views.
# Older stable tags predate these components and remain untouched.
label = workspace / 'apps/desktop/src/lib/version-label.ts'
if label.exists():
    for relative, anchor, replacement in (
        ('apps/desktop/src/components/update-status.tsx',
         'u.version(shortVersion(version.appVersion))',
         "u.version(shortVersion(version.appVersion).replace(/\+\d+$/, ''))"),
        ('apps/desktop/src/components/version-details.tsx',
         '`v${shortVersion(version.appVersion)}`',
         "`v${shortVersion(version.appVersion).replace(/\+\d+$/, '')}`"),
    ):
        file = workspace / relative
        source = file.read_text()
        if source.count(anchor) != 1:
            sys.exit(f'{relative} version-label anchor missing or ambiguous ({source.count(anchor)}x)')
        file.write_text(source.replace(anchor, replacement, 1))

    # The statusbar also names the client build; backend labels retain their
    # original version.
    status = workspace / 'apps/desktop/src/lib/version-status.ts'
    source = status.read_text()
    anchor = 'shortVersion(rawVersion) : null'
    if source.count(anchor) != 1:
        sys.exit(f'version-status.ts anchor missing or ambiguous ({source.count(anchor)}x)')
    status.write_text(source.replace(anchor, "(target === 'client' ? shortVersion(rawVersion).replace(/\\+\\d+$/, '') : shortVersion(rawVersion)) : null", 1))
    test = workspace / 'apps/desktop/src/lib/version-status.test.ts'
    assertions = test.read_text()
    before = "expect(status.label).toBe('v0.4.2+1913')"
    if assertions.count(before) != 1:
        sys.exit(f'version-status.test.ts assertion missing or ambiguous ({assertions.count(before)}x)')
    test.write_text(assertions.replace(before, "expect(status.label).toBe('v0.4.2')", 1))

    # Keep backend update checks and progress events, but avoid automatic
    # unauthenticated GitHub calls on boot/focus/daily timer. The client tab
    # and Check now continue to invoke the override on demand.
    store = workspace / 'apps/desktop/src/store/updates.ts'
    source = store.read_text()
    anchor = '  void checkUpdates()\n  void checkBackendUpdates()'
    if source.count(anchor) != 1:
        sys.exit(f'update poller anchor missing or ambiguous ({source.count(anchor)}x)')
    store.write_text(source.replace(anchor, '  void checkBackendUpdates()', 1))
    print('Cleaned client display version and disabled passive client GitHub checks')
