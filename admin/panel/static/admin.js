const $ = s => document.querySelector(s);
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const shortDate = value => value ? new Date(value).toLocaleString() : '—';
async function api(url, options) { const r = await fetch(url, options); if (!r.ok) throw new Error((await r.json()).detail || 'Request failed'); return r.json(); }
async function load() {
  try {
    const [users, history] = await Promise.all([api('/api/admin/users'), api('/api/admin/history')]);
    $('#users').innerHTML = users.map(u => `<tr><td>${escapeHtml(u.ip_address)}</td><td>••••${escapeHtml(u.token_hint)}</td><td>${escapeHtml(u.key_label)}</td><td>${shortDate(u.created_at)}</td><td>${shortDate(u.last_seen_at)}</td><td class="${u.suspended || u.ip_banned ? 'status-bad':'status-good'}">${u.ip_banned ? 'IP banned' : u.suspended ? 'Token suspended' : 'Active'}</td><td><button onclick="action('${u.id}','token',${u.suspended ? 'false':'true'})">${u.suspended ? 'Restore token':'Suspend token'}</button> <button onclick="action('${u.id}','ip',${u.ip_banned ? 'false':'true'})">${u.ip_banned ? 'Unban IP':'Ban IP'}</button></td></tr>`).join('') || '<tr><td colspan="7">No users yet.</td></tr>';
    $('#history').innerHTML = history.map(e => `<tr><td>${shortDate(e.created_at)}</td><td>${escapeHtml(e.ip_address)}</td><td>${escapeHtml(e.outcome)}</td><td>${escapeHtml(e.detail)}</td></tr>`).join('') || '<tr><td colspan="4">No login events yet.</td></tr>';
  } catch (err) { alert(err.message); }
}
window.action = async (id, scope, suspend) => { if (!confirm(`${suspend ? 'Apply':'Remove'} ${scope} restriction?`)) return; await api(`/api/admin/users/${id}/${suspend ? 'suspend':'restore'}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scope})}); load(); };
$('#key-form').addEventListener('submit', async event => { event.preventDefault(); const form = new FormData(event.target); const data = {label: form.get('label'), max_uses:Number(form.get('max_uses'))}; if (form.get('expires_days')) data.expires_days = Number(form.get('expires_days')); const result = await api('/api/admin/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); const out=$('#new-key'); out.hidden=false; out.textContent=`Give this key to the user now. It cannot be viewed again:\n\n${result.key}`; event.target.reset(); event.target.max_uses.value=1; });
load();
