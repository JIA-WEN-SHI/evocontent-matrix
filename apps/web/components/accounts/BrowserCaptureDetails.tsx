"use client";

import { useEffect, useRef, useState } from "react";
import { BookOpen, RefreshCw } from "lucide-react";
import { getBrowserCapture, getBrowserMedia, retryCaptureComments } from "@/lib/api";
import type { BrowserCapture } from "@/lib/browser-bridge";

function CapturedImage({accountId,itemId,image}:{accountId:string;itemId:string;image:BrowserCapture['images'][number]}) {
  const [url,setUrl]=useState('');
  const [error,setError]=useState('');
  useEffect(()=>{
    setUrl('');setError('');
    if (!image.id || image.status!=='saved') return;
    const controller=new AbortController(); let active=true; let objectUrl='';
    getBrowserMedia(accountId,itemId,image.id,controller.signal).then(blob=>{
      if (!active) return;
      objectUrl=URL.createObjectURL(blob);setUrl(objectUrl);
    }).catch(()=>{if(active)setError('本机图片无法读取');});
    return ()=>{active=false;controller.abort();if(objectUrl)URL.revokeObjectURL(objectUrl);};
  },[accountId,itemId,image.id,image.status]);
  return <figure className="min-w-0">
    <div className="flex aspect-[3/4] items-center justify-center overflow-hidden rounded border border-cyan-300/20 bg-black/20">
      {url&&!error?<img src={url} alt={`笔记配图 ${image.index+1}`} className="h-full w-full object-contain" onError={()=>setError('图片文件无法显示')}/>:<p className="break-words p-2 text-xs text-rose-200">{error||image.message||(image.status==='saved'?'加载中':'图片尚未保存')}</p>}
    </div>
    <figcaption className="mt-1 text-xs text-cyan-200/70">配图 {image.index+1}</figcaption>
    {image.last_attempt?<p className="mt-1 break-words text-xs text-amber-200">{image.last_attempt.message}，保留上次保存的图片。</p>:null}
  </figure>;
}

const statusLabel=(status:string)=>({complete:'已获取',partial:'部分获取',unsupported:'未识别',empty:'暂无内容',failed:'本轮读取失败，并非没有内容'}[status]||'待核对');

export function BrowserCaptureDetails({accountId,itemId,expanded=false}:{accountId:string;itemId:string;expanded?:boolean}) {
  const [open,setOpen]=useState(expanded);
  const [tab,setTab]=useState<'summary'|'body'|'comments'|'images'>('body');
  const [data,setData]=useState<BrowserCapture|null>(null);
  const [error,setError]=useState('');
  const [revision,setRevision]=useState(0);
  const [retryBusy,setRetryBusy]=useState(false);
  const [retryNotice,setRetryNotice]=useState('');
  const [retryError,setRetryError]=useState('');
  const retryLock=useRef(false);
  const retryScope=useRef(`${accountId}:${itemId}`);
  retryScope.current=`${accountId}:${itemId}`;
  useEffect(()=>{
    retryScope.current=`${accountId}:${itemId}`;
    setRetryNotice('');setRetryError('');setRetryBusy(retryLock.current);
    return ()=>{retryScope.current='';};
  },[accountId,itemId]);

  async function retryComments() {
    if(retryLock.current)return;
    const requestedScope=`${accountId}:${itemId}`;
    retryLock.current=true;setRetryBusy(true);setRetryNotice('');setRetryError('');
    try {
      const result=await retryCaptureComments(accountId,itemId);
      if(retryScope.current!==requestedScope)return;
      const warnings=(result.warnings||[]).join('；');
      const status=result.comments_status;
      setRetryNotice([status==='complete'?'本轮评论读取完成':status==='empty'?'本轮未读取到评论':status==='failed'?'本轮评论读取失败，保留之前的评论':'本轮仅获取部分评论，保留已获取内容',warnings].filter(Boolean).join('；'));
      setRevision(value=>value+1);
    } catch(e) {
      if(retryScope.current===requestedScope)setRetryError(e instanceof Error?e.message:'评论重试失败，之前保存的资料不受影响');
    } finally {
      retryLock.current=false;
      if(retryScope.current)setRetryBusy(false);
    }
  }
  useEffect(()=>{
    if(!open)return;
    let active=true;setData(null);setError('');
    getBrowserCapture(accountId,itemId).then(value=>{if(active)setData(value);}).catch(e=>{if(active)setError(e instanceof Error?e.message:'无法读取资料');});
    return ()=>{active=false;};
  },[accountId,itemId,open,revision]);
  return <div className="mt-2 min-w-0 border-t border-cyan-300/20 pt-2 text-cyan-100">
    <div className="flex gap-2">
      {!expanded?<button type="button" aria-expanded={open} onClick={()=>setOpen(value=>!value)} className="inline-flex items-center gap-1 text-xs"><BookOpen size={14}/>{open?'收起资料':'查看采集资料'}</button>:<span className="inline-flex items-center gap-1 text-sm"><BookOpen size={14}/>采集资料</span>}
      {open?<button type="button" title="刷新资料" aria-label="刷新资料" onClick={()=>setRevision(value=>value+1)}><RefreshCw size={14}/></button>:null}
    </div>
    {open?<>
      {retryError?<p role="alert" className="mt-2 break-words text-xs text-rose-200">{retryError}</p>:null}
      {retryNotice?<p role="status" className="mt-2 break-words text-xs text-amber-200">{retryNotice}</p>:null}
      {error?<p role="alert" className="mt-2 break-words text-xs text-rose-200">{error}</p>:!data?<p role="status" className="mt-2 text-xs">读取资料中</p>:<>
        <div role="group" aria-label="采集资料" className="mt-2 flex flex-wrap gap-2 border-b border-cyan-300/20">
          {([['summary','摘要'],['body','原文'],['comments',`评论 ${data.comments.length}`],['images',`图片 ${data.images.length}`]] as const).map(([value,label])=><button key={value} type="button" aria-pressed={tab===value} onClick={()=>setTab(value)} className={`border-b-2 px-1 py-2 text-xs ${tab===value?'border-emerald-300 text-emerald-100':'border-transparent'}`}>{label}</button>)}
        </div>
        <div role="region" aria-label={{summary:'摘要',body:'原文',comments:'评论',images:'图片'}[tab]} className="mt-2 max-h-96 overflow-y-auto text-sm leading-6 [overflow-wrap:anywhere]">
          {tab==='summary'?<p className="whitespace-pre-wrap">{data.summary||'模型摘要暂未生成，原文仍已保存。'}</p>:null}
          {tab==='body'?<><p className="mb-2 text-cyan-200/70">{data.author_name} · {statusLabel(data.body_status)}</p><p className="whitespace-pre-wrap">{data.body_text}</p></>:null}
          {tab==='comments'?<><div className="mb-2 flex flex-wrap items-center justify-between gap-2"><p className="text-cyan-200/70">{statusLabel(data.comments_status)} · {data.comments.length} 条（含回复）</p><button type="button" disabled={retryBusy} onClick={()=>void retryComments()} className="inline-flex min-h-8 items-center gap-1 rounded border border-cyan-300/30 px-2 text-xs disabled:opacity-50"><RefreshCw size={14} className={retryBusy?'animate-spin':''}/>{retryBusy?'重试中...':'重试评论'}</button></div><ul className="divide-y divide-cyan-300/15">{data.comments.map((comment,index)=><li key={`${comment.key}-${index}`} className={`py-2 ${comment.parent_key?'border-l border-emerald-300/30 pl-2':''}`}><p className="text-emerald-200/80">{comment.author_name}{comment.parent_key?' · 回复':''} {comment.observed_time}</p><p className="whitespace-pre-wrap">{comment.text}</p>{comment.truncated?<p className="text-amber-200">该条内容达到长度上限</p>:null}</li>)}</ul></>:null}
          {tab==='images'?<><p className="mb-2 text-cyan-200/70">已保存 {data.images.filter(image=>image.status==='saved').length} / 已发现 {data.images.length} 张</p><div className="grid grid-cols-2 gap-2">{data.images.map(image=><CapturedImage key={`${image.index}-${image.id||image.status}`} accountId={accountId} itemId={itemId} image={image}/>)}</div></>:null}
        </div>
        {data.truncation_reasons?.length?<p className="mt-2 break-words text-xs text-amber-200">{data.truncation_reasons.join('；')}</p>:null}
        <a href={data.source_url} target="_blank" rel="noreferrer" className="mt-2 inline-block text-xs underline">核对公开来源</a>
      </>}
    </>:null}
  </div>;
}
