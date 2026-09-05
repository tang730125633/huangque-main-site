(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.HQVideoAgentSession=api;
})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  var TTL_MS=30*86400000;
  function isFresh(updatedAt,now,ttl){
    var age=Number(now==null?Date.now():now)-Number(updatedAt||0);
    return Number.isFinite(age)&&age>=0&&age<=Number(ttl||TTL_MS);
  }
  function createMediaStore(options){
    options=options||{};var dbPromise=null,indexedDb=options.indexedDB,idbKeyRange=options.IDBKeyRange,BlobCtor=options.Blob,now=options.now||Date.now;
    var dbName=options.dbName||'hq_video_agent_media_v1',storeName=options.storeName||'media',maxBytes=Number(options.maxBytes)||64*1024*1024,safeText=options.safeText||function(value,size){return String(value||'').slice(0,size);};
    function key(owner,canvasId){owner=safeText(owner,65).trim();canvasId=safeText(canvasId,64);return owner&&owner.length<=64&&/^cvm_[A-Za-z0-9_-]{4,60}$/.test(canvasId)?owner+'\u0000'+canvasId:'';}
    function open(){
      if(dbPromise)return dbPromise;
      dbPromise=new Promise(function(resolve,reject){if(!indexedDb){reject(new Error('media_storage_unavailable'));return;}var request=indexedDb.open(dbName,1);request.onupgradeneeded=function(){var db=request.result,store=db.objectStoreNames.contains(storeName)?request.transaction.objectStore(storeName):db.createObjectStore(storeName,{keyPath:'key'});if(!store.indexNames.contains('owner'))store.createIndex('owner','owner',{unique:false});};request.onsuccess=function(){resolve(request.result);};request.onerror=function(){dbPromise=null;reject(request.error||new Error('media_storage_open_failed'));};request.onblocked=function(){dbPromise=null;reject(new Error('media_storage_blocked'));};});return dbPromise;
    }
    function put(owner,canvasId,file){var storageKey=key(owner,canvasId);if(!storageKey||!file||!file.size)return Promise.reject(new Error('media_storage_invalid'));if(file.size>maxBytes)return Promise.reject(new Error('media_storage_too_large'));return open().then(function(db){return new Promise(function(resolve,reject){var tx=db.transaction(storeName,'readwrite');tx.objectStore(storeName).put({key:storageKey,owner:owner,canvas_id:canvasId,name:safeText(file.name,180)||'素材',mime:safeText(file.type,100),size:Number(file.size)||0,last_modified:Number(file.lastModified)||0,blob:file,updated_at:now()});tx.oncomplete=function(){resolve(true);};tx.onerror=function(){reject(tx.error||new Error('media_storage_write_failed'));};tx.onabort=function(){reject(tx.error||new Error('media_storage_write_aborted'));};});});}
    function remove(owner,canvasId){var storageKey=key(owner,canvasId);if(!storageKey)return Promise.resolve(false);return open().then(function(db){return new Promise(function(resolve){var tx=db.transaction(storeName,'readwrite');tx.objectStore(storeName).delete(storageKey);tx.oncomplete=function(){resolve(true);};tx.onerror=tx.onabort=function(){resolve(false);};});}).catch(function(){return false;});}
    function get(owner,canvasId){var storageKey=key(owner,canvasId);if(!storageKey)return Promise.reject(new Error('media_storage_invalid'));return open().then(function(db){return new Promise(function(resolve,reject){var request=db.transaction(storeName,'readonly').objectStore(storeName).get(storageKey);request.onsuccess=function(){var record=request.result;if(!record||record.owner!==owner||record.canvas_id!==canvasId||!(record.blob instanceof BlobCtor)){reject(new Error('media_storage_missing'));return;}if(!isFresh(record.updated_at,now(),TTL_MS)){remove(owner,canvasId);reject(new Error('media_storage_expired'));return;}resolve(record);};request.onerror=function(){reject(request.error||new Error('media_storage_read_failed'));};});});}
    function removeOwner(owner){owner=safeText(owner,65).trim();if(!owner)return Promise.resolve(false);return open().then(function(db){return new Promise(function(resolve){var tx=db.transaction(storeName,'readwrite'),index=tx.objectStore(storeName).index('owner'),request=index.openCursor(idbKeyRange.only(owner));request.onsuccess=function(){var cursor=request.result;if(cursor){cursor.delete();cursor.continue();}};tx.oncomplete=function(){resolve(true);};tx.onerror=tx.onabort=function(){resolve(false);};});}).catch(function(){return false;});}
    return {key:key,put:put,get:get,remove:remove,removeOwner:removeOwner};
  }
  function reconcile(fetchFn,actionId,headers,errorFactory){
    return fetchFn('/api/gen/video/agent/actions/'+encodeURIComponent(actionId)+'/reconcile',{method:'POST',headers:headers,body:'{}'}).then(function(response){return response.json().catch(function(){return {};}).then(function(data){if(!response.ok)throw errorFactory(response,data);return data;});});
  }
  return {TTL_MS:TTL_MS,isFresh:isFresh,createMediaStore:createMediaStore,reconcile:reconcile};
});
