/* Executed only inside the selected, logged-in Pixiv tab. No secrets leave it. */
(async function organizerPage(command) {
  const result = {nonce: command.nonce, ok: false, trace: []};
  // One bounded, read-only result field avoids scanning an ever-growing DevTools log.
  const sink = document.createElement('textarea');
  sink.setAttribute('aria-label','JPGPNG_ORGANIZER:'+command.nonce);
  sink.style.cssText='position:fixed;left:0;top:0;width:2px;height:2px;opacity:0.01;pointer-events:none;z-index:2147483647;';
  sink.readOnly=true; sink.tabIndex=-1;
  window.__jpgpngOrganizerOutput?.remove();
  window.__jpgpngOrganizerOutput=sink;
  document.body.prepend(sink);
  const fail = code => { const error = new Error(); error.safeCode = code; throw error; };
  const id = value => { if (!/^[1-9][0-9]{0,19}$/.test(String(value))) fail('invalid_identity'); return String(value); };
  const list = value => { if (!Array.isArray(value)) fail('invalid_list'); return value; };
  const names = value => list(value).map(v => {
    if (typeof v !== 'string' || !v.trim() || v !== v.trim() || v.length > 20 || /[\x00-\x1f]/.test(v)) fail('invalid_group');
    return v;
  }).sort();
  const eq = (a,b) => JSON.stringify(a) === JSON.stringify(b);
  const detail = body => {
    if (!body || !['0','1'].includes(String(body.restrict))) fail('invalid_follow_detail');
    const tags = list(body.tags);
    if (tags.some(t => typeof t.name !== 'string' || typeof t.selected !== 'boolean')) fail('invalid_group_selection');
    const selected = names(tags.filter(t => t.selected).map(t => t.name));
    if (selected.length > 5 || new Set(selected).size !== selected.length) fail('invalid_group_selection');
    return {artist_id:id(body.user_id), visibility:String(body.restrict), groups:selected};
  };
  try {
    if (location.origin !== 'https://www.pixiv.net') fail('wrong_origin');
    const element = document.getElementById('__NEXT_DATA__');
    if (!element) fail('page_state_missing');
    const next = JSON.parse(element.textContent);
    const state = JSON.parse(next.props.pageProps.serverSerializedPreloadedState);
    const self = state.userData.self;
    if (!self || !self.id) fail('not_logged_in');
    const account = id(self.id);
    if (command.account_id && account !== id(command.account_id)) fail('account_mismatch');
    if (self.premium !== true) fail('membership_required');
    let lastRequest = 0;
    const call = async (path, params, write=false) => {
      const delay = 500 - (Date.now() - lastRequest);
      if (delay>0) await new Promise(resolve=>setTimeout(resolve,delay));
      lastRequest = Date.now();
      const query = new URLSearchParams(params);
      const options = {method:write?'POST':'GET', credentials:'same-origin', cache:'no-store', redirect:'error', signal:AbortSignal.timeout(12000)};
      if (write) {
        if (typeof state.api?.token !== 'string' || !state.api.token) fail('request_token_missing');
        options.headers = {'Content-Type':'application/x-www-form-urlencoded; charset=utf-8', 'X-CSRF-TOKEN':state.api.token};
        options.body = query;
      }
      result.trace.push({method:options.method, path, status:null});
      const response = await fetch(path + (write?'':'?'+query), options);
      result.trace[result.trace.length-1].status = response.status;
      if (!response.ok) fail([401,403,429].includes(response.status)?'auth_or_rate_stop':'http_failure');
      if (!response.headers.get('content-type')?.includes('application/json')) fail('unexpected_response');
      const raw = await response.text();
      if (raw.length > 2000000) fail('response_too_large');
      const data = JSON.parse(raw);
      if (data.error !== false) fail('api_error');
      return data.body;
    };
    const catalog = async () => {
      const result = {};
      for (const side of ['show','hide']) {
        const page = await call('/ajax/user/'+account+'/following',{offset:0,limit:1,rest:side,tag:''});
        result[side] = names(page.followUserTags);
      }
      return result;
    };
    let body;
    switch(command.op) {
      case 'identity': body = {account_id:account, premium:true}; break;
      case 'catalog': body = await catalog(); break;
      case 'observe': {
        body = {detail:detail(await call('/ajax/following/user/details',{user_id:id(command.artist_id)})),catalog:await catalog()};
        break;
      }
      case 'following': {
        if (!['show','hide'].includes(command.visibility) || !Number.isInteger(command.offset) || command.offset < 0 || command.offset > 1000000) fail('invalid_cursor');
        const tag = command.tag === undefined ? '' : names([command.tag])[0];
        const data = await call('/ajax/user/'+account+'/following', {offset:command.offset,limit:30,rest:command.visibility,tag});
        const total = String(data.total);
        if (!/^(0|[1-9][0-9]{0,6})$/.test(total)) fail('invalid_total');
        body = {total:Number(total), catalog:names(data.followUserTags), users:list(data.users).map(u=>({artist_id:id(u.userId),name:String(u.userName||'').slice(0,200)}))};
        if (body.users.length > 30 || new Set(body.users.map(u=>u.artist_id)).size !== body.users.length) fail('invalid_follow_page');
        break;
      }
      case 'detail': body = detail(await call('/ajax/following/user/details',{user_id:id(command.artist_id)})); break;
      case 'works': {
        const data = await call('/ajax/user/'+id(command.artist_id)+'/profile/all',{});
        if (!data.illusts || !data.manga) fail('invalid_works');
        body = {ids:[...new Set([...Object.keys(data.illusts),...Object.keys(data.manga)])].map(id)};
        if (body.ids.length > 100000) fail('works_limit');
        break;
      }
      case 'artwork': {
        const data = await call('/ajax/illust/'+id(command.illust_id),{});
        body = {illust_id:id(data.id),artist_id:id(data.userId),date:String(data.createDate||''),
          kind:data.illustType,page_count:data.pageCount,url:data.urls?.regular || data.urls?.small || null};
        break;
      }
      case 'page': {
        if (!Number.isInteger(command.page) || command.page<0 || command.page>9999) fail('invalid_page');
        const data = list(await call('/ajax/illust/'+id(command.illust_id)+'/pages',{}));
        if (command.page>=data.length) fail('invalid_page');
        body = {url:data[command.page]?.urls?.regular || data[command.page]?.urls?.small || null};
        break;
      }
      case 'add': case 'remove': {
        if (window.__jpgpngOrganizerUsedNonces?.has(command.nonce)) fail('duplicate_submission_refused');
        if (!window.__jpgpngOrganizerUsedNonces) window.__jpgpngOrganizerUsedNonces = new Set();
        if (window.__jpgpngOrganizerUsedNonces.size>=1000) fail('page_session_command_limit');
        window.__jpgpngOrganizerUsedNonces.add(command.nonce);
        const artist = id(command.artist_id), expected = names(command.before);
        if (expected.length > 5 || names([command.tag]).length !== 1) fail('invalid_group');
        const current = detail(await call('/ajax/following/user/details',{user_id:artist}));
        if (current.artist_id!==artist || current.visibility!==command.visibility || !eq(current.groups,expected)) fail('state_conflict');
        if (command.op==='add' && (expected.includes(command.tag) || expected.length>=5)) fail('invalid_add');
        if (command.op==='remove' && !expected.includes(command.tag)) fail('invalid_remove');
        await new Promise(resolve=>setTimeout(resolve,500));
        await call('/ajax/following/user/tag_'+(command.op==='add'?'add':'delete'),{user_id:artist,tag:command.tag},true);
        body = {submitted:true}; break;
      }
      default: fail('unsupported_operation');
    }
    result.ok = true; result.account_id = account; result.body = body;
  } catch(error) { result.code = error.safeCode || (error.name==='TimeoutError'?'request_timeout':'browser_request_failed'); }
  sink.value=JSON.stringify(result);
  return result;
})
