'use strict';
function markdown(source){
 return DOMPurify.sanitize(marked.parse(source,{gfm:true,breaks:false}),{
  USE_PROFILES:{html:true},FORBID_TAGS:['img','input','form','button','style'],
  FORBID_ATTR:['style','id','name']
 });
}
// Reconcile existing blocks and text nodes instead of replacing the answer on
// each streaming update. Previously completed paragraphs keep their identity.
function renderMarkdown(target,source){
 const template=document.createElement('template');template.innerHTML=markdown(source);
 function patch(parent,next){
  for(let i=0;i<next.childNodes.length;i++){
   const desired=next.childNodes[i],current=parent.childNodes[i];
   if(!current){parent.append(desired.cloneNode(true));continue;}
   if(current.nodeType!==desired.nodeType||current.nodeName!==desired.nodeName){current.replaceWith(desired.cloneNode(true));continue;}
   if(current.nodeType===Node.TEXT_NODE){if(current.data!==desired.data)current.data=desired.data;continue;}
   if(current.nodeType===Node.ELEMENT_NODE){
    for(const a of [...current.attributes])if(!desired.hasAttribute(a.name))current.removeAttribute(a.name);
    for(const a of desired.attributes)if(current.getAttribute(a.name)!==a.value)current.setAttribute(a.name,a.value);
    patch(current,desired);
   }
  }
  while(parent.childNodes.length>next.childNodes.length)parent.lastChild.remove();
 }
 patch(target,template.content);
}
