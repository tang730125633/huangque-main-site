#!/usr/bin/env python3
"""Offline release preflight and synthetic smoke render. No server or provider calls."""
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
from content_domains import video_compose_media as media
from content_domains import video_compose_render as renderer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=pathlib.Path)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    command, browser = editorial.runtime_command()
    version = subprocess.check_output([command[0], '--version'], timeout=20).decode().strip()
    if int(version.lstrip('v').split('.')[0]) < 22:
        raise ValueError('Node 22 or later is required')
    editorial.verify_assets(renderer.TEMPLATE_ROOT)
    subprocess.run(['ffmpeg', '-version'], check=True, capture_output=True, timeout=20)
    subprocess.run(['ffprobe', '-version'], check=True, capture_output=True, timeout=20)
    report = {'template_id': contract.TEMPLATE_ID, 'runtime': contract.HYPERFRAMES_VERSION,
              'node': version, 'assets_verified': True}
    if args.output_dir:
        # Explicit output must be a NEW directory; never overwrite user media.
        folder = args.output_dir.absolute()
        folder.mkdir(parents=True, exist_ok=False)
        source = folder / 'synthetic-source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=720x1280:rate=30:duration=5', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=48000:duration=5', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-af',
            'loudnorm=I=-16:TP=-1.5:LRA=11', '-movflags', '+faststart', str(source)],
            check=True, capture_output=True, timeout=120)
        plan = {'schema': contract.SCHEMA_ID, 'timebase': 'edited_output',
            'transcript_hash': 'a' * 64, 'edit_decision_version': 1,
            'title': ['口播网感模板', '双语关键词测试'],
            'captions': [{'start': .5, 'end': 2, 'text': '一个人', 'en': 'One person'},
                         {'start': 2.1, 'end': 4.5, 'text': '一台电脑', 'en': 'One computer'}],
            'keywords': ['一个人'], 'camera': [{'at': 0, 'scale': 1, 'transition': 'cut'}],
            'keyword_punches': [{'at': .5, 'word': '一个人', 'strength': 1.075}], 'callouts': []}
        payload = {'template_id': contract.TEMPLATE_ID, 'duration_ms': 5000, 'editorial_plan': plan}
        editorial.prepare_workspace(source, payload, folder / 'project', renderer.TEMPLATE_ROOT)
        if not args.prepare_only:
            output = folder / 'smoke.mp4'
            report['render'] = editorial.render(source, payload, output, renderer.TEMPLATE_ROOT)
            streams = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                '-of', 'json', str(output)], timeout=30))['streams']
            videos = [s for s in streams if s['codec_type'] == 'video']
            audios = [s for s in streams if s['codec_type'] == 'audio']
            if len(videos) != 1 or len(audios) != 1 or videos[0]['avg_frame_rate'] != '30/1':
                raise ValueError('Expected one 30 fps video and exactly one original audio track')
            report['quality'] = media.inspect_quality(output)
            if report['quality']['decision'] != 'passed':
                raise ValueError('Synthetic smoke output failed media QC')
            report['output_sha256'] = editorial._hash(output)
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
