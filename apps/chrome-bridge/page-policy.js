(function (root) {
  const origin = 'https://www.xiaohongshu.com';
  function isPublicPage(value) {
    try {
      const url = new URL(value);
      return url.origin === origin && !url.username && !url.password &&
        (/^\/(?:explore|search_result)?\/?$/.test(url.pathname) || /^\/(?:explore|search_result|discovery\/item)\/[a-f0-9]{24}\/?$/i.test(url.pathname));
    } catch { return false; }
  }
  function noteUrl(value) {
    try {
      const url = new URL(value, origin);
      const match = url.pathname.match(/^\/(?:explore|search_result|discovery\/item)\/([a-f0-9]{24})\/?$/i);
      return isPublicPage(url.href) && match ? `${origin}/explore/${match[1].toLowerCase()}` : null;
    } catch { return null; }
  }
  function validate(operation, args = {}, observation) {
    const fields = { observe: [], search: ['query', 'version'], open_note: ['target_id', 'version'],
      scroll_note: ['direction', 'version'], close_note: ['version'], read_note: ['version'], expand_body: ['target_id','version'], scroll_comments: ['version'], expand_reply: ['target_id','version'], next_image: ['target_id','version'] };
    if (!fields[operation] || !args || Array.isArray(args) || Object.keys(args).some(key => !fields[operation].includes(key))) throw new Error('此操作不允许');
    if (operation !== 'observe' && (!args.version || (observation && args.version !== observation.version))) throw new Error('页面已变化，请重新观察');
    if (operation === 'search' && (typeof args.query !== 'string' || !args.query.trim() || args.query.length > 160)) throw new Error('搜索词无效');
    if (operation === 'open_note' && (typeof args.target_id !== 'string' || !observation?.targets?.[args.target_id])) throw new Error('笔记不在本轮可见结果中');
    if (operation === 'scroll_note' && !['up', 'down'].includes(args.direction)) throw new Error('滚动方向无效');
    if (['expand_body','expand_reply','next_image'].includes(operation) && (!observation?.detailTargets?.[args.target_id] || observation.detailTargets[args.target_id].operation !== operation)) throw new Error('内容操作不在本轮公开目标中');
    return args;
  }
  const policy = Object.freeze({ isPublicPage, noteUrl, validate });
  if (typeof module !== 'undefined' && module.exports) module.exports = policy;
  else root.EvoPagePolicy = policy;
})(globalThis);
