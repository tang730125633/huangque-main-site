"""Frozen visual compiler from the approved local bilingual template.

Keep layout/motion changes versioned; this module receives validated data only.
"""
import html


def caption_markup(value, keywords):
    accented = [False] * len(value)
    for kw in keywords:
        start = 0
        while (start := value.find(kw, start)) >= 0:
            for i in range(start, start + len(kw)):
                accented[i] = True
            start += len(kw)
    # One span per glyph: static markup, no HTML supplied by the Agent is executed.
    return ''.join(f'<span class="glyph{" accent" if accented[i] else ""}">{html.escape(ch)}</span>' for i,ch in enumerate(value))

def make_html(job):
    duration = job['duration']
    title = ''.join(f'<div class="title-line"><span class="title-ink">{html.escape(line)}</span></div>' for line in job['title'])
    captions = []
    motion = []
    # Paired short phrases retain the previous line until the second one completes.
    for i, c in enumerate(job['captions']):
        partner = job['captions'][min(i + (1 if i % 2 == 0 else 0), len(job['captions']) - 1)]
        end = partner['end']
        next_pair = (i // 2 + 1) * 2
        if next_pair < len(job['captions']):
            end = min(end, job['captions'][next_pair]['start'] - 0.045)
        css = 'upper' if i % 2 == 0 else 'lower'
        en_size=26 if len(c['en'])<=44 else 24
        captions.append(f'<div id="c{i}" class="caption {css} clip" data-start="{c["start"]}" data-duration="{round(end-c["start"],4)}" data-track-index="{10+i%2}" data-layout-allow-caption-zone><div class="caption-text">{caption_markup(c["text"],job["keywords"])}</div><div id="en{i}" class="english" lang="en" style="font-size:{en_size}px">{html.escape(c["en"])}</div></div>')
        # The phrase starts at the real ASR boundary; glyph cascade is styling, not forced alignment.
        motion.append(f'tl.fromTo("#c{i} .glyph",{{opacity:0,y:7}},{{opacity:1,y:0,duration:0.11,stagger:0.022,ease:"power2.out",immediateRender:false}},{c["start"]});')
    callouts = []
    for i,c in enumerate(job['callouts']):
        callouts.append(f'<div id="q{i}" class="callout {c["side"]} clip" data-start="{c["start"]}" data-duration="{round(c["end"]-c["start"],4)}" data-track-index="20">{html.escape(c["text"])}</div>')
        motion.append(f'tl.fromTo("#q{i}",{{scale:0.76,opacity:0}},{{scale:1,opacity:1,duration:0.18,ease:"back.out(1.3)",immediateRender:false}},{c["start"]});')
    first = job['camera'][0]['scale']
    motion.append(f'tl.set("#cropA",{{scale:{first},transformOrigin:"52% 38%"}},0);')
    motion.append('tl.set("#incoming",{opacity:0,x:720},0);')
    for s in job['camera'][1:]:
        at, scale = s['at'], s['scale']
        if s['transition'] == 'cut':
            motion.append(f'tl.set("#cropA",{{scale:{scale}}},{at});')
        else:
            motion.extend([
                f'tl.set("#incoming",{{opacity:1,x:720}},{at});',
                f'tl.set("#cropB",{{scale:{scale},transformOrigin:"52% 38%"}},{at});',
                f'tl.fromTo("#outgoing",{{x:0}},{{x:-720,duration:0.28,ease:"power2.inOut",immediateRender:false}},{at});',
                f'tl.fromTo("#incoming",{{x:720}},{{x:0,duration:0.28,ease:"power2.inOut",immediateRender:false}},{at});',
                f'tl.fromTo("#streak",{{attr:{{stdDeviation:"0 0"}}}},{{attr:{{stdDeviation:"18 0"}},duration:0.14,ease:"power1.in",immediateRender:false}},{at});',
                f'tl.to("#streak",{{attr:{{stdDeviation:"0 0"}},duration:0.14,ease:"power1.out"}},{at+0.14});',
                f'tl.set("#cropA",{{scale:{scale}}},{at+0.28});',
                f'tl.set("#outgoing",{{x:0}},{at+0.28});',
                f'tl.set("#incoming",{{opacity:0,x:720}},{at+0.28});',
            ])
    motion.append('tl.set(".keyword-punch",{scale:1,transformOrigin:"52% 38%"},0);')
    for p in job.get('keyword_punches',[]):
        at=p['at']
        motion.extend([
            f'tl.addLabel("keyword-{at}",{at});',
            f'tl.fromTo(".keyword-punch",{{scale:1}},{{scale:{p["strength"]},duration:0.18,ease:"power2.inOut",immediateRender:false}},{at});',
            f'tl.to(".keyword-punch",{{scale:1,duration:0.24,ease:"power2.inOut"}},{round(at+.43,3)});',
        ])
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=720,height=1280">
<script src="assets/gsap.min.js"></script><style>
@font-face{{font-family:EditorialSerif;src:url('assets/SourceHanSerifSC-Heavy.otf') format('opentype');font-weight:900;font-display:block}}
@font-face{{font-family:EnglishSans;src:url('assets/NotoSansSC-Regular.otf') format('opentype');font-weight:400;font-display:block}}
*{{box-sizing:border-box;margin:0;padding:0}}html,body,#root{{width:720px;height:1280px;overflow:hidden;background:#151515}}
#root{{position:relative;font-family:EditorialSerif,serif;font-weight:900}}
.camera,.crop,.keyword-punch,video{{position:absolute;inset:0;width:720px;height:1280px}}
.camera{{overflow:hidden}}video{{object-fit:cover}}#outgoing,#incoming{{filter:url(#horizontalBlur)}}
#incoming{{opacity:0}}.crop{{transform-origin:52% 38%}}
.title{{position:absolute;left:32px;top:176px;width:656px;z-index:4;text-align:center;color:#74160e}}
.title-line{{height:80px;display:flex;justify-content:center;align-items:center}}
.title-ink{{display:inline-block;flex-shrink:0;white-space:nowrap;font-size:62px;line-height:1.24;letter-spacing:-1px;transform:scaleX(.82) skewX(-6deg);-webkit-text-stroke:3.5px #fffdf8;paint-order:stroke fill;text-shadow:0 2px 2px #36302b44}}
.caption{{position:absolute;width:604px;height:122px;z-index:5;color:#fffdf8;white-space:nowrap}}
.caption.upper{{left:58px;top:798px;text-align:left}}.caption.lower{{right:58px;top:934px;text-align:right}}
.caption-text{{height:84px;font-size:54px;line-height:84px;letter-spacing:-1.1px;transform:scaleX(.87);transform-origin:left center;-webkit-text-stroke:1.6px #181311;paint-order:stroke fill;text-shadow:1px 2px 2px #181311aa}}
.caption.lower .caption-text{{transform-origin:right center}}
.glyph{{display:inline-block}}.accent{{font-size:74px;color:#8b2025;-webkit-text-stroke:3px #fffdf8;text-shadow:0 0 5px #fffdf8cc,1px 2px 3px #18131166}}
.english{{height:32px;line-height:32px;width:max-content;max-width:100%;padding:0 6px;border-radius:3px;background:#181311cc;font-family:EnglishSans,sans-serif;font-weight:400;color:#fffdf8;letter-spacing:-.25px;-webkit-text-stroke:0;paint-order:normal;text-shadow:0 1px 2px #181311,0 -1px 2px #181311,1px 0 2px #181311,-1px 0 2px #181311}}
.caption.lower .english{{margin-left:auto}}
.callout{{position:absolute;top:641px;width:230px;text-align:center;z-index:6;color:#74160e;font-size:52px;line-height:1.5;-webkit-text-stroke:3px #fffdf8;paint-order:stroke fill;text-shadow:0 2px 3px #fffdf899}}
.callout.left{{left:24px}}.callout.right{{right:24px}}
</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{duration}" data-width="720" data-height="1280">
<svg width="0" height="0" style="position:absolute" data-layout-ignore><defs><filter id="horizontalBlur" x="-20%" width="140%" y="0" height="100%"><feGaussianBlur id="streak" stdDeviation="0 0"/></filter></defs></svg>
<div id="outgoing" class="camera"><div id="cropA" class="crop" data-layout-allow-overflow><div id="punchA" class="keyword-punch" data-layout-allow-overflow><video id="videoA" class="clip" data-start="0" data-duration="{duration}" data-track-index="0" src="assets/source.mp4" muted playsinline></video></div></div></div>
<div id="incoming" class="camera"><div id="cropB" class="crop" data-layout-allow-overflow><div id="punchB" class="keyword-punch" data-layout-allow-overflow><video id="videoB" class="clip" data-start="0" data-duration="{duration}" data-track-index="1" src="assets/source.mp4" muted playsinline></video></div></div></div>
<audio id="narration" class="clip" data-start="0" data-duration="{duration}" data-track-index="2" src="assets/source.mp4" data-volume="1"></audio>
<div id="headline" class="title">{title}</div>
{''.join(captions)}{''.join(callouts)}
</div><script>
window.__timelines=window.__timelines||{{}};
// Freeze text layout before registering the seekable timeline. Character counts
// do not predict the width of this bundled proportional font.
Promise.all([document.fonts.load('400 26px EnglishSans'),
             document.fonts.load('900 74px EditorialSerif')])
.then(() => document.fonts.ready).then(() => {{
  for (const english of document.querySelectorAll('.english')) {{
    // A temporary probe also works when the framework initially hides clips.
    const probe=english.cloneNode(true);
    probe.removeAttribute('id');
    Object.assign(probe.style,{{position:'fixed',left:'0',top:'0',visibility:'hidden',
      display:'block',margin:'0',maxWidth:'604px',whiteSpace:'nowrap'}});
    document.body.appendChild(probe);
    try {{
      const text=document.createRange();
      text.selectNodeContents(probe);
      if (text.getBoundingClientRect().width > 592) {{
        // Preserve the original font size where two natural lines suffice;
        // even an unbroken 58-character token may wrap, never be clipped.
        Object.assign(probe.style,{{whiteSpace:'normal',overflowWrap:'anywhere',
          width:'604px',height:'auto',minHeight:'32px',lineHeight:'32px'}});
        const maximum=parseFloat(english.style.fontSize);
        let fitted=false;
        for (let size=maximum; size>=20; size-=0.25) {{
          probe.style.fontSize=size+'px';
          if (probe.getBoundingClientRect().height<=64 &&
              text.getBoundingClientRect().width<=592.5) {{fitted=true;break;}}
        }}
        if (!fitted) throw new Error('English caption cannot fit the frozen two-line area');
        for (const key of ['whiteSpace','overflowWrap','width','height','minHeight','lineHeight','fontSize'])
          english.style[key]=probe.style[key];
      }}
      const height=84+probe.getBoundingClientRect().height+6;
      english.parentElement.style.height=height+'px';
      if (english.parentElement.classList.contains('upper')) {{
        const next=english.parentElement.nextElementSibling;
        if (next && next.classList.contains('lower'))
          next.style.top=(934+Math.max(0,height-122))+'px';
      }}
    }} finally {{probe.remove();}}
  }}
  const tl=gsap.timeline({{paused:true}});
  {chr(10).join(motion)}
  window.__timelines["main"]=tl;
}});
</script></body></html>'''
