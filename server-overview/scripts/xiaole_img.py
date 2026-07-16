#!/usr/bin/env python3
import sys, json, base64, os, urllib.request, urllib.error
env=open(os.path.expanduser("~/secret.xiaole.env")).read()
KEY=[l.split("=",1)[1].strip() for l in env.splitlines() if l.startswith("XIAOLE_KEY")][0]
URL="https://api.xiaoleai.team/v1/images/edits"
def main():
    img,prompt,out=sys.argv[1],sys.argv[2],sys.argv[3]
    size=sys.argv[4] if len(sys.argv)>4 else "1024x1536"
    quality=sys.argv[5] if len(sys.argv)>5 else "high"
    b="----xl"+base64.b16encode(os.urandom(8)).decode()
    p=[]
    def f(n,v):p.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".encode())
    f("model","gpt-image-2");f("prompt",prompt);f("size",size);f("quality",quality)
    p.append(f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"p.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode()+open(img,"rb").read()+b"\r\n")
    p.append(f"--{b}--\r\n".encode())
    req=urllib.request.Request(URL,data=b"".join(p),method="POST",headers={"Authorization":f"Bearer {KEY}","Content-Type":f"multipart/form-data; boundary={b}"})
    try:r=json.load(urllib.request.urlopen(req,timeout=300))
    except urllib.error.HTTPError as e:print("ERR",e.code,e.read().decode()[:300]);return
    it=(r.get("data") or [{}])[0]
    if it.get("b64_json"):open(out,"wb").write(base64.b64decode(it["b64_json"]));print("DONE",out)
    elif it.get("url"):urllib.request.urlretrieve(it["url"],out);print("DONE(url)",out)
    else:print("NO_IMAGE",json.dumps(r,ensure_ascii=False)[:300])
main()
