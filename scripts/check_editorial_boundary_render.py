#!/usr/bin/env python3
"""Render legal Chinese boundary fixtures through the actual editorial adapter.

Only synthetic local media; no servers, user data, model calls, or publishing.
Keep the MP4 and a decoded frame for independent visual review in addition to QC.
"""
import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'server'))
from content_domains import editorial_contract as contract
from content_domains import editorial_markup
from content_domains import video_compose_editorial as editorial
from content_domains import video_compose_media as media
from content_domains import video_compose_render as renderer
from scripts.check_editorial_layout import CASES, fixture_plan

BOUNDARY_CASES = ('ten-accent-glyphs', 'wide-title-callout')


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def preserve_error(folder, error):
    chain = []
    while error is not None:
        index = len(chain)
        chain.append({'type': type(error).__name__, 'message': str(error),
                      'returncode': getattr(error, 'returncode', None)})
        for stream in ('stdout', 'stderr'):
            value = getattr(error, stream, None)
            if value is not None:
                if isinstance(value, str):
                    value = value.encode('utf-8')
                (folder / f'error-{index}.{stream}').write_bytes(value)
        error = error.__cause__
    write_json(folder / 'error.json', chain)
    return chain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=pathlib.Path, required=True)
    args = parser.parse_args()
    folder = args.output_dir.absolute()
    folder.mkdir(parents=True, exist_ok=False)  # Never overwrite user files.
    compiler = pathlib.Path(editorial_markup.__file__)
    report = {'template_id': contract.TEMPLATE_ID, 'runtime': contract.HYPERFRAMES_VERSION,
              'compiler_sha256': editorial._hash(compiler), 'cases': [], 'result': 'failed'}
    try:
        command, browser = editorial.runtime_command()
        runtime = pathlib.Path(command[1]).parent.parent
        report['runtime_packages'] = {
            name: json.loads((runtime.parent / name / 'package.json').read_text(encoding='utf-8'))['version']
            for name in ('hyperframes', 'puppeteer-core', '@puppeteer/browsers')}
        source = folder / 'synthetic-source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=720x1280:rate=30:duration=5', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=48000:duration=5', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-af',
            'loudnorm=I=-16:TP=-1.5:LRA=11', '-movflags', '+faststart', str(source)],
            check=True, capture_output=True, timeout=120)
        report['source_sha256'] = editorial._hash(source)
        for name in BOUNDARY_CASES:
            case_dir = folder / name
            case_dir.mkdir()
            result = {'name': name, 'result': 'failed'}
            report['cases'].append(result)
            try:
                plan = contract.validate_plan(fixture_plan(CASES[name], name), 5)
                write_json(case_dir / 'plan.json', plan)
                payload = {'template_id': contract.TEMPLATE_ID, 'duration_ms': 5000,
                           'editorial_plan': plan}
                manifest = editorial.prepare_workspace(source, payload, case_dir / 'project',
                                                       renderer.TEMPLATE_ROOT)
                write_json(case_dir / 'manifest.json', manifest)
                output = case_dir / 'output.mp4'
                # Do not build a parallel render path or weaken its built-in check,
                # high quality, workers=1, low-memory, strict, or output validation.
                # Inherit the normal short global TMPDIR rather than a case path.
                result['render'] = editorial.render(source, payload, output, renderer.TEMPLATE_ROOT)
                if result['render']['build_manifest'] != manifest:
                    raise AssertionError('Prepared and rendered compiler manifests differ')
                streams = json.loads(subprocess.check_output(['ffprobe', '-v', 'error',
                    '-show_streams', '-of', 'json', str(output)], timeout=30))['streams']
                videos = [stream for stream in streams if stream['codec_type'] == 'video']
                audios = [stream for stream in streams if stream['codec_type'] == 'audio']
                if (len(videos) != 1 or len(audios) != 1 or videos[0]['codec_name'] != 'h264'
                        or audios[0]['codec_name'] != 'aac' or videos[0]['avg_frame_rate'] != '30/1'
                        or (videos[0]['width'], videos[0]['height']) != (720, 1280)):
                    raise AssertionError('Expected exactly one 720x1280 30fps H264 stream and one AAC stream')
                result['media'] = media.probe_media(output)
                if abs(result['media']['duration_ms'] - 5000) > 150:
                    raise AssertionError('Boundary render duration is outside the adapter tolerance')
                result['quality'] = media.inspect_quality(output)
                if result['quality']['decision'] != 'passed':
                    raise AssertionError('Boundary render failed the unchanged media QC')
                frame = case_dir / 'frame-2.5s.png'
                subprocess.run(['ffmpeg', '-v', 'error', '-ss', '2.5', '-i', str(output),
                    '-frames:v', '1', str(frame)], check=True, capture_output=True, timeout=30)
                if not frame.is_file() or frame.stat().st_size == 0:
                    raise AssertionError('Decoded 2.5 second frame is missing')
                if editorial._hash(compiler) != report['compiler_sha256']:
                    raise AssertionError('Compiler changed while rendering; rerun the evidence on one version')
                result.update({'result': 'passed', 'output': 'output.mp4',
                    'output_sha256': editorial._hash(output), 'frame': frame.name,
                    'frame_sha256': editorial._hash(frame), 'frame_at_seconds': 2.5,
                    'video_stream_count': len(videos), 'audio_stream_count': len(audios)})
            except Exception as error:
                result['error'] = preserve_error(case_dir, error)
                raise
            finally:
                write_json(case_dir / 'report.json', result)
                write_json(folder / 'report.json', report)
        report['result'] = 'passed'
    except Exception as error:
        report['error'] = preserve_error(folder, error)
        raise
    finally:
        write_json(folder / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
