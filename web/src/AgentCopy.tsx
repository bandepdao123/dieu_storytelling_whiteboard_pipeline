import {useState} from 'react';
import {buildAgentPrompt, type AgentSnapshot} from './agentPrompt';

export function AgentCopy({detail,disabled}:{detail:AgentSnapshot;disabled:boolean}) {
 const [state,setState]=useState<'idle'|'copying'|'copied'|'failed'>('idle');
 const [fallback,setFallback]=useState('');
 async function copy(){
  const text=buildAgentPrompt(detail);
  setState('copying');setFallback('');
  try {
   // Invoke synchronously inside the click gesture (no fetch/await beforehand).
   await navigator.clipboard.writeText(text);
   setState('copied');
  } catch {
   setFallback(text);setState('failed');
  }
 }
 return <section className="agent-copy" aria-label="Sao chép yêu cầu cho Agent">
  <div className="actions"><button type="button" disabled={disabled||state==='copying'} onClick={copy} aria-describedby="agent-copy-help">{state==='copying'?'Đang sao chép…':'Gửi Agent'}</button><span id="agent-copy-help" className="muted">Chỉ sao chép yêu cầu để Sếp dán cho Agent; không gửi, không tự chạy tác vụ.</span></div>
  {state==='copied'&&<p role="status">Đã sao chép yêu cầu. Sếp dán vào cuộc trò chuyện với Agent để gửi.</p>}
  {state==='failed'&&<div><p role="alert">Trình duyệt không cho sao chép. Chọn nội dung bên dưới rồi sao chép thủ công (Ctrl/Cmd+C).</p><label>Yêu cầu cho Agent<textarea readOnly value={fallback} rows={12} onFocus={e=>e.currentTarget.select()} /></label><button type="button" onClick={e=>{const textarea=e.currentTarget.parentElement?.querySelector('textarea');textarea?.focus();textarea?.select();}}>Chọn toàn bộ yêu cầu</button></div>}
 </section>;
}
