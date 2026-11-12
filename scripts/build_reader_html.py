"""生成离线阅读页（设计文档第 20 节 A 批）。

从 ``--apply-annotations`` 的真实产物里抽取正文，不重新渲染、不调用模型：

- 中文提示开关：隐藏/显示正文括注，词仍可点；
- 点击/键盘打开详解：内容取自同一份章末词条，不另调模型翻译；
- 位置恢复：按书隔离保存在 localStorage，版本不一致时安全回退；
- 章末词条折叠，避免打断连续阅读；
- 临时名单标记与 CSV 导出（导出后可作 ``--word-list`` 输入）。

无脚本时正文、括注、目录、回链仍可读；存储或脚本不可用只丢「记忆位置」。
"""

from __future__ import annotations

import argparse
import base64
import json
import posixpath
import re
import sys
import zipfile
from copy import deepcopy
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/build_reader_html.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from lxml import etree

from .binding import apply_binding, binding_path_for, load_binding
from .chapters import detect_chapters
from .console import configure_stdio
from .epub_common import open_epub

CSS = """
:root{--bg:#f7f5f0;--paper:#fffdf8;--ink:#24211d;--muted:#797268;--line:#e4ded2;
--accent:#9a6b3f;--accent-soft:#f3e9dc;--note:#8d7259;--tag:#b4824c;
--shadow:0 2px 18px rgba(80,64,44,.07)}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:"Iowan Old Style","Palatino Linotype",Georgia,"Songti SC",serif;
line-height:1.95;-webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto;padding:34px 22px 80px}
header.top{border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:20px}
h1{font-size:1.45rem;margin:0 0 8px;font-weight:600;letter-spacing:.02em}
.sub{color:var(--muted);font-size:.84rem;line-height:1.8;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.sub b{color:var(--ink);font-weight:600}
.bar{display:none;gap:9px;align-items:center;flex-wrap:wrap;margin:0 0 18px;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.tab{border:1px solid var(--line);background:var(--paper);color:var(--muted);
border-radius:999px;padding:7px 18px;font-size:.86rem;cursor:pointer;transition:.16s}
.tab:hover{border-color:var(--accent);color:var(--accent)}
.tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
.switch{margin-left:auto;display:inline-flex;align-items:center;gap:7px;font-size:.82rem;
color:var(--muted);cursor:pointer;user-select:none;border:1px solid var(--line);
background:var(--paper);border-radius:999px;padding:6px 14px}
.switch input{accent-color:var(--accent);cursor:pointer}
.toc{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 22px;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.toc a{font-size:.78rem;color:var(--muted);text-decoration:none;border:1px solid var(--line);
background:var(--paper);border-radius:6px;padding:4px 10px}
.toc a:hover,.toc a:focus{color:var(--accent);border-color:var(--accent)}
/* 默认（无脚本）所有书顺序可读；脚本可用时再加 .js 切到单书模式 */
.book{display:block;margin-bottom:56px}
html.js .book{display:none}
html.js .book.on{display:block}
html.js .bar{display:flex}
html.js .markbar{display:flex}
.bookmeta{background:var(--accent-soft);border:1px solid #eadcc7;border-radius:10px;
padding:13px 17px;margin-bottom:26px;font-size:.82rem;line-height:1.85;color:#5c4b39;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.bookmeta b{color:#4b3a28}
.chapter{margin:0 0 38px}
.chapter>h2{font-size:1rem;letter-spacing:.1em;color:var(--accent);
border-bottom:1px solid var(--line);padding-bottom:8px;margin:0 0 18px;scroll-margin-top:14px;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.chapter>h2 small{display:block;font-size:.72rem;letter-spacing:0;color:var(--muted);
font-weight:400;margin-top:4px}
.bs,.bs6{display:block;margin:0 0 1.02em;text-align:justify;font-size:1.02rem}
.bs1,.calibre3,.calibre1:empty,p:empty{display:none}
.bs6{display:none}
.chapter p{display:block;margin:0 0 1.02em;text-align:justify;font-size:1.02rem}
img{max-width:100%;height:auto;display:block;margin:1.3em auto;border-radius:4px;
box-shadow:var(--shadow)}
.vocab{border-bottom:1.5px dotted #b6a189;text-decoration:none;color:inherit;
cursor:pointer;border-radius:2px}
.vocab:hover,.vocab:focus{background:#f6ead9;outline:none}
.vocab:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.vocab-note{color:var(--note);font-size:.76em;white-space:nowrap;margin-left:1px;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.vocab-link{color:var(--accent);text-decoration:none;font-weight:600}
body.hide-notes .vocab-note{display:none}
details.notes-wrap{margin-top:2.6em;border-top:2px solid var(--line);padding-top:.9em}
details.notes-wrap>summary{cursor:pointer;color:var(--accent);font-size:.9rem;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
list-style:none;padding:6px 0}
details.notes-wrap>summary::-webkit-details-marker{display:none}
details.notes-wrap>summary::before{content:"▸ ";color:var(--accent)}
details.notes-wrap[open]>summary::before{content:"▾ "}
.vocab-chapter-notes{font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.vocab-notes-title{display:none}
.vocab-list{padding:0;list-style:none;margin:.6em 0 0}
.vocab-item{margin:0 0 10px;padding:12px 16px;background:var(--paper);
border:1px solid var(--line);border-radius:9px;box-shadow:var(--shadow);
font-size:.87rem;line-height:1.72;scroll-margin-top:16px}
.vocab-item:target{border-color:var(--accent);background:#fff8ee}
.vocab-head{margin:0 0 5px;font-weight:600;font-size:.96rem}
.vocab-num{color:var(--accent);margin-right:2px}
.vocab-grade,.vocab-category{color:var(--tag);font-size:.82em;font-weight:400}
.vocab-item p{margin:2px 0;text-align:left;font-size:.88rem;line-height:1.68}
.vocab-sense{color:var(--ink);font-weight:600}
.vocab-pos,.vocab-gloss,.vocab-example,.vocab-extra{color:#5f574d;font-size:.92em}
.vocab-member{margin-top:4px;color:#4a4239}
.vocab-member-word{font-weight:600}
.vocab-member-label{color:var(--accent);font-size:.88em}
.vocab-member-gloss{color:#6b6258;font-size:.9em}
.vocab-back{display:none}
/* 段后就近词条：EPUB 与阅读页同一份内容，章末索引隐藏不重复展示 */
.vocab-chapter-notes.vocab-archive{display:none}
.vocab-para-notes{margin:.55em 0 1.05em;padding:.4em .7em .5em;border-left:3px solid var(--line);
background:var(--accent-soft);border-radius:7px;font-size:.9em}
.vocab-para-title{margin:0 0 .3em;font-size:.8rem;font-weight:600;color:var(--accent);
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.vocab-para-item{margin:0 0 6px;padding:8px 12px;background:var(--paper);
border:1px solid var(--line);border-radius:7px;font-size:.83rem;line-height:1.6}
.vocab-para-item:target{border-color:var(--accent);background:#fff8ee}
.vocab-para-item:last-child{margin-bottom:0}
.vocab-para-item p{margin:1px 0;text-align:left;font-size:.86rem;line-height:1.55}
.vocab-repeat-tag{color:var(--muted);font-weight:400}
/* 详解弹层 */
dialog#detail{border:none;border-radius:12px;padding:0;max-width:560px;width:calc(100% - 32px);
background:var(--paper);color:var(--ink);box-shadow:0 18px 60px rgba(60,45,30,.28)}
dialog#detail::backdrop{background:rgba(40,32,22,.38)}
#detail-body{padding:18px 20px 6px;max-height:66vh;overflow:auto;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
#detail-body .vocab-item{border:none;box-shadow:none;margin:0;padding:0;background:none}
#detail-body .vocab-head{font-size:1.05rem}
#detail-body .key-hint{font-size:.76rem;color:var(--muted);margin:.4em 0 0}
.fb{padding:12px 20px 4px;border-top:1px solid var(--line);background:#fffdf7;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.fb-title{font-size:.78rem;color:var(--muted);margin-bottom:7px}
.fb-btns{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:7px}
.fb-btns button{border:1px solid var(--line);background:var(--paper);color:var(--muted);
border-radius:7px;padding:5px 10px;font-size:.76rem;cursor:pointer}
.fb-btns button.on{border-color:#b06a2c;color:#a75f22;background:#fdf1e3}
.fb textarea{width:100%;border:1px solid var(--line);border-radius:7px;padding:7px 9px;
font-size:.8rem;font-family:inherit;resize:vertical;background:var(--paper);color:var(--ink)}
.fb-row{display:flex;align-items:center;gap:9px;margin:7px 0 2px}
.fb-row button{border:1px solid var(--accent);background:var(--accent);color:#fff;
border-radius:7px;padding:5px 12px;font-size:.77rem;cursor:pointer}
.fb-state{font-size:.75rem;color:var(--accent)}
#detail-actions{display:flex;gap:8px;flex-wrap:wrap;padding:10px 20px 16px;
border-top:1px solid var(--line);background:#fdfaf4;border-radius:0 0 12px 12px}
#detail-actions button{font-size:.8rem;border:1px solid var(--line);background:var(--paper);
color:var(--muted);border-radius:8px;padding:7px 12px;cursor:pointer;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
#detail-actions button:hover{border-color:var(--accent);color:var(--accent)}
#detail-actions button.on{background:var(--accent);border-color:var(--accent);color:#fff}
#detail-actions .spacer{flex:1}
.markbar{display:none;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 20px;
font-size:.78rem;color:var(--muted);
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.markbar button{border:1px solid var(--line);background:var(--paper);color:var(--accent);
border-radius:7px;padding:5px 11px;font-size:.78rem;cursor:pointer}
.markbar button:disabled{color:#bbb;border-color:#eee;cursor:default}
#status{font-size:.78rem;color:var(--accent)}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
color:var(--muted);font-size:.75rem;line-height:1.9;
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
@media(max-width:640px){.wrap{padding:22px 14px 60px}.switch{margin-left:0}}
@media print{.bar,.toc,.markbar,dialog{display:none}.book{display:block!important}}
"""

SCRIPT = """
(function(){
  var body=document.body;
  var KEY='vocab-reader:';
  var MARKS='vocab-reader:marks';
  var current=document.querySelector('.book.on');
  var marked={};
  function safeGet(k){try{return localStorage.getItem(k)}catch(e){return null}}
  function safeSet(k,v){try{localStorage.setItem(k,v)}catch(e){}}
  function prefs(id){try{return JSON.parse(safeGet(KEY+id)||'{}')}catch(e){return {}}}
  function setPrefs(id,patch){
    var p=prefs(id); for(var k in patch){p[k]=patch[k]}
    safeSet(KEY+id,JSON.stringify(p));
  }
  function loadMarks(){try{marked=JSON.parse(safeGet(MARKS)||'{}')}catch(e){marked={}}}
  function saveMarks(){safeSet(MARKS,JSON.stringify(marked))}
  loadMarks();

  /* 中文提示开关 */
  var toggle=document.getElementById('notes-toggle');
  function applyNotes(show,persist){
    body.classList.toggle('hide-notes',!show);
    if(toggle) toggle.checked=show;
    if(persist!==false&&current) setPrefs(current.dataset.book,{showNotes:!!show});
  }
  if(toggle){
    toggle.addEventListener('change',function(){applyNotes(toggle.checked)});
  }

  /* 按书保存/恢复阅读位置 */
  var timer=null;
  /* 优先章节/段落锚点（稳定），没有再退到词级锚点；不记录纯装饰性节点 */
  function topAnchor(){
    if(!current) return null;
    var groups=[current.querySelectorAll('.chapter h2[id],.chapter div[id],.chapter p[id]'),
                current.querySelectorAll('.chapter span.vocab[id]')];
    for(var g=0;g<groups.length;g++){
      var best=null,bestTop=-1e9;
      for(var i=0;i<groups[g].length;i++){
        var r=groups[g][i].getBoundingClientRect();
        if(r.top<=140 && r.top>bestTop){bestTop=r.top;best=groups[g][i]}
      }
      if(best) return {id:best.id,offset:Math.round(Math.max(0,-bestTop))};
    }
    return null;
  }
  function remember(){
    if(!current) return;
    var a=topAnchor(); if(!a) return;
    setPrefs(current.dataset.book,{anchorId:a.id,offset:a.offset,
      version:current.dataset.version});
  }
  window.addEventListener('scroll',function(){
    if(timer) return;
    timer=setTimeout(function(){timer=null;remember()},600);
  },{passive:true});

  function restore(book){
    if(!book) return;
    var p=prefs(book.dataset.book);
    var show=p.showNotes===undefined?true:!!p.showNotes;
    /* ?notes=0 只对本次打开生效，便于分享「先读原文」的链接 */
    var forced=new URLSearchParams(location.search).get('notes');
    if(forced==='0'||forced==='1') show=forced==='1';
    applyNotes(show,forced?false:undefined);
    if(p.version&&p.version===book.dataset.version&&p.anchorId){
      var el=document.getElementById(p.anchorId);
      if(el){
        /* 锚点失效或版本变化时退回章节开头，不跳向错误位置 */
        window.scrollTo({top:Math.max(0,el.getBoundingClientRect().top+
          window.scrollY-(p.offset||0)-8),behavior:'instant'});
        return;
      }
    }
    window.scrollTo({top:0,behavior:'instant'});
  }
  restore(current);

  /* 切换书 */
  var tabs=document.querySelectorAll('.tab');
  Array.prototype.forEach.call(tabs,function(t){
    t.addEventListener('click',function(){
      remember();
      Array.prototype.forEach.call(tabs,function(x){x.classList.remove('on')});
      t.classList.add('on');
      Array.prototype.forEach.call(document.querySelectorAll('.book'),function(b){
        b.classList.toggle('on',b.id==='book-'+t.dataset.book);
      });
      current=document.getElementById('book-'+t.dataset.book);
      restore(current);
    });
  });

  /* 详解弹层 */
  var dlg=document.getElementById('detail');
  var dbody=document.getElementById('detail-body');
  var lastFocus=null;
  var active=null;
  function closeDetail(){
    if(!dlg) return;
    if(dlg.close) dlg.close(); else dlg.removeAttribute('open');
    if(lastFocus&&lastFocus.focus) lastFocus.focus();
  }
  function openDetail(el){
    if(!dlg||!dbody) return;
    var book=el.closest('.book'); if(!book) return;
    var prefix=book.dataset.prefix||'';
    var target=document.getElementById(prefix+'vnote-'+el.dataset.chapter+'-'+el.dataset.note);
    if(!target) return;
    active=el; lastFocus=el;
    dbody.innerHTML='';
    var clone=target.cloneNode(true);
    clone.removeAttribute('id');
    var back=clone.querySelector('.vocab-back'); if(back) back.remove();
    dbody.appendChild(clone);
    var key=el.dataset.key||el.textContent.trim();
    if(key&&key!==el.textContent.trim()){
      var hint=document.createElement('p');
      hint.className='key-hint';
      hint.textContent='名单键：'+key+'（按已核验原形导出）';
      dbody.appendChild(hint);
    }
    syncMarkButtons(key);
    resetFeedback(el);
    if(dlg.showModal) dlg.showModal(); else dlg.setAttribute('open','');
    var first=document.getElementById('detail-close');
    if(first) first.focus();
  }

  /* 临时名单标记 */
  var statusEl=document.getElementById('status');
  var masterBtn=document.getElementById('mark-mastered');
  var focusBtn=document.getElementById('mark-focus');
  var clearBtn=document.getElementById('mark-clear');
  var exportBtn=document.getElementById('export-marks');
  /* §20.7 本地纠错：反馈只记待复核，不直接改正文；导出 JSON 交给重审 */
  var FEEDBACK='vocab-reader:feedback';
  var feedback={};
  try{feedback=JSON.parse(safeGet(FEEDBACK)||'{}')}catch(e){feedback={}}
  var fbKind='';
  var fbNote=document.getElementById('fb-note');
  var fbState=document.getElementById('fb-state');
  var fbBtns=document.querySelectorAll('.fb-btns button');
  function feedbackCount(){
    var n=0; for(var k in feedback){if(feedback[k]) n++} return n;
  }
  function resetFeedback(el){
    fbKind='';
    if(fbNote) fbNote.value='';
    Array.prototype.forEach.call(fbBtns,function(b){b.classList.remove('on')});
    if(fbState) fbState.textContent='';
    var ident=el?(el.dataset.vocabId||''):'';
    var existing=ident?feedback[ident]:null;
    if(existing){
      fbKind=existing.kind||'';
      if(fbNote) fbNote.value=existing.note||'';
      Array.prototype.forEach.call(fbBtns,function(b){
        b.classList.toggle('on',b.dataset.kind===fbKind);
      });
      if(fbState) fbState.textContent='已记录（可修改后重新提交）';
    }
  }
  Array.prototype.forEach.call(fbBtns,function(b){
    b.addEventListener('click',function(){
      fbKind=b.dataset.kind;
      Array.prototype.forEach.call(fbBtns,function(x){
        x.classList.toggle('on',x===b);
      });
    });
  });
  var fbSubmit=document.getElementById('fb-submit');
  if(fbSubmit) fbSubmit.addEventListener('click',function(){
    if(!active) return;
    var ident=active.dataset.vocabId||'';
    if(!ident) return;
    if(!fbKind){ if(fbState) fbState.textContent='先选一个问题类型'; return; }
    var book=active.closest('.book');
    feedback[ident]={
      annotation_id:ident,
      surface:active.textContent.trim(),
      lemma:active.dataset.key||'',
      chapter:active.dataset.chapterLabel||'',
      gloss:active.dataset.gloss||'',
      sentence:active.dataset.sentence||'',
      book:book?(book.dataset.bookTitle||''):'',
      version:book?(book.dataset.version||''):'',
      kind:fbKind,
      note:fbNote?(fbNote.value.trim()):''
    };
    safeSet(FEEDBACK,JSON.stringify(feedback));
    if(fbState) fbState.textContent='已记录：待复核，不会直接改正文';
    refreshStatus();
  });

  function markCount(){
    var n=0; for(var k in marked){if(marked[k]) n++} return n;
  }
  function refreshStatus(){
    if(statusEl) statusEl.textContent='临时标记 '+markCount()+' 个';
    if(exportBtn) exportBtn.disabled=markCount()===0;
    var sel=document.getElementById('export-selected');
    if(sel) sel.disabled=markCount()===0;
    var fbe=document.getElementById('export-feedback');
    if(fbe) fbe.disabled=feedbackCount()===0;
    if(statusEl&&feedbackCount()>0){
      statusEl.textContent='临时标记 '+markCount()+' 个｜反馈 '+feedbackCount()+' 条';
    }
  }
  function syncMarkButtons(word){
    if(!masterBtn) return;
    var s=marked[word]||'';
    masterBtn.classList.toggle('on',s==='mastered');
    focusBtn.classList.toggle('on',s==='focus');
  }
  function setMark(word,status){
    if(!word) return;
    if(marked[word]===status) delete marked[word]; else marked[word]=status;
    saveMarks(); refreshStatus(); syncMarkButtons(word);
  }
  function activeKey(){
    if(!active) return '';
    return active.dataset.key||active.textContent.trim();
  }
  if(masterBtn) masterBtn.addEventListener('click',function(){
    if(active) setMark(activeKey(),'mastered');
  });
  if(focusBtn) focusBtn.addEventListener('click',function(){
    if(active) setMark(activeKey(),'focus');
  });
  if(clearBtn) clearBtn.addEventListener('click',function(){
    var k=activeKey();
    if(k&&marked[k]){
      delete marked[k]; saveMarks(); refreshStatus(); syncMarkButtons(k);
    }
  });
  function csvCell(v){
    v=(v===undefined||v===null)?'':String(v);
    return '"'+v.replace(/"/g,'""')+'"';
  }
  function downloadCsv(rows,name){
    var text=rows.map(function(r){return r.map(csvCell).join(',')})
      .join(String.fromCharCode(13,10));
    var blob=new Blob([String.fromCharCode(0xFEFF)+text],
      {type:'text/csv;charset=utf-8'});
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download=name;
    document.body.appendChild(a); a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},0);
  }
  if(exportBtn) exportBtn.addEventListener('click',function(){
    var rows=[['word','status']];
    Object.keys(marked).sort().forEach(function(w){
      if(marked[w]) rows.push([w,marked[w]]);
    });
    downloadCsv(rows,'vocab-word-list.csv');
  });

  /* §20.5 生词导出：只导当前书被标记的条目；原句与书名取自页面本身 */
  var selBtn=document.getElementById('export-selected');
  function currentBookEl(){
    return document.querySelector('.book.on')||document.querySelector('.book');
  }
  if(selBtn) selBtn.addEventListener('click',function(){
    var book=currentBookEl(); if(!book) return;
    var all=[].slice.call(book.querySelectorAll('span.vocab'));
    var rows=[['surface','lemma','gloss','sentence','book','chapter','locator','labels']];
    Object.keys(marked).sort().forEach(function(k){
      if(!marked[k]) return;
      var el=all.filter(function(s){return s.dataset.key===k})[0];
      if(!el) return;
      rows.push([el.textContent.trim(),k,el.dataset.gloss||'',el.dataset.sentence||'',
                 book.dataset.bookTitle||'',el.dataset.chapterLabel||'',
                 el.dataset.vocabId||'',el.dataset.labels||'']);
    });
    downloadCsv(rows,'vocab-selected.csv');
  });
  var fbExport=document.getElementById('export-feedback');
  if(fbExport) fbExport.addEventListener('click',function(){
    var items=Object.keys(feedback).map(function(k){return feedback[k]});
    var payload={schema:'vocab-reader-feedback/v1',
                 exported_at:new Date().toISOString(),items:items};
    var blob=new Blob([JSON.stringify(payload,null,2)],
      {type:'application/json;charset=utf-8'});
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download='vocab-feedback.json';
    document.body.appendChild(a); a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},0);
  });
  refreshStatus();

  /* 词语可点 + 键盘可操作 */
  Array.prototype.forEach.call(document.querySelectorAll('span.vocab'),function(el){
    el.setAttribute('tabindex','0');
    el.setAttribute('role','button');
    el.setAttribute('aria-label',el.textContent.trim()+'，查看释义');
    el.addEventListener('click',function(){openDetail(el)});
    el.addEventListener('keydown',function(e){
      if(e.key==='Enter'||e.key===' '){e.preventDefault();openDetail(el)}
    });
  });
  var closeBtn=document.getElementById('detail-close');
  if(closeBtn) closeBtn.addEventListener('click',closeDetail);
  if(dlg) dlg.addEventListener('cancel',function(e){e.preventDefault();closeDetail()});
    if(dlg) dlg.addEventListener('click',function(e){
    if(e.target===dlg){closeDetail()}
  });

  /* 从章末编号跳到词条时，自动展开折叠块 */
  if(location.hash.indexOf('vnote-')>=0){
    var target=document.getElementById(location.hash.slice(1));
    if(target){var d=target.closest('details'); if(d) d.open=true}
  }
})();
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>{css}</style>
<script>document.documentElement.className+=' js';</script>
</head><body><div class="wrap">
<header class="top">
<h1>{title}</h1>
<div class="sub">{subtitle}</div>
</header>
<div class="bar">
{tabs}
<label class="switch"><input type="checkbox" id="notes-toggle" checked/>中文提示</label>
</div>
<div class="markbar">
<span>正文里的词都可以点开看短义与完整资料；隐藏中文提示后仍可点。</span>
<div style="flex:1"></div>
<span id="status">临时标记 0 个</span>
<button id="export-marks" disabled>导出名单 CSV</button>
<button id="export-selected" disabled>导出生词 CSV</button>
<button id="export-feedback" disabled>导出反馈 JSON</button>
</div>
{books}
<dialog id="detail" aria-label="词条详解">
  <div id="detail-body"></div>
  <div class="fb">
    <div class="fb-title">这条注释有问题？反馈先记为待复核，不会直接改动正文。</div>
    <div class="fb-btns">
      <button type="button" data-kind="sense">意思不对</button>
      <button type="button" data-kind="span">组合范围不对</button>
      <button type="button" data-kind="too_much">解释太多</button>
    </div>
    <textarea id="fb-note" rows="2" placeholder="可补充说明（选填）"></textarea>
    <div class="fb-row">
      <button type="button" id="fb-submit">提交反馈</button>
      <span class="fb-state" id="fb-state"></span>
    </div>
  </div>
  <div id="detail-actions">
    <button id="mark-mastered">标记已掌握</button>
    <button id="mark-focus">标记重点学习</button>
    <button id="mark-clear">清除</button>
    <span class="spacer"></span>
    <button id="detail-close">关闭</button>
  </div>
</dialog>
<footer>{footer}</footer>
</div>
<script>{script}</script>
</body></html>
"""


def local_name(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def plain_text(el) -> str:
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node.tag, str) and "vocab-note" in (node.get("class") or "").split():
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(el)
    return " ".join("".join(parts).split())


def inline_images(root, zf: zipfile.ZipFile, doc_path: str) -> None:
    names = set(zf.namelist())
    for img in root.iter():
        if local_name(img) != "img":
            continue
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), src))
        if resolved not in names:
            continue
        mime = "image/jpeg" if resolved.lower().endswith((".jpg", ".jpeg")) else "image/png"
        payload = base64.b64encode(zf.read(resolved)).decode("ascii")
        img.set("src", f"data:{mime};base64,{payload}")


def prefix_anchors(root, prefix: str) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        ident = el.get("id")
        if ident:
            el.set("id", prefix + ident)
        href = el.get("href")
        if href and href.startswith("#") and len(href) > 1:
            el.set("href", "#" + prefix + href[1:])


def decorate_vocab(root) -> None:
    """生成时就把词语做成可聚焦控件，并写入名单用的可信键。

    §21.3：单词用章末条目里的 ``data-head``（已核验原形）作为键，
    词组保留完整原文整体，未收录词不伪造原形。名单标记、状态回显、撤销、
    导出都用 ``data-key``，避免网页导出 ``swallowing``、手工写 ``swallow``
    两套键对不上。
    """

    heads: dict[tuple[str, str], str] = {}
    for el in root.iter():
        if not isinstance(el.tag, str) or local_name(el) != "li":
            continue
        if "vocab-item" not in (el.get("class") or "").split():
            continue
        match = re.search(r"vnote-(\d+)-(\d+)$", el.get("id") or "")
        if match and el.get("data-head"):
            heads[(match.group(1), match.group(2))] = el.get("data-head") or ""

    for el in root.iter():
        if not isinstance(el.tag, str) or local_name(el) != "span":
            continue
        classes = (el.get("class") or "").split()
        if "vocab" not in classes:
            continue
        surface = (el.text or "").strip()
        chapter = el.get("data-chapter") or ""
        note = el.get("data-note") or ""
        head = heads.get((chapter, note), "")
        el.set("data-key", head or surface)
        el.set("data-head", head)
        el.set("tabindex", "0")
        el.set("role", "button")
        if surface:
            el.set("aria-label", f"{surface}，查看释义")


def _cls(el) -> list[str]:
    return (el.get("class") or "").split()


def _sentence_at(text: str, pos: int) -> str:
    """按 .?! 切句，返回包含该字符位置的句子。"""

    bounds: list[int] = []
    for match in re.finditer(r"[.!?]+[\"'”’]?\s*", text):
        bounds.append(match.end())
    start = 0
    for end in bounds + [len(text)]:
        if start <= pos < end:
            return text[start:end].strip()
        start = end
    return text.strip()


def annotate_metadata(root) -> None:
    """给每个正文词写下导出需要的字段：短义、等级、章节、原句。

    §20.5 要求生词导出的原句来自源书、不由模型重写；这里直接从渲染后的
    段落文本里切句，和正文完全同源。
    """

    meta: dict[tuple[str, str], dict] = {}
    for section in root.iter():
        # §22.4：EPUB 2 产物把章末 <section> 换成了合规的 <div>；
        # 旧版产物仍是 <section>，两种都认。
        if not isinstance(section.tag, str) or local_name(section) not in (
            "div",
            "section",
        ):
            continue
        if "vocab-chapter-notes" not in _cls(section):
            continue
        title = ""
        for heading in section.iter():
            if isinstance(heading.tag, str) and local_name(heading) == "h2":
                if "vocab-notes-title" in _cls(heading):
                    title = " ".join("".join(heading.itertext()).split())
        # 章末标题形如「本章词汇（Chapter I）」，导出只保留章节名
        bracket = re.search(r"[（(]([^）)]+)[）)]", title)
        if bracket:
            title = bracket.group(1)
        else:
            title = re.sub(r"词汇", "", title).strip()
        for item in section.iter():
            if not isinstance(item.tag, str) or local_name(item) != "li":
                continue
            if "vocab-item" not in _cls(item):
                continue
            match = re.search(r"vnote-(\d+)-(\d+)$", item.get("id") or "")
            if not match:
                continue
            gloss = ""
            labels = ""
            for node in item.iter():
                if not isinstance(node.tag, str):
                    continue
                if local_name(node) == "p" and "vocab-sense" in _cls(node):
                    gloss = node.get("data-gloss") or ""
                if local_name(node) == "span" and "vocab-grade" in _cls(node):
                    labels = " ".join("".join(node.itertext()).split())
                    labels = re.sub(r"^[\s|｜]+", "", labels)
            meta[(match.group(1), match.group(2))] = {
                "head": item.get("data-head") or "",
                "gloss": gloss,
                "labels": labels,
                "chapter": title,
            }

    for para in root.iter():
        if not isinstance(para.tag, str) or local_name(para) not in ("p", "div"):
            continue
        spans = [
            node
            for node in para.iter()
            if isinstance(node.tag, str)
            and local_name(node) == "span"
            and "vocab" in _cls(node)
            and "vocab-note" not in _cls(node)
        ]
        if not spans:
            continue
        text = ""
        positions: dict[int, int] = {}

        def walk(node) -> None:
            nonlocal text
            if isinstance(node.tag, str) and "vocab-note" in _cls(node):
                return
            if (
                isinstance(node.tag, str)
                and local_name(node) == "span"
                and "vocab" in _cls(node)
            ):
                positions[id(node)] = len(text)
            if node.text:
                text += node.text
            for child in node:
                walk(child)
                if child.tail:
                    text += child.tail

        walk(para)
        for span in spans:
            pos = positions.get(id(span))
            if pos is None:
                continue
            chapter = span.get("data-chapter") or ""
            note = span.get("data-note") or ""
            info = meta.get((chapter, note), {})
            span.set("data-sentence", _sentence_at(text, pos))
            span.set("data-gloss", info.get("gloss") or "")
            span.set("data-labels", info.get("labels") or "")
            span.set("data-chapter-label", info.get("chapter") or "")


def serialize(el) -> str:
    raw = etree.tostring(el, encoding="unicode", with_tail=True)
    return raw.replace(' xmlns="http://www.w3.org/1999/xhtml"', "").strip()


def body_of(zf: zipfile.ZipFile, doc_path: str):
    root = etree.fromstring(zf.read(doc_path))
    return next(e for e in root.iter() if local_name(e) == "body")


def wrap_notes(section_el, count_label: str) -> str:
    """把章末词条块放进 <details>，默认收起，避免打断连续阅读。"""

    html = serialize(section_el)
    title = count_label or "本章词汇"
    return (
        f'<details class="notes-wrap"><summary>{title}（点击展开完整资料）</summary>'
        f"{html}</details>"
    )


def load_wanted(source: str, doc: str) -> list[str]:
    """从 units 目录的 windows-*.json 读出该文档的段落原文前缀。"""

    texts: list[str] = []
    for file in sorted(Path(source).glob("windows-*.json")):
        payload = json.loads(file.read_text(encoding="utf-8"))
        for window in payload["windows"]:
            for para in window["paragraphs"]:
                if para["pid"].split("#")[0] == doc:
                    texts.append(" ".join(para["text"].split())[:38])
    if not texts:
        raise SystemExit(f"wanted_source 里找不到 {doc} 的段落：{source}")
    return texts


def notes_block(body, label: str) -> str:
    for el in body:
        if not isinstance(el.tag, str):
            continue
        classes = (el.get("class") or "").split()
        if "vocab-chapter-notes" not in classes:
            continue
        # 词条已就近放在段后；隐藏数据层不再另起一份折叠词表，避免重复。
        if "vocab-archive" in classes:
            return ""
        return wrap_notes(deepcopy(el), label)
    return ""


def extract(
    zf: zipfile.ZipFile,
    prefix: str,
    sections: list[dict],
    binding: dict | None = None,
) -> str:
    """按配置抽取章节：整篇（all）或按段落原文前缀匹配（wanted_source）。"""

    cache: dict[str, object] = {}
    chunks: list[str] = []
    last_of_doc: dict[str, int] = {}
    for index, section in enumerate(sections):
        last_of_doc[section["doc"]] = index

    for index, section in enumerate(sections):
        doc = section["doc"]
        if doc not in cache:
            body = body_of(zf, doc)
            inline_images(body, zf, doc)
            # §22.4：EPUB 内不含 data-*，按稳定 id 从包外映射注入内存 DOM。
            # 必须在 prefix_anchors 改写 id 之前，否则对不上。
            if binding:
                apply_binding(body, binding)
            prefix_anchors(body, prefix)
            decorate_vocab(body)
            annotate_metadata(body)
            cache[doc] = body
        body = cache[doc]
        anchor = f'id="{prefix}ch-{index + 1}"'
        is_last = last_of_doc[doc] == index
        label = section.get("notes_label", "本章词汇")

        if section.get("all"):
            bounds = section.get("slice")
            if bounds is None and (section.get("start_id") or section.get("end_id")):
                start = _index_of_id(body, section.get("start_id"))
                end = _index_of_id(body, section.get("end_id"))
                bounds = (
                    0 if start is None else start,
                    len(body) - 1 if end is None else max(end - 1, 0),
                )
            parts = []
            for pos, el in enumerate(body):
                if not isinstance(el.tag, str):
                    continue
                if bounds and not (bounds[0] <= pos <= bounds[1]):
                    continue
                if "vocab-chapter-notes" in (el.get("class") or "").split():
                    continue
                parts.append(serialize(deepcopy(el)))
            inner = "\n".join(parts)
        else:
            wanted = section.get("wanted")
            if wanted is None:
                wanted = load_wanted(section["wanted_source"], doc)
            wanted = [w[:38] for w in wanted]
            matched = []
            for el in body:
                if not isinstance(el.tag, str) or local_name(el) != "p":
                    continue
                text = plain_text(el)
                if text and any(text.startswith(w) for w in wanted):
                    matched.append(el)
            if len(matched) < len(wanted):
                raise SystemExit(
                    f"{doc} 段落匹配 {len(matched)}/{len(wanted)}，拒绝生成残缺页面"
                )
            start = int(section.get("start") or 0)
            take = int(section.get("take") or len(wanted))
            chosen = matched[start : start + take]
            inner = "\n".join(serialize(deepcopy(el)) for el in chosen)

        is_sliced = bool(
            section.get("slice") or section.get("start_id") or section.get("end_id")
        )
        if is_last and not is_sliced:
            # 按章节切片时，本章词条块已落在切片内，不能再追加一遍
            block = notes_block(body, label)
            if block:
                inner += "\n" + block

        heading = section["label"]
        hint = section.get("hint") or ""
        small = f"<small>{hint}</small>" if hint else ""
        chunks.append(
            f'<article class="chapter"><h2 {anchor}>{heading}{small}</h2>{inner}</article>'
        )
    return "\n".join(chunks)


def _binding_for(epub_path: Path) -> dict | None:
    """§22.4：新产物把内部信息放在包外映射；旧产物仍带 data-*，保持兼容。"""

    path = binding_path_for(epub_path)
    if not path.is_file():
        return None
    try:
        return load_binding(path)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"标注映射无法读取：{path}\n{exc}") from exc


def _file_version(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return digest


def _index_of_id(body, ident: str | None) -> int | None:
    """元素在 body 直接子节点中的位置（含后代命中）。"""

    if not ident:
        return None
    for pos, el in enumerate(body):
        if not isinstance(el.tag, str):
            continue
        if el.get("id") == ident:
            return pos
        for node in el.iter():
            if isinstance(node.tag, str) and node.get("id") == ident:
                return pos
    return None


def _parse_range(spec: str | None, total: int) -> list[int]:
    """``1-2`` / ``3`` / 空 → 0 基章节索引；越界项直接丢掉。"""

    if not spec or spec.strip().lower() in ("all", "*"):
        return list(range(total))
    picked: list[int] = []
    for part in re.split(r"[,，]", spec):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            picked.extend(range(int(left), int(right) + 1))
        else:
            picked.append(int(part))
    ordered = sorted({index - 1 for index in picked if 1 <= index <= total})
    if not ordered:
        raise SystemExit(f"章节范围 {spec!r} 超出本书范围（共 {total} 章）")
    return ordered


def _doc_label(zf: zipfile.ZipFile, doc: str) -> str:
    """无目录时的兜底标题：doc 的 title，或首个 h1–h3 文本。"""

    try:
        root = etree.fromstring(zf.read(doc))
    except Exception:
        return Path(doc).stem
    for el in root.iter():
        if isinstance(el.tag, str) and local_name(el) == "title" and (el.text or "").strip():
            return el.text.strip()[:40]
    for el in root.iter():
        if isinstance(el.tag, str) and local_name(el) in ("h1", "h2", "h3"):
            text = " ".join("".join(el.itertext()).split())
            if text:
                return text[:40]
    return Path(doc).stem


def _chapter_entries(chapter, spine: list[str], label: str) -> list[dict]:
    """一章 → 若干 section；跨 XHTML 的章节按文档拆开但共用标题。"""

    if chapter.start_path == chapter.end_path:
        return [
            {
                "doc": chapter.start_path,
                "label": label,
                "all": True,
                "start_id": chapter.start_id,
                "end_id": chapter.end_before_id,
            }
        ]
    try:
        first = spine.index(chapter.start_path)
        last = spine.index(chapter.end_path)
    except ValueError:  # 目录指向 spine 之外，退回单文档
        return [{"doc": chapter.start_path, "label": label, "all": True}]
    entries: list[dict] = []
    for pos in range(first, last + 1):
        doc = spine[pos]
        entry = {"doc": doc, "label": label if pos == first else f"{label}（续）", "all": True}
        if pos == first:
            entry["start_id"] = chapter.start_id
        if pos == last:
            entry["end_id"] = chapter.end_before_id
        entries.append(entry)
    return entries


def auto_sections(epub_path: Path, chapters_spec: str | None = None) -> list[dict]:
    """从最终产物自动生成章节配置。

    优先用原书目录（nav / NCX）识别逻辑章节——「前两章」按章节选，不是按段落数；
    没有目录时退回 spine 顺序，一章一个 XHTML。不依赖任何示例配置或固定文件名。
    """

    archive, package = open_epub(epub_path)
    try:
        spine = [item.item.path for item in package.spine]
        chapters = detect_chapters(archive, package)
        labels = None
        if not chapters:
            with zipfile.ZipFile(epub_path) as zf:
                labels = [_doc_label(zf, doc) for doc in spine]
    finally:
        archive.close()

    if chapters:
        picked = _parse_range(chapters_spec, len(chapters))
        sections: list[dict] = []
        for index in picked:
            chapter = chapters[index]
            label = (chapter.label or "").strip() or f"第 {index + 1} 章"
            sections.extend(_chapter_entries(chapter, spine, label))
        return sections

    if not spine:
        raise SystemExit(f"产物里没有可阅读的正文文档：{epub_path}")
    picked = _parse_range(chapters_spec, len(spine))
    return [
        {"doc": spine[index], "label": (labels or [])[index], "all": True}
        for index in picked
    ]


def render(config: dict) -> Path:
    """按配置渲染阅读页；返回实际写入路径。"""

    books_html: list[str] = []
    tabs: list[str] = []
    for index, book in enumerate(config["books"]):
        active = " on" if index == 0 else ""
        tab_active = " on" if index == 0 else ""
        tabs.append(
            f'<button class="tab{tab_active}" data-book="{book["id"]}">{book["tab"]}</button>'
        )
        sections: list[dict] = book["sections"]
        book_toc = "".join(
            f'<a href="#{book["prefix"]}ch-{order}">{section["label"]}</a>'
            for order, section in enumerate(sections, start=1)
        )
        epub_path = Path(book["epub"])
        if not epub_path.exists():
            raise SystemExit(
                f"找不到标注产物：{epub_path}\n"
                "它是 --apply-annotations 的临时输出，可能已被清理。重跑：\n"
                "  python scripts/run_pipeline.py --book <书> --lexicon <词库> "
                "--engine units --apply-annotations <units 目录> --reading-level <档位> "
                "--out <临时目录> --report <临时报告> [窗口参数与 emit 时一致] --no-epubcheck"
            )
        binding = _binding_for(epub_path)
        with zipfile.ZipFile(epub_path) as zf:
            inner = extract(zf, book["prefix"], sections, binding)
        meta = book.get("meta", "")
        # 版本随产物字节变化：换了档位、改了词注都会让旧阅读位置失效
        version = str(book.get("version") or _file_version(epub_path))
        books_html.append(
            f'<section class="book{active}" id="book-{book["id"]}" '
            f'data-book="{book["id"]}" data-prefix="{book["prefix"]}" '
            f'data-book-title="{book.get("book_title", book["tab"])}" '
            f'data-version="{version}">'
            f'<div class="bookmeta">{meta}</div>'
            f'<nav class="toc" aria-label="章节">{book_toc}</nav>{inner}</section>'
        )
    # 不用 str.format：CSS/JS 里全是花括号，会被当成占位符
    html = TEMPLATE
    for token, value in (
        ("{title}", config["title"]),
        ("{subtitle}", config["subtitle"]),
        ("{css}", CSS),
        ("{script}", SCRIPT),
        ("{tabs}", "\n".join(tabs)),
        ("{books}", "\n".join(books_html)),
        ("{footer}", config["footer"]),
    ):
        html = html.replace(token, value)
    output = Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output


def build(config_path: Path) -> Path:
    return render(json.loads(config_path.read_text(encoding="utf-8")))


def build_single(
    epub_path: str | Path,
    output: str | Path,
    *,
    chapters: str | None = None,
    title: str | None = None,
    force: bool = False,
) -> dict:
    """单本书的自动模式：只给产物路径，章节、标签、目录全部自动推导。"""

    epub = Path(epub_path)
    target = Path(output)
    if not epub.is_file():
        raise SystemExit(f"找不到标注产物：{epub}")
    if target.exists() and not force:
        raise SystemExit(f"阅读页已存在，未覆盖：{target}\n需要覆盖时加 --force")

    sections = auto_sections(epub, chapters)
    if not sections:
        raise SystemExit(f"没有可写入的章节：{epub}")

    archive, package = open_epub(epub)
    try:
        book_title = (package.title or "").strip() or epub.stem
    finally:
        archive.close()

    config = {
        "title": title or f"{book_title} · 伴读版",
        "subtitle": f"共 {len(sections)} 节 · 正文与章末词条来自同一份标注产物",
        "output": str(target),
        "footer": "本页由词汇伴读离线生成；词注、编号与章末词条和 EPUB 产物一致。",
        "books": [
            {
                "id": "book",
                "tab": book_title,
                "book_title": book_title,
                "prefix": "b1-",
                "epub": str(epub),
                "meta": "",
                "sections": sections,
            }
        ],
    }
    path = render(config)
    return {
        "ok": True,
        "path": str(path),
        "bytes": path.stat().st_size,
        "sections": len(sections),
        "epub": str(epub),
    }


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="从标注产物生成离线阅读页")
    parser.add_argument("--config", metavar="JSON", help="多书配置（旧模式）")
    parser.add_argument("--epub", metavar="EPUB", help="标注产物；自动推导章节")
    parser.add_argument("--out", metavar="HTML", help="输出阅读页路径")
    parser.add_argument(
        "--chapters", metavar="RANGE", help="章节范围，如 1-2 或 3；默认全书"
    )
    parser.add_argument("--title", metavar="TEXT", help="页面标题，默认用书名")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的阅读页")
    args = parser.parse_args()

    if args.config and (args.epub or args.out):
        parser.error("--config 与 --epub/--out 不能同时使用")
    if args.config:
        output = build(Path(args.config))
    elif args.epub and args.out:
        info = build_single(
            args.epub,
            args.out,
            chapters=args.chapters,
            title=args.title,
            force=args.force,
        )
        output = Path(info["path"])
    else:
        parser.error("需要 --config，或同时给出 --epub 与 --out")
    print(f"阅读页已生成 -> {output}  ({output.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
