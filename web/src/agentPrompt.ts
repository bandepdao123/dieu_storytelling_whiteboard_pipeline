export type AgentSnapshot = {
 project: {id:string; name:string; version:number; state:string};
 script:string|null; audio:null|{duration_ms:number};
 scenes:{qa_state:string; approval_state:string}[];
 artifacts:{kind:string; status:string}[];
 next_actions:string[]; gates:Record<string,{current:boolean}>;
};

export function nextAgentStep(d:AgentSnapshot):string {
 if(d.project.state==='PAUSED') return 'Đang tạm dừng: đọc nguyên nhân, xin Sếp xác nhận; không tự tiếp tục.';
 if(d.project.state==='BLOCKED') return 'Đang bị chặn: kiểm tra nguyên nhân, đề xuất xử lý; không tự gỡ chặn.';
 if(d.project.state!=='ACTIVE') return 'Chỉ rà soát kết quả và trạng thái; không tự mở lại dự án.';
 if(!d.script?.trim()) return 'Đề nghị Sếp cung cấp TXT kịch bản nguồn; không tự viết thay.';
 if(!d.audio) return 'Đề nghị Sếp cung cấp PCM WAV khớp kịch bản; không tự gọi TTS trả phí.';
 if(!d.scenes.length) return d.next_actions.includes('plan')
  ? 'Kiểm tra WAV và phụ đề, rồi lập kế hoạch cảnh cục bộ nếu vẫn đủ điều kiện.'
  : 'Kiểm tra điều kiện lập cảnh: chưa rõ phụ đề/cue; chỉ xin SRT khớp WAV nếu thiếu.';
 // A scene count or partial snapshot cannot establish downstream readiness.
 return 'Giữ kế hoạch cảnh; đối chiếu QA, tệp và duyệt để tìm gate chưa đạt tiếp theo; chưa rõ thì báo Sếp.';
}

export function buildAgentPrompt(d:AgentSnapshot):string {
 return `Mở https://audiobooks.io.vn, tìm dự án ID ${JSON.stringify(d.project.id)}.
Bước tiếp theo: ${nextAgentStep(d)}
Đọc lại dự án và gate hiện hành trước khi làm; không vượt duyệt, chưa rõ thì hỏi Sếp.`;
}
