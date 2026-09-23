"""Real word timing and bilingual cues for narration-driven template jobs."""
import difflib
import hashlib
import json
import math
import threading
import time
import unicodedata
import urllib.error
import urllib.request

TEMPLATE_ID = "bilingual-stagger-salon"
_SLOTS = threading.BoundedSemaphore(2)


def ensure_ready():
    from . import matrix_template_semantics as semantics, video_compose_asr as asr
    if not semantics.OPENAI_KEY or not asr.OPENAI_KEY:
        raise ValueError("双语字幕的翻译或语音对齐服务未配置")


def _remaining(deadline):
    from . import task_termination
    task_termination.check()
    remaining = deadline-time.time()
    if remaining <= 0:
        raise TimeoutError("双语字幕准备超时")
    return remaining


def _units(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',text).casefold() if c.isalnum())


def align_cues(text, translated, words, audio_duration, fingerprint):
    if not .1 <= audio_duration <= 60:
        raise ValueError("双语模板配音需在 60 秒以内")
    if not isinstance(translated,list) or not 1 <= len(translated) <= 60:
        raise ValueError("双语字幕分段无效")
    if any(not isinstance(c,dict) or not isinstance(c.get('text'),str)
           or not 1<=len(c['text'])<=10 or not isinstance(c.get('en'),str)
           or not 1<=len(c['en'])<=48 for c in translated):
        raise ValueError("双语字幕过长或缺少翻译")
    original=_units(text)
    copy_key=lambda s: ''.join(unicodedata.normalize('NFKC',s).split())
    if copy_key(''.join(c['text'] for c in translated)) != copy_key(text):
        raise ValueError("双语字幕内容与口播原文不一致")
    recognized=[]; timings=[]
    for word in words:
        if word.get('timing_source') != 'provider_word':
            raise ValueError("双语字幕必须使用真实词级时间，不允许估算")
        units=_units(str(word.get('text') or ''))
        start=float(word['start_ms'])/1000;end=float(word['end_ms'])/1000
        if not units:
            continue
        if not math.isfinite(start+end) or not 0<=start<end<=audio_duration+.1:
            raise ValueError("语音识别返回无效词时间")
        for i,unit in enumerate(units):
            recognized.append(unit);timings.append((start+(end-start)*i/len(units),start+(end-start)*(i+1)/len(units)))
    mapping={}
    for block in difflib.SequenceMatcher(None,original,''.join(recognized),autojunk=False).get_matching_blocks():
        for i in range(block.size):mapping[block.a+i]=block.b+i
    if len(mapping)!=len(original):
        raise ValueError("配音识别存在漏字或错字，无法可靠对齐")
    duration=math.ceil((audio_duration+.6)*30-1e-7)/30
    cues=[];offset=0
    for i,chunk in enumerate(translated):
        count=len(_units(chunk['text']))
        if not count:
            raise ValueError("字幕短句没有有效文字")
        first=timings[mapping[offset]][0]; cursor=offset; times=[]
        for char in chunk['text']:
            units=_units(char)
            when=timings[mapping[cursor]][0] if units else (times[-1] if times else first)
            times.append(round(when,6));cursor+=len(units)
        english=' '.join(chunk['en'].split())
        english_end=times[0]+.05+.035*max(0,len(english.split())-1)+.18
        end=max(timings[mapping[offset+count-1]][1]+.15,times[-1]+.2,english_end)
        offset+=count
        yellow=chunk.get('yellow',[])
        if not isinstance(yellow,list) or any(type(n) is not int or not 0<=n<len(chunk['text']) for n in yellow):
            raise ValueError("字幕关键词索引无效")
        cues.append(dict(text=chunk['text'],en=english,times=times,end=min(duration,end),row=i%2,yellow=yellow))
    cues[-1]['end']=duration
    for i,cue in enumerate(cues):
        if i+2<len(cues):cue['end']=min(cue['end'],cues[i+2]['times'][0]+.055)
        if max(cue['times'][-1]+.2,cue['times'][0]+.23+.035*max(0,len(cue['en'].split())-1))>cue['end']+.001:
            raise ValueError("字幕过密，无法完成逐字入场，请降低语速")
    return dict(version=1,audio_duration=audio_duration,duration=duration,audio_fingerprint=fingerprint,cues=cues)


def prepare(audio, text, deadline):
    from . import matrix_template_semantics as semantic, video_compose_asr as asr
    ensure_ready()
    duration=float(audio['duration'])
    if not .1<=duration<=60:
        raise ValueError("双语模板配音需在 60 秒以内")
    if not _SLOTS.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("等待双语字幕服务超时")
    try:
        path=audio['path'];fingerprint=hashlib.sha256(path.read_bytes()).hexdigest()
        body,content_type=asr._multipart([('model',asr.ASR_MODEL),('language','zh'),('prompt',text),
            ('response_format','verbose_json'),('timestamp_granularities[]','word')],'file',path)
        base=asr.OPENAI_TRANSCRIBE_BASE.rstrip('/')
        url=base+('/audio/transcriptions' if base.endswith('/v1') else '/v1/audio/transcriptions')
        request=urllib.request.Request(url,data=body,headers={'Authorization':'Bearer '+asr.OPENAI_KEY,'Content-Type':content_type},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=min(120,_remaining(deadline))) as response:
                recognized=asr.parse_verbose_response(json.load(response))
            prompt=('将口播原文按完整语义拆成每段最多10个Unicode字符的中文字幕，并逐句翻译成最多48字符的简洁英文。'
                    '不得改写或增删原文和标点，不拆开词组，不添加承诺。yellow是核心关键词的零基Unicode字符索引。'
                    '只返回JSON：{"cues":[{"text":"原文短句","en":"English","yellow":[]}]}。')
            request=urllib.request.Request(semantic._chat_url(),method='POST',
                headers={'Authorization':'Bearer '+semantic.OPENAI_KEY,'Content-Type':'application/json'},
                data=json.dumps({'model':semantic.MODEL,'temperature':0,'response_format':{'type':'json_object'},
                                 'max_tokens':2000,'messages':[{'role':'system','content':prompt},{'role':'user','content':text}]},ensure_ascii=False).encode())
            with urllib.request.urlopen(request,timeout=min(45,_remaining(deadline))) as response:
                translated=json.loads(json.load(response)['choices'][0]['message']['content'])['cues']
        except (urllib.error.URLError,KeyError,TypeError,json.JSONDecodeError) as exc:
            raise RuntimeError("双语字幕对齐或翻译失败，请重试") from exc
        _remaining(deadline)
        return align_cues(text,translated,recognized['words'],duration,fingerprint)
    finally:
        _SLOTS.release()
