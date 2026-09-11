(function(root){
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function label(key,value){
    const labels={quality:{low:'经济',medium:'标准',high:'高品质',auto:'自动',standard:'标准',hd:'高清'},background:{opaque:'不透明',transparent:'透明',auto:'自动'},output_format:{png:'PNG',jpeg:'JPEG',webp:'WebP'},resolution:{'480p':'480p · 流畅','720p':'720p · 高清','1080p':'1080p · 超清'}};
    const ratios={'1024x1024':'1:1','1536x1024':'3:2','1024x1536':'2:3','1792x1024':'7:4','1024x1792':'4:7','2048x2048':'1:1','2048x1152':'16:9','1152x2048':'9:16','3840x2160':'16:9','2160x3840':'9:16','2048x1536':'4:3','1536x2048':'3:4','1024x1280':'4:5','1280x1024':'5:4','1024x768':'4:3','768x1024':'3:4','1280x720':'16:9','720x1280':'9:16'};
    return key==='size'&&ratios[value]?value+' · '+ratios[value]:key==='duration'?value+' 秒':labels[key]?.[value]||value;
  }
  function choose(spec,current,key,value){
    const wanted={...(current?.values||{}),[key]:value};
    const candidates=spec.combinations.filter(c=>String(c.values[key])===String(value));
    return candidates.sort((a,b)=>Object.keys(wanted).filter(k=>String(b.values[k])===String(wanted[k])).length-Object.keys(wanted).filter(k=>String(a.values[k])===String(wanted[k])).length)[0];
  }
  function mount(host,spec,onChange,initial,options={}){
    let current=spec.combinations.find(c=>c.id===(initial||spec.default))||spec.combinations[0];
    let billingEnabled=options.billingEnabled!==false;
    const priceText=()=>billingEnabled?'本次 '+Number(current?.points||0)+' 点 · 1 个产物':'内测期间免费 · 1 个产物';
    function render(note=''){
      if(!current){host.textContent='没有可选组合';return}
      host.innerHTML='<div class="cp-fields">'+spec.fields.filter(f=>f.visible!==false).map(f=>{
        const values=[...new Set(spec.combinations.map(c=>String(c.values[f.key])))];
        return '<label>'+esc(f.label)+'<select data-cp-field="'+esc(f.key)+'">'+values.map(v=>'<option value="'+esc(v)+'" '+(String(current.values[f.key])===v?'selected':'')+'>'+esc(label(f.key,v))+'</option>').join('')+'</select></label>';
      }).join('')+'</div><p class="cp-price">'+priceText()+'</p><p class="cp-note" role="status">'+esc(note||'选项会按合法组合联动')+'</p>';
      if(onChange)onChange(current);
    }
    host.onchange=e=>{if(!e.target.dataset.cpField)return;current=choose(spec,current,e.target.dataset.cpField,e.target.value);render(billingEnabled?'已按合法组合更新相关参数，请确认点数。':'已按合法组合更新相关参数。')};
    render();return {value:()=>current,setBillingEnabled:enabled=>{billingEnabled=!!enabled;const price=host.querySelector('.cp-price');if(price)price.textContent=priceText();if(!billingEnabled){const note=host.querySelector('.cp-note');if(note)note.textContent='选项会按合法组合联动'}}};
  }
  root.ChannelParameterControls={esc,choose,mount,label};
})(typeof window==='undefined'?globalThis:window);
