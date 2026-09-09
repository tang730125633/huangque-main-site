#!/usr/bin/env python3
"""Real, offline English-caption layout regression against the locked renderer."""
import argparse
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from content_domains import editorial_contract as contract
from content_domains import video_compose_editorial as editorial
from content_domains import video_compose_render as renderer

REPORTED_ENGLISH = 'Our community empowers women with meaningful opportunities'
CASES = {
    'short-original': ['One person', 'One computer'],
    'reported-lower': ['One person', REPORTED_ENGLISH],
    'reported-both': [REPORTED_ENGLISH, REPORTED_ENGLISH],
    'wide-58': ['W' * 58, 'W' * 58],
    'wide-44': ['W' * 44, 'W' * 44],
    'escaped-text': ['A <b>literal</b> caption & a quoted "word"', 'One computer'],
    'accent-short': ['Small', 'One person'],
    'mixed-three-pairs': [REPORTED_ENGLISH, 'One person', 'Small', 'W' * 58,
                          'W' * 44, REPORTED_ENGLISH],
}


def fixture_plan(english):
    # Keep the reviewer's short-English/accent-glyph failure, and a longer
    # sequence whose pairs have different one/two-line combinations.
    accent = english == CASES['accent-short'] or len(english) > 2
    word = '内容' if accent else '一个人'
    if len(english) == 2:
        captions = [{'start': .5, 'end': 2, 'text': '内容有价值' if accent else '一个人', 'en': english[0]},
                    {'start': 2.1, 'end': 4.5, 'text': '内容有价值' if accent else '一台电脑', 'en': english[1]}]
    else:
        captions = [{'start': round(.2 + i * 4.4 / len(english), 3),
                     'end': round(.2 + (i + 1) * 4.4 / len(english) - .06, 3),
                     'text': '内容有价值', 'en': line} for i, line in enumerate(english)]
    return {'schema': contract.SCHEMA_ID, 'timebase': 'edited_output',
        'transcript_hash': 'a' * 64, 'edit_decision_version': 1,
        'title': ['一个人的效率', '让内容更有价值'],
        'captions': captions,
        'keywords': [word], 'camera': [{'at': 0, 'scale': 1, 'transition': 'cut'}],
        'keyword_punches': [{'at': .5, 'word': word, 'strength': 1.075}], 'callouts': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=pathlib.Path, required=True)
    args = parser.parse_args()
    command, browser = editorial.runtime_command()
    root = args.output_dir.absolute()
    root.mkdir(parents=True, exist_ok=False)  # Never overwrite user files.
    source = root / 'synthetic-source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=720x1280:rate=30:duration=5', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=48000:duration=5', '-c:v', 'libx264',
        '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)],
        check=True, capture_output=True, timeout=120)
    environment = {**os.environ, 'HYPERFRAMES_BROWSER_PATH': browser,
        'HYPERFRAMES_NO_TELEMETRY': '1', 'HYPERFRAMES_SKIP_SKILLS': '1',
        'DO_NOT_TRACK': '1', 'TEMP': str(root), 'TMP': str(root), 'TMPDIR': str(root)}
    fixtures = []
    for name, english in CASES.items():
        plan = contract.validate_plan(fixture_plan(english), 5)
        workspace = root / name
        manifest = editorial.prepare_workspace(source, {'editorial_plan': plan,
            'template_id': contract.TEMPLATE_ID, 'duration_ms': 5000}, workspace,
            renderer.TEMPLATE_ROOT)
        fixtures.append({'name': name, 'workspace': str(workspace),
                         'english': english, 'manifest': manifest})
    fixtures_path = root / 'fixtures.json'
    fixtures_path.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2), encoding='utf-8')
    # Resolve Puppeteer through the selected runtime, not a separately installed version.
    runtime = pathlib.Path(command[1]).resolve().parent.parent
    try:
        measured = editorial._run_owned([command[0], str(ROOT / 'scripts/check_editorial_layout.mjs'),
            str(runtime), browser, str(fixtures_path)], root, environment, 180)
    except subprocess.CalledProcessError as error:
        (root / 'geometry.stdout').write_bytes(error.output or b'')
        (root / 'geometry.stderr').write_bytes(error.stderr or b'')
        raise
    (root / 'geometry.stdout').write_bytes(measured.stdout)
    (root / 'geometry.stderr').write_bytes(measured.stderr)
    checks = []
    for fixture in fixtures:
        name, workspace = fixture['name'], pathlib.Path(fixture['workspace'])
        try:
            result = editorial._run_owned(command + ['check', str(workspace), '--json'],
                workspace, environment, 180)
        except subprocess.CalledProcessError as error:
            (root / (name + '-check.stdout')).write_bytes(error.output or b'')
            (root / (name + '-check.stderr')).write_bytes(error.stderr or b'')
            raise
        payload = json.loads(result.stdout)
        (root / (name + '-check.json')).write_bytes(result.stdout)
        (root / (name + '-check.stderr')).write_bytes(result.stderr)
        if not payload.get('ok') or not payload.get('layout', {}).get('samples'):
            raise AssertionError(name + ': real layout audit did not pass/run')
        checks.append({'name': name, 'ok': True, 'html_sha256': fixture['manifest']['html_sha256']})
    report = {'runtime': contract.HYPERFRAMES_VERSION, 'cases': checks,
              'browser_measurements': 'measurements.json', 'result': 'passed'}
    (root / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
