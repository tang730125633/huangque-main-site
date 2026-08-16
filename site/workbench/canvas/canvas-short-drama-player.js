(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaPlayer=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function text(value){ return String(value==null?'':value); }
  function classifyMediaError(error){
    var status=Number(error&&error.status)||0;
    var code=text(error&&error.code).toLowerCase();
    if(status===401||status===403||code.indexOf('forbidden')>=0){
      return {code:'forbidden',message:'无权读取此媒体资源'};
    }
    if(status===404||code.indexOf('not_found')>=0){
      return {code:'not_ready',message:'媒体尚未就绪或已被清理'};
    }
    if(status===410||code.indexOf('expired')>=0){
      return {code:'expired',message:'播放地址已过期，正在刷新'};
    }
    if(code.indexOf('format')>=0){
      return {code:'unsupported',message:'浏览器不支持此媒体格式'};
    }
    return {code:'network',message:'媒体加载中断，请检查网络后重试'};
  }
  function createPlayer(options){
    options=options||{};
    var api=options.api,host=options.host;
    var onError=typeof options.onError==='function'?options.onError:function(){};
    var onReady=typeof options.onReady==='function'?options.onReady:function(){};
    var destroyed=false,generation=0,objectUrls=[],video=null,version=null;
    var refreshed=false,lastPosition=0,subtitlesVisible=true;
    var playbackRate=1,volume=1,muted=false;
    function revoke(){
      if(typeof URL!=='undefined'&&URL.revokeObjectURL){
        objectUrls.forEach(function(value){ URL.revokeObjectURL(value); });
      }
      objectUrls=[];
    }
    function detach(){
      if(!video) return;
      lastPosition=Number(video.currentTime)||0;
      playbackRate=Number(video.playbackRate)||1;
      volume=Number(video.volume);
      muted=video.muted===true;
      video.pause&&video.pause();
      video.removeEventListener&&video.removeEventListener('error',mediaError);
      video.removeEventListener&&video.removeEventListener('loadedmetadata',ready);
      video.removeAttribute&&video.removeAttribute('src');
      if(video.querySelectorAll){
        Array.prototype.forEach.call(
          video.querySelectorAll('track'),function(track){
            if(track.parentNode) track.parentNode.removeChild(track);
          }
        );
      }
      video.load&&video.load();
      video=null;revoke();
    }
    function ready(){
      if(!video||destroyed) return;
      if(lastPosition>0&&Number(video.duration)>lastPosition){
        try{ video.currentTime=lastPosition; }catch(ignore){}
      }
      video.playbackRate=playbackRate;
      if(isFinite(volume)) video.volume=volume;
      video.muted=muted;
      Array.prototype.forEach.call(video.textTracks||[],function(track){
        track.mode=subtitlesVisible?'showing':'hidden';
      });
      onReady(version);
    }
    function mediaError(){
      if(destroyed) return;
      var classified=classifyMediaError({
        code:video&&video.error&&video.error.code===4?'format':'network'
      });
      onError(classified,version);
    }
    function attach(target,nextVersion){
      detach();video=target;version=nextVersion||null;refreshed=false;
      var source=text(version&&version.media_url||version&&version.url);
      var subtitle=text(version&&version.subtitle_url);
      if(!video||!version||!source) return Promise.resolve(null);
      var current=++generation;
      video.addEventListener&&video.addEventListener('error',mediaError);
      video.addEventListener&&video.addEventListener('loadedmetadata',ready);
      function protectedUrl(path){
        if(path.indexOf('/api/gen/file/')!==0||
            !api||typeof api.asset!=='function'){
          return Promise.resolve(path);
        }
        return api.asset(path).then(function(blob){
          if(typeof URL!=='undefined'&&URL.createObjectURL){
            var url=URL.createObjectURL(blob);objectUrls.push(url);return url;
          }
          return path;
        });
      }
      return Promise.all([
        protectedUrl(source),
        subtitle?protectedUrl(subtitle):Promise.resolve('')
      ]).then(function(urls){
        if(destroyed||current!==generation||!video) return null;
        video.src=urls[0];
        if(urls[1]&&video.ownerDocument&&video.ownerDocument.createElement){
          var track=video.ownerDocument.createElement('track');
          track.kind='subtitles';track.label='中文字幕';track.srclang='zh-CN';
          track.src=urls[1];track.default=true;
          video.appendChild(track);
        }
        video.load&&video.load();
        return version;
      }).catch(function(error){
        if(destroyed||current!==generation) return null;
        onError(classifyMediaError(error),version);
        return null;
      });
    }
    function retry(nextVersion){
      if(refreshed) return Promise.resolve(false);
      refreshed=true;
      var target=video;
      lastPosition=target?Number(target.currentTime)||lastPosition:lastPosition;
      return attach(target,nextVersion||version).then(function(){ return true; });
    }
    function toggleSubtitles(force){
      subtitlesVisible=typeof force==='boolean'?force:!subtitlesVisible;
      if(video){
        Array.prototype.forEach.call(video.textTracks||[],function(track){
          track.mode=subtitlesVisible?'showing':'hidden';
        });
      }
      return subtitlesVisible;
    }
    return {
      attach:attach,
      retry:retry,
      toggleSubtitles:toggleSubtitles,
      subtitlesVisible:function(){ return subtitlesVisible; },
      position:function(){ return lastPosition; },
      destroy:function(){
        destroyed=true;generation+=1;detach();host=null;version=null;
      }
    };
  }
  return {classifyMediaError:classifyMediaError,createPlayer:createPlayer};
});
